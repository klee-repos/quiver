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

echo "[1b/9] 2G swapfile backstop (a transient tick+desktop memory peak swaps, not OOM-kills)"
if ! swapon --show 2>/dev/null | grep -q .; then
  { fallocate -l 2G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=2048; } \
    && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile \
    && { grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab; } \
    || echo "  NOTE: swap setup skipped/failed (non-fatal)"
fi

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
# NOTE: RH_OAUTH_TOKEN is GONE from the required set — the Robinhood MCP now authenticates
# via OAuth done once in the on-box browser (Chrome Remote Desktop) and stored under
# CLAUDE_CONFIG_DIR, NOT a static bearer in the env. See setup-desktop.sh / DEPLOY.md.
for _k in DEEPSEEK_API_KEY RH_ACCOUNT_NUMBER RESEND_API_KEY NOTIFY_TO; do
  _v=$(fetch "$_k") || { echo "FATAL: required SSM param $SSM_PREFIX/$_k is missing" >&2; exit 1; }
  { [ -n "$_v" ] && [ "$_v" != "None" ]; } || { echo "FATAL: required SSM param $SSM_PREFIX/$_k is empty" >&2; exit 1; }
  printf -v "$_k" '%s' "$_v"
done
# Claude Code auth is now OPTIONAL at provision time. If an SSM token is present we use it;
# otherwise the headless tick relies on an interactive `claude login` done ONCE in the
# on-box desktop (same CLAUDE_CONFIG_DIR). So this NEVER FATALs — a token-less box simply
# AUTH-fails safe on each tick until the operator logs in. Write ONLY one auth line (an
# ANTHROPIC_API_KEY would override a subscription token and bill the API).
_CCOT=$(fetch_opt CLAUDE_CODE_OAUTH_TOKEN)
_AAK=$(fetch_opt ANTHROPIC_API_KEY)
CLAUDE_AUTH_LINE=""
if [ -n "$_CCOT" ] && [ "$_CCOT" != "None" ]; then
  CLAUDE_AUTH_LINE="CLAUDE_CODE_OAUTH_TOKEN=$_CCOT"
elif [ -n "$_AAK" ] && [ "$_AAK" != "None" ]; then
  CLAUDE_AUTH_LINE="ANTHROPIC_API_KEY=$_AAK"
else
  echo "  NOTE: no Claude auth in SSM — the box will rely on an interactive 'claude login'"
  echo "        in the on-box desktop. Until then, ticks AUTH-fail safe (place nothing)."
fi
# All claude + MCP credential / OAuth state lives here: writable, refreshable, and under
# /opt/quiver/state so it survives the service's ProtectHome=read-only. BOTH the systemd
# service (via this quiver.env) and the desktop login session (via /etc/environment, set
# in setup-desktop.sh) point at the same dir, so the OAuth the operator does in the browser
# is the OAuth the headless tick reuses.
CLAUDE_CONFIG_DIR="$QUIVER_HOME/state/claude-config"
install -d -m 700 -o "$QUIVER_USER" -g "$QUIVER_USER" "$CLAUDE_CONFIG_DIR"
# Pre-create npx's writable cache (quiver.service redirects npm_config_cache here so the
# resend MCP digest send works under ProtectHome=read-only) — owned by quiver up front.
install -d -m 700 -o "$QUIVER_USER" -g "$QUIVER_USER" "$QUIVER_HOME/state/.npm-cache"
{
  [ -n "$CLAUDE_AUTH_LINE" ] && echo "$CLAUDE_AUTH_LINE"
  echo "CLAUDE_CONFIG_DIR=$CLAUDE_CONFIG_DIR"
  echo "DEEPSEEK_API_KEY=$DEEPSEEK_API_KEY"
  echo "RH_ACCOUNT_NUMBER=$RH_ACCOUNT_NUMBER"
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

# Register the Robinhood MCP at USER scope (no header -> the CLI does the OAuth dance when
# the operator authenticates it via `/mcp` in the on-box browser). run_tick.py drops
# --strict-mcp-config so the headless tick reuses this user-scope server + its stored OAuth;
# resend stays in mcp.json (static key, no OAuth). Best-effort: `claude mcp add` flags vary
# by CLI version and this is re-doable via `/mcp`, so a failure here must NOT abort the box.
_QHOME="$(getent passwd "$QUIVER_USER" | cut -d: -f6)"
if sudo -u "$QUIVER_USER" env HOME="$_QHOME" CLAUDE_CONFIG_DIR="$CLAUDE_CONFIG_DIR" \
     claude mcp add robinhood-trading --scope user --transport http \
     https://agent.robinhood.com/mcp/trading >/dev/null 2>&1; then
  echo "  registered robinhood-trading (user scope) — authenticate it via /mcp in the desktop"
else
  echo "  NOTE: 'claude mcp add robinhood-trading' skipped/failed — add it via /mcp on the box"
fi
# config.yaml carries no secrets and IS committed, so it ships in the clone — nothing to
# seed. strategy.yaml is per-user + gitignored (it reveals your macro book), so it is NOT
# in the clone: pull the REAL file from SSM if present (keeps the box reproducible from
# `terraform apply` AND keeps the book private); else seed the SAFE example. Re-runs NEVER
# clobber an existing file, so a manual edit on the box survives the next setup.sh.
seed_yaml() {  # $1=SSM param name  $2=dest path  $3=example path
  [ -f "$2" ] && return 0
  local v
  if v=$(aws ssm get-parameter --region "$AWS_REGION" --name "$SSM_PREFIX/$1" \
           --with-decryption --query Parameter.Value --output text 2>/dev/null) \
     && [ -n "$v" ] && [ "$v" != "None" ]; then
    printf '%s\n' "$v" > "$2"
    echo "  $(basename "$2") <- SSM $SSM_PREFIX/$1"
  else
    cp "$3" "$2"
    echo "  NOTE: $(basename "$2") seeded from $(basename "$3") (SSM $SSM_PREFIX/$1 absent) — replace before going live."
  fi
  chown "$QUIVER_USER:$QUIVER_USER" "$2"
}
seed_yaml STRATEGY_YAML "$QUIVER_HOME/strategy.yaml" "$QUIVER_HOME/strategy.yaml.example"

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

echo "[9/9] offline healthcheck (env sourced so load_config sees RH_ACCOUNT_NUMBER/NOTIFY_TO)"
# Non-fatal: a healthcheck hiccup must not abort provisioning after the timer is enabled.
sudo -u "$QUIVER_USER" bash -c 'set -a; . /etc/quiver/quiver.env; set +a; exec "'"$QUIVER_HOME"'/.venv/bin/python" "'"$QUIVER_HOME"'/deploy/runner/healthcheck.py"' \
  || echo "  NOTE: healthcheck reported issues (see above) — inspect before trusting a live tick"

cat <<'NOTE'
DONE. The only steps left are interactive, done ONCE in the on-box browser (Chrome
Remote Desktop) — see DEPLOY.md:
  1) Claude login: in the desktop, run `claude` and sign into your Pro/Max plan (unless
     a CLAUDE_CODE_OAUTH_TOKEN was provided via SSM).
  2) Robinhood auth: run `/mcp` -> robinhood-trading -> authenticate in the browser.
     Re-auth roughly every ~3.8 days (Robinhood's OAuth fully expires; no headless refresh).
Both persist under CLAUDE_CONFIG_DIR (/opt/quiver/state/claude-config), which the headless
tick reuses. config.yaml ships dry_run:false (LIVE) by design — straight-to-live per the
operator; touch /opt/quiver/KILL to halt.
NOTE
