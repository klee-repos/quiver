#!/usr/bin/env bash
# Quiver — in-place code update for the running GCP box. Idempotent; safe to re-run.
# Runs AS ROOT (the laptop wrapper pipes this in via `sudo bash -s`); it drops to the
# `quiver` user for every git/pip/test op (the repo + venv are quiver-owned) and only
# uses root for the systemd bits. Normally you don't run this by hand — use the laptop
# one-liner `deploy/gcp/sync.sh`. To run it directly on the box over IAP SSH instead:
#   sudo bash /opt/quiver/deploy/gcp/update.sh
set -euo pipefail

QUIVER_HOME="${QUIVER_HOME:-/opt/quiver}"
QUIVER_USER="${QUIVER_USER:-quiver}"

# Every git/pip/python op runs as the repo owner. `-c safe.directory` keeps git happy when
# the invoking shell is root but the tree is quiver-owned.
asq()   { sudo -u "$QUIVER_USER" "$@"; }
git_q() { asq git -C "$QUIVER_HOME" -c safe.directory="$QUIVER_HOME" "$@"; }

# The deploy lock, held for the whole run. claude-update.sh takes the same lock, so the
# Claude binary never changes while this script installs code or units.
exec 9>/run/quiver-deploy.lock
flock -w 900 9 || { echo "ERROR: another deploy holds /run/quiver-deploy.lock — try again later."; exit 1; }

echo "=== quiver update — $(date -u '+%Y-%m-%dT%H:%M:%SZ') ==="

BR="$(git_q rev-parse --abbrev-ref HEAD)"
[ "$BR" = "HEAD" ] && { echo "ERROR: box is in detached HEAD — fix the checkout first."; exit 1; }

echo "[1/6] fetch origin/$BR"
git_q fetch --quiet origin "$BR"

OLD="$(git_q rev-parse --short HEAD)"
REMOTE="origin/$BR"
if [ "$(git_q rev-parse HEAD)" = "$(git_q rev-parse "$REMOTE")" ]; then
  echo "Already up to date at $OLD — nothing to do."
  # A run that failed its healthcheck left the broker path stopped. After a box-side fix,
  # a re-run starts it again, but only when the healthcheck passes.
  if ! systemctl is-active --quiet quiver-broker.path; then
    if asq bash -c "cd '$QUIVER_HOME' && set -a; . /etc/quiver/quiver.env 2>/dev/null; set +a; exec .venv/bin/python deploy/runner/healthcheck.py"; then
      systemctl start quiver-broker.path && echo "broker path started"
    else
      echo "broker paused: healthcheck failed"; exit 1
    fi
  fi
  echo "timer: $(systemctl is-active quiver.timer 2>/dev/null || echo unknown)"
  exit 0
fi

# The broker path unit stays stopped for the whole update, so no broker run starts against
# half-installed code or a guard that fails its healthcheck. It starts again only in [6/6].
echo "[0/6] pause the broker"
systemctl stop quiver-broker.path 2>/dev/null || true
while systemctl is-active --quiet quiver-broker.service; do sleep 5; done   # never stop it mid-order
trap 'systemctl is-active --quiet quiver-broker.path || echo "broker paused: fix and re-run update.sh"' EXIT

# A ff-only pull would refuse on a dirty tree anyway; check first so we can give a clear
# message and protect a box-side config.yaml edit (the one tracked file someone might tweak
# in place — e.g. flipping dry_run to watch a tick). Untracked files (.gcp-provisioned) ignored.
echo "[2/6] verifying the working tree is clean"
DIRTY="$(git_q status --porcelain --untracked-files=no)"
if [ -n "$DIRTY" ]; then
  echo "REFUSING TO PULL — the box has local changes to tracked files:"
  echo "$DIRTY"
  if grep -q 'config\.yaml$' <<<"$DIRTY"; then
    BAK="/tmp/quiver-config.$(date -u '+%Y%m%dT%H%M%SZ').bak"
    asq cp "$QUIVER_HOME/config.yaml" "$BAK"
    echo
    echo "config.yaml diverges from the repo. Backed up the box copy to: $BAK"
    echo "Its diff vs the repo:"
    git_q --no-pager diff -- config.yaml || true
    echo
    echo "Decide which wins, then re-run:"
    echo "  keep the repo's version : sudo -u $QUIVER_USER git -C $QUIVER_HOME checkout -- config.yaml"
    echo "  keep the box's version  : commit + push your config to the repo first"
  fi
  exit 1
fi

# What changed across this update — drives the conditional reinstall/reload below.
CHANGED="$(git_q diff --name-only HEAD "$REMOTE")"

echo "[3/6] fast-forward $OLD -> $(git_q rev-parse --short "$REMOTE")"
git_q merge --ff-only "$REMOTE"
NEW="$(git_q rev-parse --short HEAD)"

echo "[4/6] dependencies"
if grep -qE '^(requirements\.lock\.txt|pyproject\.toml)$' <<<"$CHANGED"; then
  # Run pip FROM the project dir: requirements.lock.txt ends with `-e .` (an editable self-ref),
  # which only resolves when CWD is the repo. The second line mirrors setup.sh.
  asq sh -c "cd '$QUIVER_HOME' && .venv/bin/pip install -q -r requirements.lock.txt"
  asq sh -c "cd '$QUIVER_HOME' && .venv/bin/pip install -q -e . --no-deps"
fi
# git deleted quiver_eve/, but its gitignored node_modules/ stays on disk.
if [ -d "$QUIVER_HOME/quiver_eve" ] && [ -z "$(git_q ls-files quiver_eve)" ]; then
  rm -rf "$QUIVER_HOME/quiver_eve" && echo "  removed quiver_eve/"
fi

echo "[5/6] directories + systemd units"
# Every unit lists these paths. A missing one stops the unit.
install -d -m 755 -o "$QUIVER_USER" -g "$QUIVER_USER" "$QUIVER_HOME/logs"
for d in state/claude-config state/claude-desk state/triggers state/tmp state/.cache logs/desk; do
  [ -d "$QUIVER_HOME/$d" ] || install -d -m 700 -o "$QUIVER_USER" -g "$QUIVER_USER" "$QUIVER_HOME/$d"
done
for u in quiver-intel.timer quiver-intel.service; do
  if [ -e "/etc/systemd/system/$u" ]; then
    systemctl disable --now "$u" 2>/dev/null || true; rm -f "/etc/systemd/system/$u"; echo "  removed $u"
  fi
done
UNITS="quiver.service quiver.timer quiver-chat.service quiver-worker.path quiver-worker.service
quiver-broker.path quiver-broker.service quiver-claude-update.service quiver-claude-update.timer
quiver-backup.service quiver-backup.timer"
if grep -qE '^deploy/(quiver[a-z-]*\.(service|timer|path)|claude-update\.sh)$' <<<"$CHANGED"; then
  for u in $UNITS; do cp "$QUIVER_HOME/deploy/$u" "/etc/systemd/system/$u"; done
  install -m 0755 -o root -g root "$QUIVER_HOME/deploy/claude-update.sh" /usr/local/bin/quiver-claude-update
fi
systemctl daemon-reload
systemctl reset-failed quiver-intel.service 2>/dev/null || true
systemctl enable quiver-worker.path quiver-broker.path quiver-claude-update.timer quiver-backup.timer
systemctl start quiver-worker.path quiver-claude-update.timer quiver-backup.timer
# A changed path unit must restart to watch again. The broker path starts only after the
# healthcheck. NEVER restart quiver-broker.service here.
grep -qE '^deploy/quiver-worker\.path$' <<<"$CHANGED" && systemctl restart quiver-worker.path

# The offline HEALTHCHECK gates every restart: config, the XNYS calendar, the ledger schema,
# every import, the guard self-test. No network, no broker. setup.sh runs the same gate at
# [9/9]. If it fails, the new code is on disk, but the broker path stays stopped.
echo "[6/6] offline healthcheck (restart gate)"
if ! asq bash -c "cd '$QUIVER_HOME' && set -a; . /etc/quiver/quiver.env 2>/dev/null; set +a; exec .venv/bin/python deploy/runner/healthcheck.py"; then
  echo "HEALTHCHECK FAILED on $NEW — NOT restarting the chat bridge, the timer, or the broker path."; exit 1
fi
systemctl start quiver-broker.path
# The bridge is long-running and imports most of lib/: restart it on ANY change outside docs/
# and tests/. Worker, broker, and monitor start a fresh process each run.
if grep -qvE '^(docs/|tests/)' <<<"$CHANGED" && systemctl is-enabled --quiet quiver-chat.service; then
  systemctl restart quiver-chat.service && echo "  chat bridge restarted"
fi
systemctl restart quiver.timer

echo
echo "DONE — updated $OLD -> $NEW on $BR. timer: $(systemctl is-active quiver.timer). broker path: $(systemctl is-active quiver-broker.path). The next run uses the new code."
