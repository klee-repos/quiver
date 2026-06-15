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

echo "=== quiver update — $(date -u '+%Y-%m-%dT%H:%M:%SZ') ==="

BR="$(git_q rev-parse --abbrev-ref HEAD)"
[ "$BR" = "HEAD" ] && { echo "ERROR: box is in detached HEAD — fix the checkout first."; exit 1; }

echo "[1/6] fetch origin/$BR"
git_q fetch --quiet origin "$BR"

OLD="$(git_q rev-parse --short HEAD)"
REMOTE="origin/$BR"
if [ "$(git_q rev-parse HEAD)" = "$(git_q rev-parse "$REMOTE")" ]; then
  echo "Already up to date at $OLD — nothing to do."
  echo "timer: $(systemctl is-active quiver.timer 2>/dev/null || echo unknown)"
  exit 0
fi

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
  echo "  deps changed -> reinstalling (locked env, then the package, mirroring setup.sh)"
  # Run pip FROM the project dir: requirements.lock.txt ends with `-e .` (an editable self-ref),
  # which only resolves when CWD is the repo. `sudo -u` doesn't reliably set CWD, so cd inside
  # the quiver shell explicitly. The lock's `-e .` already installs the package; the second
  # line is a harmless idempotent mirror of setup.sh.
  asq sh -c "cd '$QUIVER_HOME' && .venv/bin/pip install -q -r requirements.lock.txt"
  asq sh -c "cd '$QUIVER_HOME' && .venv/bin/pip install -q -e . --no-deps"
else
  echo "  no dependency changes — skipping pip"
fi

echo "[5/6] systemd units"
if grep -qE '^deploy/quiver\.(service|timer)$' <<<"$CHANGED"; then
  echo "  unit files changed -> reinstalling + daemon-reload"
  cp "$QUIVER_HOME/deploy/quiver.service" /etc/systemd/system/quiver.service
  cp "$QUIVER_HOME/deploy/quiver.timer"   /etc/systemd/system/quiver.timer
  systemctl daemon-reload
else
  echo "  no unit changes — skipping"
fi

# Gate the restart on the offline HEALTHCHECK — the purpose-built box self-test (config + XNYS
# calendar + ledger schema + every decision import load cleanly; no network/broker). This is the
# exact gate setup.sh runs at provision ([9/9]). The full unit suite is a LOCAL/pre-push gate, not
# a box gate — a couple of its cases are env/ordering-sensitive on the box and false-fail there.
# Run with the box env sourced so config validation sees RH_ACCOUNT_NUMBER etc. If it can't go
# green, leave the timer alone: the new code is on disk but the next tick keeps the old behavior.
echo "[6/6] offline healthcheck (restart gate)"
if ! asq bash -c "cd '$QUIVER_HOME' && set -a; . /etc/quiver/quiver.env 2>/dev/null; set +a; exec .venv/bin/python deploy/runner/healthcheck.py"; then
  echo
  echo "HEALTHCHECK FAILED on $NEW — NOT restarting the timer. Investigate before trusting a tick."
  exit 1
fi

# Each tick is a fresh python process spawned by the timer, so new code is picked up on the next
# tick regardless; restarting the timer just reloads the (possibly changed) unit cleanly. It does
# NOT kill an in-flight quiver.service tick.
systemctl restart quiver.timer

echo
echo "DONE — updated $OLD -> $NEW on $BR. timer: $(systemctl is-active quiver.timer). Next tick runs the new code."
