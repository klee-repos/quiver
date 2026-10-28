#!/usr/bin/env bash
# Quiver — re-authenticate the Robinhood MCP on the box from your laptop. No desktop.
#
#   ./deploy/gcp/rh-reauth.sh            # log in, then print the server status
#   ./deploy/gcp/rh-reauth.sh --status   # print the server status only
#
# Robinhood's MCP token expires about every 3.8 days and has no headless refresh, so a human
# logs in again. The login needs a browser, but NOT a browser on the box.
# `claude mcp login --no-browser` (Claude Code >= 2.1.186) prints the authorization URL and
# reads the redirect URL back from the terminal. The OAuth state lands in CLAUDE_CONFIG_DIR,
# the same store the headless tick reads.
#
# The login runs as `quiver`, with the binary and the config dir the service uses. Both come
# from /etc/quiver/quiver.env. This prevents a login against a different install than the one
# that trades (the 2026-08-13 split).
set -euo pipefail

ZONE="${QUIVER_ZONE:-us-central1-a}"
INSTANCE="${QUIVER_INSTANCE:-quiver}"

MODE=login
case "${1:-}" in
  "") ;;
  --status) MODE=status ;;
  *) echo "usage: $0 [--status]" >&2; exit 2 ;;
esac

# Runs on the box. POSIX sh only: the remote login shell runs it, and that is not always bash.
# `read -d ''`, not `$(cat <<EOS)`: bash 3.2 (the macOS /bin/bash) cannot parse a `case`
# pattern inside `$( )`. `read` returns non-zero at end of input, so `|| true`.
read -r -d '' REMOTE <<'EOS' || true
set -eu
ENVF=/etc/quiver/quiver.env
# quiver.env may quote a value (setup.sh writes CLAUDE_BIN="..."); strip one pair of quotes.
val() { sudo sed -n "s/^$1=//p" "$ENVF" | tail -n 1 | sed -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/"; }
BIN="$(val CLAUDE_BIN)"; BIN="${BIN:-/opt/quiver/npm-global/bin/claude}"
CFG="$(val CLAUDE_CONFIG_DIR)"; CFG="${CFG:-/opt/quiver/state/claude-config}"
cd /opt/quiver
as_quiver() { sudo -H -u quiver env CLAUDE_CONFIG_DIR="$CFG" "$BIN" "$@"; }
echo "binary: $BIN ($(as_quiver --version 2>/dev/null || echo 'version unknown'))"
if [ "$MODE" = login ]; then
  as_quiver mcp login robinhood-trading --no-browser || echo "login did not complete"
fi
OUT="$(as_quiver mcp get robinhood-trading 2>&1 || true)"
printf '%s\n' "$OUT"
case "$OUT" in
  *Status:*Connected*) echo "OK: robinhood-trading is connected. The next tick can trade." ;;
  *) echo "NOT CONNECTED: robinhood-trading needs a login. Run this script again." >&2; exit 1 ;;
esac
EOS

if [ "$MODE" = login ]; then
  cat <<'EOM'
The box prints a Robinhood authorization URL.
  1. Open the URL in a browser on this machine. Log in to Robinhood and approve.
  2. The browser then shows a connection error for http://localhost:<port>/callback?...
     This is correct. Nothing listens on that port on your laptop.
  3. Copy the full address of that page. Paste it at the prompt in this terminal.

EOM
fi

# `-- -t` forces a terminal. `claude mcp login` refuses to run without one.
exec gcloud compute ssh "$INSTANCE" --zone "$ZONE" --tunnel-through-iap \
  --command="MODE=$MODE; $REMOTE" -- -t
