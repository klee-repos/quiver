#!/usr/bin/env bash
# Quiver headless trader — GCP (Compute Engine) provisioning, Ubuntu 24.04 x86_64.
# Idempotent. Run as root by the instance startup-script AFTER the PUBLIC repo is cloned
# to /opt/quiver. It reads secrets from GCP Secret Manager (via the instance SA's metadata
# token) and ships logs via the Google Cloud Ops Agent. The trading stack, the systemd
# units, and the CLAUDE_CONFIG_DIR / on-server-OAuth wiring are unchanged.
set -euo pipefail

QUIVER_USER="${QUIVER_USER:-quiver}"
QUIVER_HOME="${QUIVER_HOME:-/opt/quiver}"
SECRET_PREFIX="${SECRET_PREFIX:-quiver-}"
PROJECT="$(curl -s -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/project/project-id)"

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

echo "[2/9] Node 24 (claude CLI runtime + the EVE brain runtime; eve engines.node>=24)"
# Idempotent: ensure Node 24 is present (the EVE brain requires >=24; claude CLI runs on 24).
# We do NOT gate on `command -v node` (that would keep an old Node 20 on an already-provisioned box).
_ensure_node24() {
  if command -v node >/dev/null && [ "$(node -p 'process.versions.node.split(".")[0]')" -ge 24 ] 2>/dev/null; then return 0; fi
  curl -fsSL https://deb.nodesource.com/setup_24.x | bash -
  apt-get install -y nodejs
}
_ensure_node24

echo "[3/9] claude CLI + EVE brain deps"
# Root-global install. This is only the FALLBACK on PATH — the service runs the
# quiver-owned copy installed at [5/9] and pinned via CLAUDE_BIN. Both are kept
# current by quiver-claude-update.timer, because a STALE fallback is invisible:
# on 2026-08-13 the service silently ran /usr/bin/claude 2.1.177 while the
# operator's shell ran a newer build, so updating by hand fixed nothing.
npm i -g @anthropic-ai/claude-code
# The EVE brain (quiver_eve/) is committed; install its deps at provision time.
# node_modules/ is gitignored. npm ci = reproducible from the lockfile once it exists;
# npm install (no lock yet) is fine for the first provision.
if [ -f "$QUIVER_HOME/quiver_eve/package.json" ]; then
  ( cd "$QUIVER_HOME/quiver_eve" && { npm ci 2>/dev/null || npm install --omit=dev; } )
fi

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

# The SERVICE's own claude CLI, in a quiver-OWNED npm prefix.
# npm's default global prefix is /usr (root), and the service user has no sudo,
# so `npm -g` there can never self-update. /opt/quiver IS writable by the
# service: ProtectSystem=full locks only /usr, /boot and /efi. Keeping this
# under /opt also dodges ProtectHome=read-only, which blocks ~/.local.
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

# OPENROUTER_API_KEY is the LIVE brain key (the EVE brain reads it; there is no other).
# The legacy GLM/DEEPSEEK/RESEND keys below are ROLLBACK ONLY — kept required so a rollback
# path retains its creds, but a new box fails fast if the live key is missing.
for _k in OPENROUTER_API_KEY GLM_API_KEY DEEPSEEK_API_KEY RH_ACCOUNT_NUMBER RESEND_API_KEY NOTIFY_TO; do
  _v=$(fetch "$_k") || { echo "FATAL: required secret $SECRET_PREFIX$_k missing/inaccessible" >&2; exit 1; }
  { [ -n "$_v" ] && [ "$_v" != "null" ]; } || { echo "FATAL: required secret $SECRET_PREFIX$_k empty" >&2; exit 1; }
  printf -v "$_k" '%s' "$_v"
done

# Claude auth is OPTIONAL (on-box `claude login` in the desktop is the fallback). Write one.
_CCOT=$(fetch_opt CLAUDE_CODE_OAUTH_TOKEN)
_AAK=$(fetch_opt ANTHROPIC_API_KEY)
CLAUDE_AUTH_LINE=""
if [ -n "$_CCOT" ]; then CLAUDE_AUTH_LINE="CLAUDE_CODE_OAUTH_TOKEN=$_CCOT";
elif [ -n "$_AAK" ]; then CLAUDE_AUTH_LINE="ANTHROPIC_API_KEY=$_AAK";
else echo "  NOTE: no Claude auth secret — relying on an on-box 'claude login' in the desktop."; fi

# All claude + MCP credential/OAuth state under /opt/quiver/state (writable; survives the
# service's ProtectHome=read-only). Same dir the desktop OAuth writes and the tick reads.
CLAUDE_CONFIG_DIR="$QUIVER_HOME/state/claude-config"
# Create state/ ITSELF as quiver FIRST: `install -d a/b` gives the intermediate `a` default
# (root) ownership and only chowns the named leaf — which would leave /opt/quiver/state
# root-owned and make the ledger (state/ledger.db) unwritable by the quiver tick.
install -d -m 755 -o "$QUIVER_USER" -g "$QUIVER_USER" "$QUIVER_HOME/state"
install -d -m 700 -o "$QUIVER_USER" -g "$QUIVER_USER" "$CLAUDE_CONFIG_DIR"
install -d -m 700 -o "$QUIVER_USER" -g "$QUIVER_USER" "$QUIVER_HOME/state/.npm-cache"
# The read-only chat bridge writes its log here (its own dir — NOT the tick's /var/log/quiver).
install -d -m 700 -o "$QUIVER_USER" -g "$QUIVER_USER" "$QUIVER_HOME/state/chat"
{
  [ -n "$CLAUDE_AUTH_LINE" ] && echo "$CLAUDE_AUTH_LINE"
  echo "CLAUDE_CONFIG_DIR=$CLAUDE_CONFIG_DIR"
  echo "OPENROUTER_API_KEY=$OPENROUTER_API_KEY"
  echo "GLM_API_KEY=$GLM_API_KEY"
  echo "DEEPSEEK_API_KEY=$DEEPSEEK_API_KEY"
  echo "RH_ACCOUNT_NUMBER=$RH_ACCOUNT_NUMBER"
  echo "RESEND_API_KEY=$RESEND_API_KEY"
  echo "NOTIFY_TO=$NOTIFY_TO"
  echo "RESEND_FROM=$(fetch_opt RESEND_FROM)"
  echo "NOTIFY_ALERTS_TO=$(fetch_opt NOTIFY_ALERTS_TO)"
  # Telegram: the SAME token/chat id power BOTH the read-only chat bridge
  # (deploy/quiver-chat.service) AND — now — ALL of the tick's alerting (digest + halt/auth/error
  # via tick.py report-send + run_tick.py's last-resort sender; there is no more email/Resend MCP).
  # Both empty => the bridge stays idle AND alerts are best-effort-skipped (`unconfigured`). The two
  # secrets must have a box-SA secretAccessor IAM grant (terraform/main.tf excludes them by default;
  # grant via `gcloud secrets add-iam-policy-binding` — see main.tf comment). See docs/CHAT.md +
  # docs/DEPLOY.md; validate on the box with `tick.py send-test`.
  echo "TELEGRAM_BOT_TOKEN=$(fetch_opt TELEGRAM_BOT_TOKEN)"
  echo "TELEGRAM_ALLOWED_CHAT_IDS=$(fetch_opt TELEGRAM_ALLOWED_CHAT_IDS)"
  echo "TELEGRAM_ALERT_CHAT_IDS=$(fetch_opt TELEGRAM_ALERT_CHAT_IDS)"  # optional alert-only override
  # PIN the orchestrator binary. run_tick.py defaults CLAUDE_BIN to a BARE
  # "claude", which systemd resolves on ITS path — never ~/.local/bin. That split
  # the box in two on 2026-08-13: the service ran an old /usr/bin/claude while the
  # operator updated and re-authed a different install, so the fix never landed.
  # An absolute path removes the ambiguity.
  echo "CLAUDE_BIN=$QUIVER_HOME/npm-global/bin/claude"
} > /etc/quiver/quiver.env
chmod 600 /etc/quiver/quiver.env
chown "$QUIVER_USER:$QUIVER_USER" /etc/quiver/quiver.env
[ -f "$QUIVER_HOME/deploy/runner/mcp.json" ] || cp "$QUIVER_HOME/deploy/runner/mcp.json.example" "$QUIVER_HOME/deploy/runner/mcp.json"

# Register the Robinhood MCP at USER scope (no header -> OAuth done in the on-box browser).
# run_tick.py drops --strict-mcp-config so the headless tick reuses this + its stored OAuth.
_QHOME="$(getent passwd "$QUIVER_USER" | cut -d: -f6)"
if sudo -u "$QUIVER_USER" env HOME="$_QHOME" CLAUDE_CONFIG_DIR="$CLAUDE_CONFIG_DIR" \
     claude mcp add robinhood-trading --scope user --transport http \
     https://agent.robinhood.com/mcp/trading >/dev/null 2>&1; then
  echo "  registered robinhood-trading (user scope) — authenticate it via /mcp in the desktop"
else
  echo "  NOTE: 'claude mcp add robinhood-trading' skipped/failed — add it via /mcp on the box"
fi

# strategy.yaml (the trading universe) is per-user + gitignored, so NOT in the public clone:
# materialize it from Secret Manager (or seed the SAFE example -> nothing to trade).
seed_yaml() {  # $1=secret id  $2=dest  $3=example
  [ -f "$2" ] && return 0
  local v; v=$(fetch_opt "$1")
  if [ -n "$v" ]; then printf '%s\n' "$v" > "$2"; echo "  $(basename "$2") <- Secret Manager $SECRET_PREFIX$1";
  else cp "$3" "$2"; echo "  NOTE: $(basename "$2") seeded from example ($SECRET_PREFIX$1 absent) — replace before live."; fi
  chown "$QUIVER_USER:$QUIVER_USER" "$2"
}
seed_yaml STRATEGY_YAML "$QUIVER_HOME/strategy.yaml" "$QUIVER_HOME/strategy.yaml.example"

# STRATEGY.md (the human-readable narrative the agent appends learnings to) is per-box +
# gitignored like strategy.yaml, but it is NOT secret — seed it from the committed example if
# absent so a fresh box starts with the "## 7. Agent learnings" section the append writer needs.
[ -f "$QUIVER_HOME/STRATEGY.md" ] || {
  cp "$QUIVER_HOME/STRATEGY.md.example" "$QUIVER_HOME/STRATEGY.md"
  chown "$QUIVER_USER:$QUIVER_USER" "$QUIVER_HOME/STRATEGY.md"
  echo "  STRATEGY.md <- seeded from example"
}

echo "[7/9] systemd units + timer"
cp "$QUIVER_HOME/deploy/quiver.service" /etc/systemd/system/quiver.service
cp "$QUIVER_HOME/deploy/quiver.timer" /etc/systemd/system/quiver.timer
cp "$QUIVER_HOME/deploy/quiver-intel.service" /etc/systemd/system/quiver-intel.service
cp "$QUIVER_HOME/deploy/quiver-intel.timer" /etc/systemd/system/quiver-intel.timer
cp "$QUIVER_HOME/deploy/quiver-chat.service" /etc/systemd/system/quiver-chat.service
cp "$QUIVER_HOME/deploy/quiver-claude-update.service" /etc/systemd/system/quiver-claude-update.service
cp "$QUIVER_HOME/deploy/quiver-claude-update.timer" /etc/systemd/system/quiver-claude-update.timer
install -m 0755 -o root -g root "$QUIVER_HOME/deploy/claude-update.sh" /usr/local/bin/quiver-claude-update
touch /var/log/quiver/claude-update.log
chown "$QUIVER_USER:$QUIVER_USER" /var/log/quiver/claude-update.log
systemctl daemon-reload
systemctl enable --now quiver.timer
systemctl enable --now quiver-intel.timer
systemctl enable --now quiver-claude-update.timer
# The read-only Telegram chat bridge is opt-in: only start it if a bot token is configured.
if grep -q '^TELEGRAM_BOT_TOKEN=.\+' /etc/quiver/quiver.env; then
  systemctl enable --now quiver-chat.service && echo "  chat bridge: enabled"
else
  echo "  chat bridge: idle (no TELEGRAM_BOT_TOKEN) — see docs/CHAT.md to enable"
fi

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

echo "[9/9] offline healthcheck (env sourced so load_config sees RH_ACCOUNT_NUMBER/NOTIFY_TO)"
sudo -u "$QUIVER_USER" bash -c 'set -a; . /etc/quiver/quiver.env; set +a; exec "'"$QUIVER_HOME"'/.venv/bin/python" "'"$QUIVER_HOME"'/deploy/runner/healthcheck.py"' \
  || echo "  NOTE: healthcheck reported issues (see above) — inspect before trusting a live tick"

cat <<'NOTE'
DONE (GCP trading stack). The only steps left are interactive, done ONCE in the on-box
browser (Chrome Remote Desktop) — see docs/DEPLOY.md:
  1) claude   (sign into your Pro/Max plan, unless a Claude token was set in Secret Manager)
  2) /mcp -> robinhood-trading -> authenticate. Re-auth ~every 3.8 days (Robinhood OAuth,
     no headless refresh).
Both persist under CLAUDE_CONFIG_DIR (/opt/quiver/state/claude-config), which the headless
tick reuses. config.yaml ships dry_run:false (LIVE) by design — straight-to-live per the
operator; touch /opt/quiver/KILL to halt.
NOTE
