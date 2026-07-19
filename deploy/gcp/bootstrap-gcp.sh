#!/usr/bin/env bash
# One-time GCP bootstrap — push the local .env + strategy.yaml secrets into Secret Manager
# as quiver-<KEY>, so the box reads them via its service account (never in tfstate, never in
# the public repo). Idempotent: creates the secret if absent, then adds a new version.
# Run from anywhere with gcloud authed (gcloud auth login).
#   ./deploy/gcp/bootstrap-gcp.sh
# Env overrides: GCP_PROJECT (defaults to the gcloud config project), SECRET_PREFIX (quiver-).
set -euo pipefail

PROJECT="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
PREFIX="${SECRET_PREFIX:-quiver-}"
cd "$(dirname "$0")/../.."
[ -f .env ] || { echo "FATAL: no .env (cp .env.example .env first)"; exit 1; }
[ -f strategy.yaml ] || { echo "FATAL: no strategy.yaml (cp strategy.yaml.example strategy.yaml)"; exit 1; }
[ -n "$PROJECT" ] || { echo "FATAL: no GCP project (gcloud config set project <id>)"; exit 1; }
echo "project: $PROJECT  prefix: $PREFIX"
set -a; . ./.env; set +a

put() {  # $1 = key, $2 = value
  local name="$PREFIX$1"
  gcloud secrets describe "$name" --project="$PROJECT" >/dev/null 2>&1 \
    || gcloud secrets create "$name" --replication-policy=automatic --project="$PROJECT" >/dev/null
  printf '%s' "$2" | gcloud secrets versions add "$name" --data-file=- --project="$PROJECT" >/dev/null \
    && echo "  $name  <- set"
}

echo "[1] secrets from .env -> Secret Manager"
for k in GLM_API_KEY DEEPSEEK_API_KEY OPENROUTER_API_KEY RH_ACCOUNT_NUMBER NOTIFY_TO RESEND_API_KEY RESEND_FROM NOTIFY_ALERTS_TO CLAUDE_CODE_OAUTH_TOKEN CONGRESS_API_KEY TELEGRAM_BOT_TOKEN TELEGRAM_ALLOWED_CHAT_IDS; do
  v="${!k:-}"; { [ -n "$v" ] && put "$k" "$v"; } || echo "  $k: (empty in .env, skipped)"
done

echo "[2] your private strategy book -> Secret Manager"
put STRATEGY_YAML "$(cat strategy.yaml)"

echo
echo "DONE. Secrets live in Secret Manager (project $PROJECT). Next:"
echo "  1. gcloud auth application-default login         # terraform uses ADC"
echo "  2. cd deploy/gcp/terraform && terraform init && terraform apply"
echo "  3. Link Chrome Remote Desktop + the on-box claude/mcp login (docs/DEPLOY.md)"
