#!/usr/bin/env bash
# Quiver box — desktop + Chrome Remote Desktop layer (x86_64 Ubuntu 24.04).
#
# This adds the ON-SERVER browser the operator uses for the periodic Robinhood + Claude
# OAuth (~every 3.8 days — Robinhood's token fully expires, no headless refresh). It runs
# AFTER deploy/gcp/setup.sh, which already stood up the trading stack. BEST-EFFORT BY DESIGN: a
# desktop/network hiccup here must never undo a working trading box, so we do NOT use
# `set -e` and every step is guarded. Re-runnable (idempotent-ish).
#
# Why x86_64 (not the cheaper arm64): Chrome Remote Desktop's Linux host and Google
# Chrome are amd64-only. The Terraform pins an x86_64 image + e2-medium for exactly this.
set -uo pipefail

QUIVER_USER="${QUIVER_USER:-quiver}"
QUIVER_HOME="${QUIVER_HOME:-/opt/quiver}"
CLAUDE_CONFIG_DIR="${CLAUDE_CONFIG_DIR:-$QUIVER_HOME/state/claude-config}"
QHOME="$(getent passwd "$QUIVER_USER" | cut -d: -f6)"
export DEBIAN_FRONTEND=noninteractive

echo "[desktop 1/6] give $QUIVER_USER a login shell (a desktop session needs one)"
usermod -s /bin/bash "$QUIVER_USER" || true

echo "[desktop 2/6] XFCE + session deps"
apt-get update -y || true
apt-get install -y xfce4 xfce4-terminal dbus-x11 x11-xserver-utils || \
  echo "  NOTE: XFCE install had issues — re-run this script over IAP SSH"

echo "[desktop 3/6] Google Chrome (amd64)"
if ! command -v google-chrome >/dev/null 2>&1; then
  if curl -fsSL https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb -o /tmp/chrome.deb; then
    apt-get install -y /tmp/chrome.deb || echo "  NOTE: Chrome install failed; install it later"
    rm -f /tmp/chrome.deb
  else
    echo "  NOTE: Chrome download failed; install it later"
  fi
fi

echo "[desktop 4/6] Chrome Remote Desktop host (amd64)"
if [ ! -x /opt/google/chrome-remote-desktop/start-host ]; then
  if curl -fsSL https://dl.google.com/linux/direct/chrome-remote-desktop_current_amd64.deb -o /tmp/crd.deb; then
    apt-get install -y /tmp/crd.deb || echo "  NOTE: CRD install failed; install it later"
    rm -f /tmp/crd.deb
  else
    echo "  NOTE: CRD download failed; install it later"
  fi
fi

echo "[desktop 5/6] CRD session -> XFCE for $QUIVER_USER"
# CRD execs this file to start the desktop the remote session displays.
printf 'exec /usr/bin/startxfce4\n' > "$QHOME/.chrome-remote-desktop-session"
chown "$QUIVER_USER:$QUIVER_USER" "$QHOME/.chrome-remote-desktop-session"
if getent group chrome-remote-desktop >/dev/null 2>&1; then
  usermod -aG chrome-remote-desktop "$QUIVER_USER" || true
fi

echo "[desktop 6/6] CLAUDE_CONFIG_DIR for the quiver desktop shells (NOT global /etc/environment)"
# The `claude login` / `/mcp` OAuth the operator does in the on-box browser MUST write to the
# SAME dir the headless systemd tick reads (the service gets it via quiver.env). A Chrome
# Remote Desktop terminal is an interactive shell that does NOT reliably inherit
# /etc/environment (CRD execs the session outside pam_env), so export it directly in the
# quiver user's shell rc files. Keeping it OUT of /etc/environment also avoids pointing a
# root debug shell at quiver's 700 credential dir.
for _rc in "$QHOME/.bashrc" "$QHOME/.profile"; do
  touch "$_rc"
  grep -q 'CLAUDE_CONFIG_DIR=' "$_rc" 2>/dev/null \
    || printf 'export CLAUDE_CONFIG_DIR=%s\n' "$CLAUDE_CONFIG_DIR" >> "$_rc"
  chown "$QUIVER_USER:$QUIVER_USER" "$_rc"
done

cat <<NOTE

DONE (desktop layer). One HUMAN step remains to link Chrome Remote Desktop — it is tied to
YOUR Google account, so only you can mint the code:
  1) Open https://remotedesktop.google.com/headless -> "Set up another computer" -> Debian
     Linux, and copy the 'Authorize' (start-host) command it shows. It carries a one-time
     --code="4/..." tied to your Google login.
  2) Run it ON THIS BOX as $QUIVER_USER (paste the code), then set a 6-digit PIN when asked:
       sudo -u $QUIVER_USER env HOME=$QHOME CLAUDE_CONFIG_DIR=$CLAUDE_CONFIG_DIR \\
         /opt/google/chrome-remote-desktop/start-host \\
         --code="PASTE_4/..." \\
         --redirect-url="https://remotedesktop.google.com/_/oauthredirect" \\
         --name=quiver-box
  3) The box then shows at https://remotedesktop.google.com/access — connect with the PIN,
     open Chrome, launch a terminal, and run:  claude   (sign in), then  /mcp  (Robinhood).
NOTE
