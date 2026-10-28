#!/usr/bin/env bash
# Quiver Desk — GCP (Compute Engine) provisioning, Ubuntu 24.04 x86_64.
# Idempotent. Run as root by the instance startup-script AFTER the PUBLIC repo is cloned
# to /opt/quiver. It reads secrets from GCP Secret Manager (via the instance SA's metadata
# token) and ships logs via the Google Cloud Ops Agent. It installs the four desk services
# (quiver-chat, quiver-worker, quiver-broker, quiver) and the two housekeeping services.
# Each unit that runs Claude sets CLAUDE_CONFIG_DIR itself. quiver.env never sets it.
set -euo pipefail

QUIVER_USER="${QUIVER_USER:-quiver}"
QUIVER_HOME="${QUIVER_HOME:-/opt/quiver}"
SECRET_PREFIX="${SECRET_PREFIX:-quiver-}"
PROJECT="$(curl -s -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/project/project-id)"

echo "[1/9] apt deps"
apt-get update -y
apt-get install -y curl ca-certificates gnupg jq git python3-venv build-essential unzip sqlite3

echo "[1b/9] 4G swapfile backstop (a memory peak swaps, not OOM-kills)"
# The box has 4 GiB of RAM (e2-medium). The resident desk session, an analyst run, and a broker
# run can overlap. The swapfile absorbs a peak above that, for example the nightly npm update.
if ! swapon --show 2>/dev/null | grep -q .; then
  { fallocate -l 4G /swapfile 2>/dev/null || dd if=/dev/zero of=/swapfile bs=1M count=4096; } \
    && chmod 600 /swapfile && mkswap /swapfile >/dev/null && swapon /swapfile \
    && { grep -q '^/swapfile ' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab; } \
    || echo "  NOTE: swap setup skipped/failed (non-fatal)"
fi

echo "[2/9] Node 24 (the claude CLI runtime)"
# Idempotent: ensure Node 24 is present. The claude CLI installs from npm and runs on Node 24.
# We do NOT gate on `command -v node` (that would keep an old Node 20 on an already-provisioned box).
_ensure_node24() {
  if command -v node >/dev/null && [ "$(node -p 'process.versions.node.split(".")[0]')" -ge 24 ] 2>/dev/null; then return 0; fi
  curl -fsSL https://deb.nodesource.com/setup_24.x | bash -
  apt-get install -y nodejs
}
_ensure_node24

echo "[3/9] claude CLI"
# Root-global install. This is only the FALLBACK on PATH — the service runs the
# quiver-owned copy installed at [5/9] and pinned via CLAUDE_BIN. Both are kept
# current by quiver-claude-update.timer, because a STALE fallback is invisible:
# on 2026-08-13 the service silently ran /usr/bin/claude 2.1.177 while the
# operator's shell ran a newer build, so updating by hand fixed nothing.
npm i -g @anthropic-ai/claude-code

echo "[4/9] (no cloud CLI on the box — secrets read via the metadata token + Secret Manager REST)"

echo "[5/9] service user + venv (repo already cloned by the startup-script; public, no key)"
id -u "$QUIVER_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$QUIVER_USER"
mkdir -p "$QUIVER_HOME" /etc/quiver /var/log/quiver
python3 -m venv "$QUIVER_HOME/.venv"
# Install the FULL locked env (parity with the validated local venv), THEN the package itself.
# `pip install -e .` alone resolves only pyproject `dependencies` and MISSES anything imported
# but not declared there (e.g. pandas-market-calendars, which lib/market needs for preflight) —
# that gap bricked preflight on the first box. The lock file is the source of truth for the env.
"$QUIVER_HOME/.venv/bin/pip" install -q -r "$QUIVER_HOME/requirements.lock.txt"
"$QUIVER_HOME/.venv/bin/pip" install -q -e "$QUIVER_HOME" --no-deps
chown -R "$QUIVER_USER:$QUIVER_USER" "$QUIVER_HOME" /var/log/quiver

# The SERVICE's own claude CLI, in a quiver-OWNED npm prefix (CLAUDE_BIN).
# quiver-claude-update (root) keeps it current. The services run with
# ProtectSystem=strict and DISABLE_AUTOUPDATER=1, so they never write it.
# Keeping this under /opt also dodges ProtectHome=read-only, which blocks ~/.local.
sudo -u "$QUIVER_USER" env HOME="$(getent passwd "$QUIVER_USER" | cut -d: -f6)" \
  npm_config_cache="$QUIVER_HOME/state/.npm-cache" \
  npm install -g --prefix "$QUIVER_HOME/npm-global" @anthropic-ai/claude-code@latest

echo "[6/9] secrets: Secret Manager -> /etc/quiver/quiver.env (instance SA via metadata token)"
_sm_token() { curl -s -H "Metadata-Flavor: Google" "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token" | jq -r .access_token; }
# Required: capture into top-level vars FIRST so `set -e` aborts on a missing secret.
fetch() {  # $1 = secret id (sans prefix); prints decoded value; non-zero on absent/error.
  # RETRIES transient 403/429/5xx (Secret Manager IAM grants are eventually-consistent — a
  # fresh secretAccessor binding can lag tens of seconds after terraform applies, and the box
  # reads secrets the instant it boots). 404/400 (truly absent) fail FAST so fetch_opt on an
  # optional/ungranted secret doesn't burn the retry budget.
  local resp code data attempt=0
  while :; do
    resp=$(curl -s -w '\n%{http_code}' -H "Authorization: Bearer $(_sm_token)" \
      "https://secretmanager.googleapis.com/v1/projects/$PROJECT/secrets/$SECRET_PREFIX$1/versions/latest:access")
    code="${resp##*$'\n'}"
    if [ "$code" = "200" ]; then
      data=$(printf '%s' "${resp%$'\n'*}" | jq -r '.payload.data')
      { [ -n "$data" ] && [ "$data" != "null" ]; } || return 1   # guard before base64 -d
      printf '%s' "$data" | base64 -d
      return 0
    fi
    case "$code" in 404 | 400) return 1 ;; esac                  # absent → no retry
    attempt=$((attempt + 1)); [ "$attempt" -ge 6 ] && return 1   # ~60s of propagation grace
    sleep 10
  done
}
fetch_opt() { fetch "$1" 2>/dev/null || echo ""; }

# Required keys (plan §15.3). A missing or empty one stops the provision.
REQUIRED_KEYS="RH_ACCOUNT_NUMBER CLAUDE_CODE_OAUTH_TOKEN TELEGRAM_BOT_TOKEN TELEGRAM_ALLOWED_CHAT_IDS SEC_USER_AGENT"
# Optional keys. An absent one is written empty.
OPTIONAL_KEYS="ALPHA_VANTAGE_API_KEY TELEGRAM_ALERT_CHAT_IDS"
for _k in $REQUIRED_KEYS; do
  _v=$(fetch "$_k") || { echo "FATAL: required secret $SECRET_PREFIX$_k missing/inaccessible" >&2; exit 1; }
  { [ -n "$_v" ] && [ "$_v" != "null" ]; } || { echo "FATAL: required secret $SECRET_PREFIX$_k empty" >&2; exit 1; }
  printf -v "$_k" '%s' "$_v"
done
for _k in $OPTIONAL_KEYS; do
  printf -v "$_k" '%s' "$(fetch_opt "$_k")"
done
CLAUDE_BIN="$QUIVER_HOME/npm-global/bin/claude"
# Each line is KEY="value". systemd and bash both read it, and SEC_USER_AGENT has a space.
# A double quote, a backslash, a dollar sign or a backtick would break that form, so stop.
for _k in $REQUIRED_KEYS $OPTIONAL_KEYS CLAUDE_BIN; do
  case "${!_k}" in
    *[\"\\\$\`]*) echo "FATAL: secret $SECRET_PREFIX$_k holds a quote, backslash, \$ or backtick" >&2; exit 1 ;;
  esac
done

# Directories, owner quiver. Create state/ ITSELF as quiver FIRST: `install -d a/b` gives the
# intermediate `a` default (root) ownership and only chowns the named leaf.
#   state/claude-config  the Robinhood MCP OAuth (quiver-broker only)
#   state/claude-desk    the desk + analyst session transcripts; no MCP server is registered
#   state/triggers       the path-unit trigger files (worker, broker)
# Every unit lists these paths. A missing one stops the unit, so create each here.
install -d -m 755 -o "$QUIVER_USER" -g "$QUIVER_USER" "$QUIVER_HOME/state"
install -d -m 755 -o "$QUIVER_USER" -g "$QUIVER_USER" "$QUIVER_HOME/logs"
for _d in state/claude-config state/claude-desk state/triggers state/tmp state/.cache state/.npm-cache logs/desk; do
  install -d -m 700 -o "$QUIVER_USER" -g "$QUIVER_USER" "$QUIVER_HOME/$_d"
done
CLAUDE_CONFIG_DIR="$QUIVER_HOME/state/claude-config"   # for `claude mcp add` below only

# The env file. It must NOT set CLAUDE_CONFIG_DIR: EnvironmentFile= overrides Environment=,
# so a value here would move the desk units onto the Robinhood OAuth dir.
{
  for _k in $REQUIRED_KEYS $OPTIONAL_KEYS; do printf '%s="%s"\n' "$_k" "${!_k}"; done
  # PIN the binary. A bare "claude" resolves on systemd's PATH, never ~/.local/bin. That split
  # the box in two on 2026-08-13: the service ran an old /usr/bin/claude while the operator
  # updated and re-authed a different install. An absolute path removes the ambiguity.
  printf 'CLAUDE_BIN="%s"\n' "$CLAUDE_BIN"
} > /etc/quiver/quiver.env
chmod 600 /etc/quiver/quiver.env
chown "$QUIVER_USER:$QUIVER_USER" /etc/quiver/quiver.env

# Register the Robinhood MCP at USER scope in state/claude-config only (no header -> OAuth via
# deploy/gcp/rh-reauth.sh). run_broker.py passes no --mcp-config, so the broker run loads this
# server and its stored OAuth. state/claude-desk never gets a Robinhood server.
_QHOME="$(getent passwd "$QUIVER_USER" | cut -d: -f6)"
if sudo -u "$QUIVER_USER" env HOME="$_QHOME" CLAUDE_CONFIG_DIR="$CLAUDE_CONFIG_DIR" \
     claude mcp add robinhood-trading --scope user --transport http \
     https://agent.robinhood.com/mcp/trading >/dev/null 2>&1; then
  echo "  registered robinhood-trading (user scope) — authenticate it with deploy/gcp/rh-reauth.sh"
else
  echo "  NOTE: 'claude mcp add robinhood-trading' skipped/failed — add it over IAP SSH"
fi

# strategy.yaml (the seed book) is per-user + gitignored, so NOT in the public clone:
# materialize it from Secret Manager (or seed the SAFE example). `tick.py desk-seed` reads it once.
seed_yaml() {  # $1=secret id  $2=dest  $3=example
  [ -f "$2" ] && return 0
  local v; v=$(fetch_opt "$1")
  if [ -n "$v" ]; then printf '%s\n' "$v" > "$2"; echo "  $(basename "$2") <- Secret Manager $SECRET_PREFIX$1";
  else cp "$3" "$2"; echo "  NOTE: $(basename "$2") seeded from example ($SECRET_PREFIX$1 absent) — replace before live."; fi
  chown "$QUIVER_USER:$QUIVER_USER" "$2"
}
seed_yaml STRATEGY_YAML "$QUIVER_HOME/strategy.yaml" "$QUIVER_HOME/strategy.yaml.example"

# STRATEGY.md (the human-readable narrative) is per-box + gitignored like strategy.yaml, but it
# is NOT secret. `tick.py desk-seed` reads it once for premise v1. Seed it from the committed
# example if absent.
[ -f "$QUIVER_HOME/STRATEGY.md" ] || {
  cp "$QUIVER_HOME/STRATEGY.md.example" "$QUIVER_HOME/STRATEGY.md"
  chown "$QUIVER_USER:$QUIVER_USER" "$QUIVER_HOME/STRATEGY.md"
  echo "  STRATEGY.md <- seeded from example"
}

echo "[7/9] systemd units"
UNITS="quiver.service quiver.timer quiver-chat.service quiver-worker.path quiver-worker.service
quiver-broker.path quiver-broker.service quiver-claude-update.service quiver-claude-update.timer
quiver-backup.service quiver-backup.timer"
for u in $UNITS; do cp "$QUIVER_HOME/deploy/$u" "/etc/systemd/system/$u"; done
install -m 0755 -o root -g root "$QUIVER_HOME/deploy/claude-update.sh" /usr/local/bin/quiver-claude-update
touch /var/log/quiver/claude-update.log
chown "$QUIVER_USER:$QUIVER_USER" /var/log/quiver/claude-update.log
# Cap the journal. The default cap is 10% of the disk (~1.9G here). A full
# root disk hangs a run with no alert (2026-09-24).
mkdir -p /etc/systemd/journald.conf.d
printf '[Journal]\nSystemMaxUse=300M\n' >/etc/systemd/journald.conf.d/quiver.conf
systemctl restart systemd-journald
systemctl daemon-reload
# The nightly ledger backup needs objectCreator + objectViewer on the bucket for the box
# service account (see docs/DEPLOY.md). The chat bridge is not opt-in: [6/9] stops without
# the Telegram secrets.
systemctl enable --now quiver.timer quiver-worker.path quiver-broker.path \
  quiver-claude-update.timer quiver-backup.timer quiver-chat.service

echo "[8/9] Google Cloud Ops Agent — ship /var/log/quiver/tick.log to the 'quiver_tick' log"
if [ ! -d /etc/google-cloud-ops-agent ]; then
  curl -sSO https://dl.google.com/cloudagents/add-google-cloud-ops-agent-repo.sh
  bash add-google-cloud-ops-agent-repo.sh --also-install || echo "  NOTE: Ops Agent install hiccup"
  rm -f add-google-cloud-ops-agent-repo.sh
fi
touch /var/log/quiver/tick.log
chown "$QUIVER_USER:$QUIVER_USER" /var/log/quiver/tick.log
cat > /etc/google-cloud-ops-agent/config.yaml <<'OPSCFG'
logging:
  receivers:
    quiver_tick:
      type: files
      include_paths: [/var/log/quiver/tick.log]
  service:
    pipelines:
      quiver:
        receivers: [quiver_tick]
OPSCFG
systemctl restart google-cloud-ops-agent 2>/dev/null || echo "  NOTE: ops-agent restart skipped"

echo "[9/9] offline healthcheck (env sourced so load_config sees RH_ACCOUNT_NUMBER)"
sudo -u "$QUIVER_USER" bash -c 'set -a; . /etc/quiver/quiver.env; set +a; exec "'"$QUIVER_HOME"'/.venv/bin/python" "'"$QUIVER_HOME"'/deploy/runner/healthcheck.py"' \
  || echo "  NOTE: healthcheck reported issues (see above) — inspect before trusting a live run"

cat <<'NOTE'
DONE (Quiver Desk). Two steps are left. Both run by hand — see docs/DEPLOY.md:
  1. ./deploy/gcp/rh-reauth.sh   (on your LAPTOP: log in to Robinhood; repeat when the
                                  AUTH_ERROR page arrives — the Robinhood OAuth has no
                                  headless refresh)
  2. sudo -u quiver bash -c 'cd /opt/quiver && set -a; . /etc/quiver/quiver.env; set +a; .venv/bin/python tick.py desk-seed'
                                 (on the BOX: seed the premise, the book and the theses
                                  from strategy.yaml and STRATEGY.md, once)
The Robinhood login persists under /opt/quiver/state/claude-config, which only quiver-broker
reads. Every Claude process logs in with the CLAUDE_CODE_OAUTH_TOKEN secret. Touch
/opt/quiver/KILL to stop every execution.
NOTE
