# Quiver — AWS deployment (headless, persistent box)

The pattern (decision D1, from the BRAX reference): a single hardened **t4g.small
Ubuntu** box runs a **systemd timer** that, on each weekday market-hours wake, drives
**one tick** by running the `claude` CLI headless over `TICK.md`. Secrets live in
**SSM Parameter Store** (free); the box runs the **CloudWatch agent** to ship the
per-tick log, and CloudWatch metric-filter alarms page via **SNS**; **EC2
auto-recovery** reboots-in-place on host failure (D4). ~**$12–15/mo** on-demand
(excluding Anthropic/DeepSeek token usage).

Execution-only by construction: the Python brain owns every decision, and the
**PreToolUse order guard** (`deploy/runner/order_guard.py`) mechanically denies any
order whose `ref_id` the Python `plan` did not reserve in the ledger (D5).

> **Honest limitation (D3/D10):** the Robinhood OAuth token has **no refresh** —
> re-auth is interactive. The bot runs until the token expires, then **hard-stops and
> pages you** (it never trades on stale auth). Measure the token TTL during the soak.

---

## 0. Prerequisites (you provide)
- An AWS account + region (default `us-east-1`), AWS CLI configured locally, Terraform ≥ 1.5.
- An EC2 key pair (for the one-time SSH MCP auth).
- A **read-only deploy key** so the box can clone the private repo over SSH (created in
  step 1b). Set `repo_url` to the SSH form: `git@github.com:<you>/quiver.git`.
- API keys: `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, `RESEND_API_KEY`; `RH_ACCOUNT_NUMBER`, `NOTIFY_TO`.
- A **Resend-verified sender domain** and `RESEND_FROM` (e.g. `quiver@yourdomain.com`). This powers
  the Python **last-resort pager** (`run_tick.py`): when the headless orchestrator crashes, times
  out, or preflight errors, it emails you directly over the Resend HTTP API — the failures you most
  need to hear about, which the in-tick MCP path can't cover. It is *optional only if you accept that
  orchestrator-level crashes won't page you*. Verify the domain in Resend (add the DNS records) FIRST;
  a blank/unverified `RESEND_FROM` is a silent no-send. Optional: `NOTIFY_ALERTS_TO` to route critical
  alerts to a separate pager list.

## 1. Create the secrets in SSM (out-of-band — never in tfstate)
```bash
P=/quiver
for k in ANTHROPIC_API_KEY DEEPSEEK_API_KEY RH_ACCOUNT_NUMBER RESEND_API_KEY NOTIFY_TO RESEND_FROM; do
  read -rs -p "$k: " v; echo
  aws ssm put-parameter --name "$P/$k" --type SecureString --value "$v" --overwrite
done
# Optional: a separate pager list for critical alerts (falls back to NOTIFY_TO):
# aws ssm put-parameter --name "$P/NOTIFY_ALERTS_TO" --type SecureString --value "pager@you.com" --overwrite
# RH_OAUTH_TOKEN is set after the interactive auth in step 4; seed a placeholder for now:
aws ssm put-parameter --name "$P/RH_OAUTH_TOKEN" --type SecureString --value "PENDING" --overwrite
```

## 1b. GitHub deploy key (read-only, repo-scoped — the box clones over SSH)
Modern best practice for one host needing read-only access to one private repo. The
key is scoped to this repo, can't push, and isn't tied to your account's other access.
```bash
ssh-keygen -t ed25519 -C "quiver-deploy-key" -f ~/.ssh/quiver_deploy -N ""    # no passphrase (unattended boot)
# Add the PUBLIC key as a read-only deploy key (leave write access OFF):
gh repo deploy-key add ~/.ssh/quiver_deploy.pub --title "quiver-box" --repo <you>/quiver
#   (or: repo -> Settings -> Deploy keys -> Add deploy key, paste ~/.ssh/quiver_deploy.pub)
# Store the PRIVATE key in SSM; the box fetches it via the instance role at boot:
aws ssm put-parameter --name "$P/GITHUB_DEPLOY_KEY" --type SecureString \
  --value "$(cat ~/.ssh/quiver_deploy)" --overwrite
```
The box pins GitHub's host key (no trust-on-first-use). You can delete the local
private key afterward — it lives in SSM. Set `repo_url = git@github.com:<you>/quiver.git`.

## 2. Provision with Terraform
```bash
cd deploy/terraform
cp terraform.tfvars.example terraform.tfvars   # fill alert_email, key_name, repo_url
terraform init && terraform apply
```
Confirm the **SNS email subscription** (check your inbox). Note the `instance_id` /
`public_ip` outputs. `user_data` runs `deploy/setup.sh` automatically (apt, Node, the
`claude` CLI, the venv, secrets→`/etc/quiver/quiver.env`, systemd timer, healthcheck).

## 3. Verify the box came up
```bash
ssh ubuntu@<public_ip>
sudo -u quiver /opt/quiver/.venv/bin/python /opt/quiver/deploy/runner/healthcheck.py   # -> ok:true
systemctl status quiver.timer
journalctl -u quiver.service -n 50
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a status   # -> "status": "running"
# PAGER ACCEPTANCE GATE — prove the last-resort alert path actually delivers (this
# really sends, using the box's RESEND_API_KEY + RESEND_FROM, exactly as run_tick.py
# would). You must receive the email within ~30s; if not, your pager is broken — fix
# RESEND_FROM (verified domain) / RESEND_API_KEY before going live.
set -a; . /etc/quiver/quiver.env; set +a
sudo -u quiver -E /opt/quiver/.venv/bin/python /opt/quiver/tick.py send-test --kind auth_error  # -> "sent": true
```

## 4. One-time Robinhood MCP auth (manual — no automation possible)
```bash
sudo -u quiver -H bash -c 'cd /opt/quiver && claude'    # then run /mcp and authenticate Robinhood
# capture the resulting token and push it to SSM, then re-pull the env:
aws ssm put-parameter --name /quiver/RH_OAUTH_TOKEN --type SecureString --value "<token>" --overwrite
sudo bash /opt/quiver/deploy/setup.sh                    # re-materializes /etc/quiver/quiver.env
```
On expiry you'll be paged via SNS; repeat this step.

## 5. Dry-run soak, then go live (decision D2)
0. **Put your real `config.yaml` on the box.** `config.yaml` is gitignored, so the clone
   doesn't include it — `setup.sh` seeds it from `config.yaml.example` (SAFE: `dry_run:
   true`, generic watchlist). SSH in and replace `/opt/quiver/config.yaml` with your real
   watchlist + caps (`scp` it up, or paste it), still `dry_run: true`, before the soak.
1. Ensure `config.yaml: dry_run: true`. Let the timer run a **full market day** —
   the tick reviews-but-never-places. Confirm **zero `place_equity_order` calls**
   (tool-use logs + an empty Robinhood order history) and that the digests match a
   local `/loop` dry-run. **Measure how long the OAuth token survives.**
2. Activate the strategy book (off-tick):
   `sudo -u quiver /opt/quiver/.venv/bin/python /opt/quiver/tick.py strategy-set --input '{"equity": <equity>}'`
   then flip `config.yaml: risk.rebalance_enabled: true` once you want target-aware sizing.
3. **Go live** on a fresh trading day only: flip `config.yaml: dry_run: false`
   (clear that day's ledger rows first if it already has any — see CLAUDE.md), with
   the caps in `config.yaml` sized for the account.

`dial_up_63_37.enabled` and the `learning.auto_apply_*` flags stay **OFF** until you
explicitly enable them.

---

## E2E drills (run before trusting it with money)
- **AUTH_ERROR:** put a garbage `RH_OAUTH_TOKEN` in SSM, re-run setup, trigger a tick
  (`sudo systemctl start quiver.service`) → it must place nothing, the `AUTH_ERROR`
  CloudWatch alarm fires, and the run exits non-zero.
- **Order guard:** the offline suite already proves `order_guard.evaluate` denies an
  unreserved `ref_id` and allows a reserved one; on the box, confirm the PreToolUse
  hook is wired (`--settings deploy/runner/settings.json`) by inspecting a tick's
  tool-use log.
- **Crash recovery:** kill the box mid-tick (`aws ec2 reboot-instances`); on reboot,
  `preflight` reports any reserved-but-unfinalized order and reconciles via
  `get_equity_orders` without minting a new `ref_id`.
- **Halt / KILL:** `sudo -u quiver touch /opt/quiver/KILL` → the next tick no-ops; a
  daily-loss breach writes `KILL` and pages you.

## Controls
- **Stop trading now:** `ssh` in, `sudo -u quiver touch /opt/quiver/KILL` (or stop the
  timer: `sudo systemctl stop quiver.timer`).
- **Logs:** on the box, `journalctl -u quiver.service -f` (full detail incl. the raw
  orchestrator tail) or `tail -f /var/log/quiver/tick.log` (the clean per-tick status
  lines). The CloudWatch agent ships `tick.log` to the `/quiver/tick` log group, where
  the metric-filter alarms (`AUTH_ERROR` / `write_kill` / plan `error`) fire to SNS.
  `run_tick.py` surfaces `AUTH_ERROR` (scanned from the orchestrator transcript) and a
  daily-loss halt (`write_kill`, read deterministically from the ledger) as their own
  clean lines, so the alarms key on real conditions — not on the noisy transcript.
- **Cost:** EC2 ~$12 + EBS ~$1.6 + CloudWatch ~$1–3 + SSM $0 ≈ **$15/mo**; a Savings
  Plan trims the EC2 floor toward ~$8.
