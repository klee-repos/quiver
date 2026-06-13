#!/usr/bin/env bash
# Quiver headless trader — EC2 provisioning (Ubuntu 24.04, ARM64/t4g). Idempotent.
# Adapted from the BRAX setup.sh; the only real swap is secrets: az keyvault -> AWS
# SSM Parameter Store, fetched via the instance-role (no creds on disk). Run as root.
set -euo pipefail

QUIVER_USER="${QUIVER_USER:-quiver}"
QUIVER_HOME="${QUIVER_HOME:-/opt/quiver}"
SSM_PREFIX="${SSM_PREFIX:-/quiver}"
AWS_REGION="${AWS_REGION:-us-east-1}"

echo "[1/8] apt deps"
apt-get update -y
apt-get install -y curl ca-certificates gnupg jq git python3-venv build-essential unzip

echo "[2/8] Node 20 (the claude CLI runtime)"
command -v node >/dev/null || { curl -fsSL https://deb.nodesource.com/setup_20.x | bash - ; apt-get install -y nodejs ; }

echo "[3/8] claude CLI"
npm i -g @anthropic-ai/claude-code

echo "[4/8] AWS CLI v2"
command -v aws >/dev/null || { curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o /tmp/awscliv2.zip ; unzip -q -o /tmp/awscliv2.zip -d /tmp ; /tmp/aws/install --update ; }

echo "[5/8] service user + repo + venv"
id -u "$QUIVER_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$QUIVER_USER"
mkdir -p "$QUIVER_HOME" /etc/quiver /var/log/quiver
if [ -n "${QUIVER_REPO_URL:-}" ] && [ ! -d "$QUIVER_HOME/.git" ]; then git clone "$QUIVER_REPO_URL" "$QUIVER_HOME" ; fi
python3 -m venv "$QUIVER_HOME/.venv"
"$QUIVER_HOME/.venv/bin/pip" install -q -e "$QUIVER_HOME"
chown -R "$QUIVER_USER:$QUIVER_USER" "$QUIVER_HOME" /var/log/quiver

echo "[6/8] secrets: SSM -> /etc/quiver/quiver.env (instance-role auth)"
fetch() { aws ssm get-parameter --region "$AWS_REGION" --name "$SSM_PREFIX/$1" --with-decryption --query Parameter.Value --output text ; }
{
  echo "ANTHROPIC_API_KEY=$(fetch ANTHROPIC_API_KEY)"
  echo "DEEPSEEK_API_KEY=$(fetch DEEPSEEK_API_KEY)"
  echo "RH_ACCOUNT_NUMBER=$(fetch RH_ACCOUNT_NUMBER)"
  echo "RH_OAUTH_TOKEN=$(fetch RH_OAUTH_TOKEN)"
  echo "RESEND_API_KEY=$(fetch RESEND_API_KEY)"
  echo "NOTIFY_TO=$(fetch NOTIFY_TO)"
} > /etc/quiver/quiver.env
chmod 600 /etc/quiver/quiver.env
chown "$QUIVER_USER:$QUIVER_USER" /etc/quiver/quiver.env
[ -f "$QUIVER_HOME/deploy/runner/mcp.json" ] || cp "$QUIVER_HOME/deploy/runner/mcp.json.example" "$QUIVER_HOME/deploy/runner/mcp.json"

echo "[7/8] systemd units + timer"
cp "$QUIVER_HOME/deploy/quiver.service" /etc/systemd/system/quiver.service
cp "$QUIVER_HOME/deploy/quiver.timer" /etc/systemd/system/quiver.timer
systemctl daemon-reload
systemctl enable --now quiver.timer

echo "[8/8] offline healthcheck"
sudo -u "$QUIVER_USER" "$QUIVER_HOME/.venv/bin/python" "$QUIVER_HOME/deploy/runner/healthcheck.py"

cat <<'NOTE'
DONE. Two steps that CANNOT be automated:
  1) Robinhood MCP auth (the token has NO refresh — re-auth is interactive): as the
     quiver user run `claude` then `/mcp` to authenticate, and push the resulting
     token to SSM ($SSM_PREFIX/RH_OAUTH_TOKEN). On expiry, repeat and re-run setup.
  2) Keep config.yaml dry_run=true for the soak; flip to false on a FRESH trading
     day only after a clean dry-run validation (clear that day's ledger rows first).
NOTE
