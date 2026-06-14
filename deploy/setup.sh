#!/usr/bin/env bash
# Quiver headless trader — EC2 provisioning (Ubuntu 24.04, ARM64/t4g). Idempotent.
# Adapted from the BRAX setup.sh; the only real swap is secrets: az keyvault -> AWS
# SSM Parameter Store, fetched via the instance-role (no creds on disk). Run as root.
set -euo pipefail

QUIVER_USER="${QUIVER_USER:-quiver}"
QUIVER_HOME="${QUIVER_HOME:-/opt/quiver}"
SSM_PREFIX="${SSM_PREFIX:-/quiver}"
AWS_REGION="${AWS_REGION:-us-east-1}"

echo "[1/9] apt deps"
apt-get update -y
apt-get install -y curl ca-certificates gnupg jq git python3-venv build-essential unzip

echo "[2/9] Node 20 (the claude CLI runtime)"
command -v node >/dev/null || { curl -fsSL https://deb.nodesource.com/setup_20.x | bash - ; apt-get install -y nodejs ; }

echo "[3/9] claude CLI"
npm i -g @anthropic-ai/claude-code

echo "[4/9] AWS CLI v2"
command -v aws >/dev/null || { curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscliv2.zip ; unzip -q -o /tmp/awscliv2.zip -d /tmp ; /tmp/aws/install --update ; }

echo "[5/9] service user + repo + venv"
id -u "$QUIVER_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$QUIVER_USER"
mkdir -p "$QUIVER_HOME" /etc/quiver /var/log/quiver

# Read-only deploy key (SSM) so a private-repo clone/pull works unattended. GitHub's
# host key is pinned (verified, not trust-on-first-use). Installed for root (this
# clone / standalone re-runs) and the quiver user (future `git -C /opt/quiver pull`).
GH_HOSTKEY='github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl'
install_deploy_key() {  # $1 = home dir, $2 = owner user/group
  install -d -m 700 -o "$2" -g "$2" "$1/.ssh"
  aws ssm get-parameter --region "$AWS_REGION" --name "$SSM_PREFIX/GITHUB_DEPLOY_KEY" \
    --with-decryption --query Parameter.Value --output text > "$1/.ssh/id_ed25519"
  echo "$GH_HOSTKEY" > "$1/.ssh/known_hosts"
  chmod 600 "$1/.ssh/id_ed25519"; chmod 644 "$1/.ssh/known_hosts"
  chown "$2:$2" "$1/.ssh/id_ed25519" "$1/.ssh/known_hosts"
}
install_deploy_key /root root
if [ -n "${QUIVER_REPO_URL:-}" ] && [ ! -d "$QUIVER_HOME/.git" ]; then git clone "$QUIVER_REPO_URL" "$QUIVER_HOME" ; fi
python3 -m venv "$QUIVER_HOME/.venv"
"$QUIVER_HOME/.venv/bin/pip" install -q -e "$QUIVER_HOME"
install_deploy_key "$(getent passwd "$QUIVER_USER" | cut -d: -f6)" "$QUIVER_USER"
chown -R "$QUIVER_USER:$QUIVER_USER" "$QUIVER_HOME" /var/log/quiver

echo "[6/9] secrets: SSM -> /etc/quiver/quiver.env (instance-role auth)"
fetch() { aws ssm get-parameter --region "$AWS_REGION" --name "$SSM_PREFIX/$1" --with-decryption --query Parameter.Value --output text ; }
# Optional params: empty (not an error) when the SSM parameter is absent.
fetch_opt() { aws ssm get-parameter --region "$AWS_REGION" --name "$SSM_PREFIX/$1" --with-decryption --query Parameter.Value --output text 2>/dev/null || echo "" ; }
# Required secrets: capture into top-level vars FIRST so `set -e` actually aborts on a
# missing param. A failing `$(fetch K)` *inside* `echo "K=$(...)"` does NOT trip set -e
# (echo exits 0), which would silently write an empty value — including a dead pager
# RESEND_API_KEY. The assignment form `_v=$(...)` does propagate the failure.
for _k in ANTHROPIC_API_KEY DEEPSEEK_API_KEY RH_ACCOUNT_NUMBER RH_OAUTH_TOKEN RESEND_API_KEY NOTIFY_TO; do
  _v=$(fetch "$_k") || { echo "FATAL: required SSM param $SSM_PREFIX/$_k is missing" >&2; exit 1; }
  { [ -n "$_v" ] && [ "$_v" != "None" ]; } || { echo "FATAL: required SSM param $SSM_PREFIX/$_k is empty" >&2; exit 1; }
  printf -v "$_k" '%s' "$_v"
done
{
  echo "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY"
  echo "DEEPSEEK_API_KEY=$DEEPSEEK_API_KEY"
  echo "RH_ACCOUNT_NUMBER=$RH_ACCOUNT_NUMBER"
  echo "RH_OAUTH_TOKEN=$RH_OAUTH_TOKEN"
  echo "RESEND_API_KEY=$RESEND_API_KEY"
  echo "NOTIFY_TO=$NOTIFY_TO"
  # Python last-resort alert sender (run_tick.py). RESEND_FROM must be a verified
  # Resend domain sender, else orchestrator-crash paging silently no-ops.
  echo "RESEND_FROM=$(fetch_opt RESEND_FROM)"
  echo "NOTIFY_ALERTS_TO=$(fetch_opt NOTIFY_ALERTS_TO)"
} > /etc/quiver/quiver.env
chmod 600 /etc/quiver/quiver.env
chown "$QUIVER_USER:$QUIVER_USER" /etc/quiver/quiver.env
[ -f "$QUIVER_HOME/deploy/runner/mcp.json" ] || cp "$QUIVER_HOME/deploy/runner/mcp.json.example" "$QUIVER_HOME/deploy/runner/mcp.json"

echo "[7/9] systemd units + timer"
cp "$QUIVER_HOME/deploy/quiver.service" /etc/systemd/system/quiver.service
cp "$QUIVER_HOME/deploy/quiver.timer" /etc/systemd/system/quiver.timer
systemctl daemon-reload
systemctl enable --now quiver.timer

echo "[8/9] CloudWatch agent — ship the tick log to /quiver/tick (so the alarms fire)"
# Logs-only agent: tails /var/log/quiver/tick.log -> the Terraform-owned /quiver/tick
# log group, where the metric-filter alarms (AUTH_ERROR / write_kill / plan error)
# page via SNS. The instance role already grants the needed logs:* perms.
CWCTL=/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl
if [ ! -x "$CWCTL" ]; then
  ARCH="$(dpkg --print-architecture)"   # arm64 on t4g
  curl -fsSL "https://amazoncloudwatch-agent.s3.amazonaws.com/ubuntu/${ARCH}/latest/amazon-cloudwatch-agent.deb" -o /tmp/amazon-cloudwatch-agent.deb
  dpkg -i -E /tmp/amazon-cloudwatch-agent.deb || apt-get install -f -y
fi
touch /var/log/quiver/tick.log                       # give the agent a file to tail at once
chown "$QUIVER_USER:$QUIVER_USER" /var/log/quiver/tick.log
"$CWCTL" -a fetch-config -m ec2 -s -c "file:$QUIVER_HOME/deploy/cloudwatch-agent.json"

echo "[9/9] offline healthcheck"
sudo -u "$QUIVER_USER" "$QUIVER_HOME/.venv/bin/python" "$QUIVER_HOME/deploy/runner/healthcheck.py"

cat <<'NOTE'
DONE. Two steps that CANNOT be automated:
  1) Robinhood MCP auth (the token has NO refresh — re-auth is interactive): as the
     quiver user run `claude` then `/mcp` to authenticate, and push the resulting
     token to SSM ($SSM_PREFIX/RH_OAUTH_TOKEN). On expiry, repeat and re-run setup.
  2) Keep config.yaml dry_run=true for the soak; flip to false on a FRESH trading
     day only after a clean dry-run validation (clear that day's ledger rows first).
NOTE
