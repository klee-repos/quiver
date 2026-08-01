#!/usr/bin/env bash
# Quiver — one-command deploy of your latest code to the running GCP box, from your laptop.
#
#   ./deploy/gcp/sync.sh
#
# The box pulls from GitHub, so this (1) pushes your current branch (no-op if already pushed),
# then (2) pipes deploy/gcp/update.sh over IAP SSH and runs it as root on the box: ff-only pull,
# conditional dep reinstall + systemd reload, unit-test gate, timer restart. Piping the local
# script means it works even on the very first run, before the box has this file.
#
# NOTE: only COMMITTED + PUSHED code deploys — uncommitted working-tree changes are not sent.
set -euo pipefail

ZONE="${QUIVER_ZONE:-us-central1-a}"
INSTANCE="${QUIVER_INSTANCE:-quiver}"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO_ROOT"

BR="$(git rev-parse --abbrev-ref HEAD)"
[ "$BR" = "main" ] || echo "WARNING: you're on '$BR', but the box tracks origin/main — it will only pull main."

# Pre-push gate. `git commit -am` stages only files git ALREADY tracks, so a new module can be
# referenced by a commit that does not contain it; the box then pulls the reference without the
# file. Nothing downstream catches that — update.sh ff-merges BEFORE its healthcheck, the
# healthcheck never imports tick.py, and a failed healthcheck only skips the timer restart while
# the new code sits on disk for the next (freshly spawned) tick to run anyway.
# This runs BEFORE the unpushed-commits test on purpose: the `else` branch below still updates
# the box, so gating only `git push` would leave the re-deploy path unguarded.
GUARD_PY=".venv/bin/python"; [ -x "$GUARD_PY" ] || GUARD_PY="python3"
if [ "${QUIVER_SKIP_REFS_CHECK:-}" = "1" ]; then
  echo "==> WARNING: skipping the untracked-reference gate (QUIVER_SKIP_REFS_CHECK=1)"
else
  # `if !`/`|| rc=$?` rather than a bare call: set -e at the top would abort here and none of
  # the remedy text below would ever print.
  # --rev HEAD, not the working tree: the box pulls a COMMIT. Checking the working tree here
  # would block a re-deploy of a clean HEAD whenever you happen to have an in-progress edit that
  # wires in a not-yet-added module — and a gate that blocks legitimate deploys just trains you
  # to set QUIVER_SKIP_REFS_CHECK by reflex. The working-tree question is asked by
  # tests/run_e2e.sh instead, where it belongs.
  rc=0; "$GUARD_PY" tests/check_tracked_refs.py --rev HEAD || rc=$?
  if [ "$rc" -eq 1 ]; then
    echo
    echo "REFUSING TO DEPLOY — tracked code references a file git is not carrying (above)."
    echo "The box would pull the reference without the file."
    echo "  fix   : git add <path>          (if it ships)  — or delete the reference (if it doesn't)"
    echo "  bypass: QUIVER_SKIP_REFS_CHECK=1 ./deploy/gcp/sync.sh   (emergency only)"
    exit 1
  elif [ "$rc" -ne 0 ]; then
    # Exit 2 = the check could not run (git failed, python missing). Distinct from "found a
    # violation" on purpose: "I could not look" must not read as "I looked and it is fine".
    echo
    echo "REFUSING TO DEPLOY — the untracked-reference gate could not run (exit $rc, above)."
    echo "  bypass: QUIVER_SKIP_REFS_CHECK=1 ./deploy/gcp/sync.sh   (emergency only)"
    exit 1
  fi
fi

if [ -n "$(git log "origin/$BR..$BR" --oneline 2>/dev/null)" ]; then
  echo "==> pushing unpushed commits on $BR to origin"
  git push origin "$BR"
else
  echo "==> $BR already pushed to origin (nothing to push)"
fi

echo "==> updating the box ($INSTANCE, $ZONE) over IAP — this runs update.sh as root there"
gcloud compute ssh "$INSTANCE" --zone "$ZONE" --tunnel-through-iap \
  --command="sudo bash -s" < "$REPO_ROOT/deploy/gcp/update.sh"
