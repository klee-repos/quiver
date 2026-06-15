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

if [ -n "$(git log "origin/$BR..$BR" --oneline 2>/dev/null)" ]; then
  echo "==> pushing unpushed commits on $BR to origin"
  git push origin "$BR"
else
  echo "==> $BR already pushed to origin (nothing to push)"
fi

echo "==> updating the box ($INSTANCE, $ZONE) over IAP — this runs update.sh as root there"
gcloud compute ssh "$INSTANCE" --zone "$ZONE" --tunnel-through-iap \
  --command="sudo bash -s" < "$REPO_ROOT/deploy/gcp/update.sh"
