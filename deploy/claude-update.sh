#!/usr/bin/env bash
# Keep every Claude Code install on the box current.
#
# The box has three installs. All must stay on the same version, because a
# stale one is invisible until a tick runs the wrong binary:
#   1. /opt/quiver/npm-global/bin/claude  - the SERVICE binary (CLAUDE_BIN)
#   2. /home/quiver/.local/bin/claude     - the interactive CRD shell binary
#   3. /usr/bin/claude                    - the legacy root npm global
#
# An update to the SERVICE binary is gated: the new build must authenticate
# before it is kept. A version failure rolls back. An auth failure does NOT
# roll back - an expired OAuth token is not the build's fault.
set -uo pipefail

LOG=/var/log/quiver/claude-update.log
NPM_PREFIX=/opt/quiver/npm-global
SVC_BIN="$NPM_PREFIX/bin/claude"
NATIVE_BIN=/home/quiver/.local/bin/claude
CFG=/opt/quiver/state/claude-config
CACHE=/opt/quiver/state/.npm-cache

log() { echo "$(date -Is) $*" >>"$LOG"; }

as_quiver() { sudo -u quiver env HOME=/home/quiver CLAUDE_CONFIG_DIR="$CFG" \
                npm_config_cache="$CACHE" "$@"; }

ver() { "$1" --version 2>/dev/null | awk '{print $1}'; }

log "=== claude-update start ==="
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
OUT=$(as_quiver timeout 150 "$SVC_BIN" -p "reply with exactly: AUTH_OK" 2>&1 | tail -3)
if grep -q "AUTH_OK" <<<"$OUT"; then
  log "verify OK ($AFTER)"
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

# 3. Interactive native install (CRD shell). Tracks the same 'latest' target
#    so the human debugs the same build the bot runs.
if as_quiver "$NATIVE_BIN" install latest >>"$LOG" 2>&1; then
  log "native install ok -> $(sudo -u quiver "$NATIVE_BIN" --version 2>/dev/null)"
else
  log "native install FAILED"
fi

# 4. Legacy root npm global. Kept current only so a fallback PATH resolution
#    can never land on a stale build.
if npm install -g @anthropic-ai/claude-code@latest >>"$LOG" 2>&1; then
  log "root npm install ok -> $(ver /usr/bin/claude)"
else
  log "root npm install FAILED"
fi

log "=== claude-update done: svc=$(ver "$SVC_BIN") native=$(sudo -u quiver "$NATIVE_BIN" --version 2>/dev/null | awk '{print $1}') root=$(ver /usr/bin/claude) ==="
