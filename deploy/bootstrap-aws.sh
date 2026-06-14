#!/usr/bin/env bash
# One-time AWS bootstrap for the Quiver box — does everything that CAN be automated so
# the only manual steps left are the ones a human truly must do (ANTHROPIC_API_KEY,
# `terraform apply`, confirming the SNS email, and the interactive Robinhood /mcp).
#
# It is IDEMPOTENT (re-runnable): SSM puts use --overwrite; the EC2 key pair + GitHub
# deploy key are created only if absent. Run from the repo root with AWS creds + `gh`
# authed. Reads secrets from ./.env and the real config.yaml / strategy.yaml; pushes
# them to SSM SecureString so they never touch the repo or tfstate.
#
#   ./deploy/bootstrap-aws.sh
#
# Env overrides: AWS_REGION (us-east-1), SSM_PREFIX (/quiver), QUIVER_REPO
# (owner/repo for the deploy key), QUIVER_KEY_NAME (quiver-box).
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
P="${SSM_PREFIX:-/quiver}"
REPO="${QUIVER_REPO:-klee-repos/quiver}"
KEYNAME="${QUIVER_KEY_NAME:-quiver-box}"

cd "$(dirname "$0")/.."
[ -f .env ] || { echo "FATAL: no .env (cp .env.example .env first)"; exit 1; }
[ -f config.yaml ] || { echo "FATAL: no config.yaml (cp config.yaml.example config.yaml)"; exit 1; }
[ -f strategy.yaml ] || { echo "FATAL: no strategy.yaml (cp strategy.yaml.example strategy.yaml)"; exit 1; }

echo "AWS account: $(aws sts get-caller-identity --query Account --output text)  region: $REGION  prefix: $P"
set -a; . ./.env; set +a

put()     { aws ssm put-parameter --region "$REGION" --name "$P/$1" --type SecureString --value "$2" --overwrite >/dev/null && echo "  SSM $P/$1  <- set"; }
put_big() { aws ssm put-parameter --region "$REGION" --name "$P/$1" --type SecureString --tier Intelligent-Tiering --value "$2" --overwrite >/dev/null && echo "  SSM $P/$1  <- file ($(printf '%s' "$2" | wc -c | tr -d ' ')B)"; }
have()    { aws ssm get-parameter --region "$REGION" --name "$P/$1" >/dev/null 2>&1; }

echo "[1] secrets from .env -> SSM"
for k in DEEPSEEK_API_KEY RH_ACCOUNT_NUMBER NOTIFY_TO RESEND_API_KEY RESEND_FROM NOTIFY_ALERTS_TO; do
  v="${!k:-}"; [ -n "$v" ] && put "$k" "$v" || echo "  $k: (empty in .env, skipped)"
done

echo "[2] your private config + strategy -> SSM (Intelligent-Tiering)"
put_big CONFIG_YAML   "$(cat config.yaml)"
put_big STRATEGY_YAML "$(cat strategy.yaml)"

echo "[3] RH_OAUTH_TOKEN placeholder (the real token comes from the interactive /mcp)"
have RH_OAUTH_TOKEN || put RH_OAUTH_TOKEN "PENDING"

echo "[4] ANTHROPIC_API_KEY"
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then put ANTHROPIC_API_KEY "$ANTHROPIC_API_KEY";
else echo "  >> NOT set — this is one of the few you must push yourself:";
     echo "     aws ssm put-parameter --region $REGION --name $P/ANTHROPIC_API_KEY --type SecureString --value '<key>' --overwrite"; fi

echo "[5] GitHub read-only deploy key (the box clones over SSH)"
if have GITHUB_DEPLOY_KEY; then echo "  SSM $P/GITHUB_DEPLOY_KEY: exists";
else
  tmp=$(mktemp -d); ssh-keygen -t ed25519 -C "quiver-deploy" -f "$tmp/key" -N "" >/dev/null
  gh repo deploy-key add "$tmp/key.pub" --title "quiver-box" --repo "$REPO" >/dev/null 2>&1 \
    && echo "  deploy key added to $REPO (read-only)" || echo "  (gh deploy-key add skipped — may already exist)"
  put GITHUB_DEPLOY_KEY "$(cat "$tmp/key")"; rm -rf "$tmp"
fi

echo "[6] EC2 key pair ($KEYNAME) for the one-time SSH /mcp auth"
if aws ec2 describe-key-pairs --region "$REGION" --key-names "$KEYNAME" >/dev/null 2>&1; then
  echo "  key pair $KEYNAME: exists"
else
  umask 077; aws ec2 create-key-pair --region "$REGION" --key-name "$KEYNAME" \
    --query KeyMaterial --output text > "$HOME/.ssh/$KEYNAME.pem"
  chmod 600 "$HOME/.ssh/$KEYNAME.pem"; echo "  created -> ~/.ssh/$KEYNAME.pem"
fi

echo
echo "DONE. Still human-only (cannot be automated):"
echo "  1. ANTHROPIC_API_KEY -> SSM (above), if not done."
echo "  2. cd deploy/terraform && terraform apply   (spins up the paid box)."
echo "  3. Confirm the SNS subscription email AWS sends you."
echo "  4. One-time Robinhood /mcp on the box, then push RH_OAUTH_TOKEN to SSM + re-run setup.sh."
