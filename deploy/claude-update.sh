#!/usr/bin/env bash
# Keep every Claude Code install on the box current.
#
# The box has two installs. Both must stay on the same version, because a
# stale one is invisible until a tick runs the wrong binary:
#   1. /opt/quiver/npm-global/bin/claude  - the SERVICE binary (CLAUDE_BIN)
#   2. /usr/bin/claude                    - the legacy root npm global
#
# An update to the SERVICE binary is gated: the new build must authenticate
# before it is kept. A version failure rolls back. An auth failure does NOT
# roll back - an expired OAuth token is not the build's fault. A new build
# must also pass the broker CLI canary (run_broker.py --canary). A canary
# failure rolls back and pages (stage broker).
#
# The script never replaces the binary under a running job. It takes the
# deploy lock that update.sh holds, pauses the broker path unit, and skips
# the day while the worker or the broker runs.
set -uo pipefail

LOG=/var/log/quiver/claude-update.log
NPM_PREFIX=/opt/quiver/npm-global
SVC_BIN="$NPM_PREFIX/bin/claude"
CFG=/opt/quiver/state/claude-config
CACHE=/opt/quiver/state/.npm-cache
ENVF=/etc/quiver/quiver.env

log() { echo "$(date -Is) $*" >>"$LOG"; }

as_quiver() { sudo -u quiver env HOME=/home/quiver CLAUDE_CONFIG_DIR="$CFG" \
                npm_config_cache="$CACHE" "$@"; }

# The gate must use the login the service uses. The service reads a subscription
# token from quiver.env. A gate without it tests the stored login instead. An
# expired stored login then reads as an auth problem and hides a broken build.
# sudo drops the caller's environment, so --preserve-env names the token. The
# token is never put on a command line.
# setup.sh writes each value as KEY="value". Remove the quotes: the gate must
# send the bare token.
TOKEN=$(sed -n 's/^CLAUDE_CODE_OAUTH_TOKEN=//p' "$ENVF" 2>/dev/null | tail -n 1 \
          | sed -e 's/^"\(.*\)"$/\1/')
as_quiver_auth() {
  if [ -n "$TOKEN" ]; then
    CLAUDE_CODE_OAUTH_TOKEN="$TOKEN" sudo --preserve-env=CLAUDE_CODE_OAUTH_TOKEN -u quiver \
      env HOME=/home/quiver CLAUDE_CONFIG_DIR="$CFG" npm_config_cache="$CACHE" "$@"
  else
    as_quiver "$@"
  fi
}

ver() { "$1" --version 2>/dev/null | awk '{print $1}'; }

# Clear both npm caches on EVERY exit, including the rollback exits below.
# npm never evicts old tarballs, so each release adds ~200M across the two
# caches. On 2026-09-24 they filled the 19G root disk and a tick hung with
# no alert. The installs do not read the cache again after they finish.
clean_caches() {
  as_quiver npm cache clean --force >>"$LOG" 2>&1 || log "service npm cache clean FAILED"
  npm cache clean --force >>"$LOG" 2>&1 || log "root npm cache clean FAILED"
  log "npm caches cleaned; root disk free: $(df -h / | awk 'NR==2{print $4}')"
}

# One Telegram page (stage broker). Best-effort: a page failure only logs.
# The page runs as quiver with quiver.env, the same way the runners page.
PAGE_PY='import sys
sys.path.insert(0, "/opt/quiver")
from lib import telegram
token, chats = telegram.resolve_env()
if token and chats:
    telegram.send_message(token=token, chat_ids=chats, text=sys.argv[1])'
page() {  # $1 = the page text
  runuser -u quiver -- bash -c 'set -a; . /etc/quiver/quiver.env; set +a; exec /opt/quiver/.venv/bin/python -c "$1" "$2"' \
    _ "$PAGE_PY" "$1" >>"$LOG" 2>&1 || log "page FAILED (stage broker)"
}

log "=== claude-update start ==="

# update.sh holds this lock for its whole run. Never swap the binary under it.
exec 9>/run/quiver-deploy.lock; flock -n 9 || { log "skipped: update.sh holds the deploy lock"; exit 0; }
WAS=$(systemctl is-active quiver-broker.path)
WASW=$(systemctl is-active quiver-worker.path)
# No broker or worker run during the binary swap. A trigger file written meanwhile fires
# when the path unit starts again, so no job is lost.
systemctl stop quiver-broker.path quiver-worker.path >>"$LOG" 2>&1
# The one EXIT trap. It also runs on the rollback exits below. Bash keeps one
# EXIT trap, so clean_caches and the two path restarts share it.
trap 'clean_caches; [ "$WAS" = active ] && systemctl start quiver-broker.path; [ "$WASW" = active ] && systemctl start quiver-worker.path' EXIT
for u in quiver-broker.service quiver-worker.service; do
  systemctl is-active --quiet "$u" && { log "skipped: $u is active"; exit 0; }
done

BEFORE=$(ver "$SVC_BIN")
log "service binary before: ${BEFORE:-none}"

# 1. SERVICE binary - the one that trades. Updated first so the gate below
#    covers it.
if as_quiver npm install -g --prefix "$NPM_PREFIX" \
     @anthropic-ai/claude-code@latest >>"$LOG" 2>&1; then
  log "service npm install ok"
else
  log "service npm install FAILED"
fi

AFTER=$(ver "$SVC_BIN")
log "service binary after: ${AFTER:-none}"

# 2. GATE the service binary before touching anything else. A broken build
#    here stops the whole trading loop, so prove it runs.
OUT=$(as_quiver_auth timeout 150 "$SVC_BIN" -p "reply with exactly: AUTH_OK" 2>&1 </dev/null | tail -3)
if grep -q "AUTH_OK" <<<"$OUT"; then
  log "verify OK ($AFTER)"
  # A new build must also pass the broker CLI canary: the hooks of
  # deploy/runner/settings.json load and block. The canary gets the broker
  # unit's environment. --bin takes the binary path, not the version.
  if [ -n "$AFTER" ] && [ "$AFTER" != "$BEFORE" ]; then
    if runuser -u quiver -- env HOME=/home/quiver CLAUDE_CONFIG_DIR="$CFG" DISABLE_AUTOUPDATER=1 \
         QUIVER_TICK_LOG=/var/log/quiver/tick.log \
         bash -c 'set -a; . /etc/quiver/quiver.env; set +a; exec /opt/quiver/.venv/bin/python /opt/quiver/deploy/runner/run_broker.py --canary --bin "$1"' \
         _ "$SVC_BIN" >>"$LOG" 2>&1; then
      log "canary OK ($AFTER)"
    else
      log "canary FAILED on $AFTER"
      if [ -n "$BEFORE" ] && as_quiver npm install -g --prefix "$NPM_PREFIX" \
           @anthropic-ai/claude-code@"$BEFORE" >>"$LOG" 2>&1; then
        log "canary failed: kept $BEFORE"
        page "Quiver: the broker CLI canary failed on Claude Code $AFTER. The update put $BEFORE back. Stage: broker."
      else
        log "canary failed and ROLLBACK FAILED - service binary may be broken"
        page "Quiver: the broker CLI canary failed on Claude Code $AFTER, and no rollback to ${BEFORE:-none} worked. Stage: broker."
      fi
      exit 1
    fi
  fi
elif grep -qiE "oauth|authenticat|401|expired|spend limit|usage limit" <<<"$OUT"; then
  # Not a bad build - the operator must re-auth. Keep the new version.
  log "verify INCONCLUSIVE - auth problem, NOT rolling back: $OUT"
elif [ -n "$BEFORE" ] && [ "$AFTER" != "$BEFORE" ]; then
  log "verify FAILED on $AFTER: $OUT"
  if as_quiver npm install -g --prefix "$NPM_PREFIX" \
       @anthropic-ai/claude-code@"$BEFORE" >>"$LOG" 2>&1; then
    log "ROLLED BACK to $BEFORE"
  else
    log "ROLLBACK FAILED - service binary may be broken"
  fi
  exit 1
else
  log "verify FAILED and no rollback target: $OUT"
  exit 1
fi

# 3. Legacy root npm global. Kept current only so a fallback PATH resolution
#    can never land on a stale build.
if npm install -g @anthropic-ai/claude-code@latest >>"$LOG" 2>&1; then
  log "root npm install ok -> $(ver /usr/bin/claude)"
else
  log "root npm install FAILED"
fi

# Move the resident desk session onto the new binary. try-restart does
# nothing when the chat bridge is stopped.
if [ "$AFTER" != "$BEFORE" ]; then
  systemctl try-restart quiver-chat.service && log "chat bridge restarted onto $AFTER"
fi

log "=== claude-update done: svc=$(ver "$SVC_BIN") root=$(ver /usr/bin/claude) ==="
