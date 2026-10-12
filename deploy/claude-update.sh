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
# roll back - an expired OAuth token is not the build's fault.
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
TOKEN=$(sed -n 's/^CLAUDE_CODE_OAUTH_TOKEN=//p' "$ENVF" 2>/dev/null | tail -n 1)
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
trap clean_caches EXIT

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
OUT=$(as_quiver_auth timeout 150 "$SVC_BIN" -p "reply with exactly: AUTH_OK" 2>&1 </dev/null | tail -3)
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

# 3. Legacy root npm global. Kept current only so a fallback PATH resolution
#    can never land on a stale build.
if npm install -g @anthropic-ai/claude-code@latest >>"$LOG" 2>&1; then
  log "root npm install ok -> $(ver /usr/bin/claude)"
else
  log "root npm install FAILED"
fi

log "=== claude-update done: svc=$(ver "$SVC_BIN") root=$(ver /usr/bin/claude) ==="
