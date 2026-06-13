# Quiver — AWS deployment (headless, persistent box)

The pattern (decision D1, from the BRAX reference): a single hardened **t4g.small
Ubuntu** box runs a **systemd timer** that, on each weekday market-hours wake, drives
**one tick** by running the `claude` CLI headless over `TICK.md`. Secrets live in
**SSM Parameter Store** (free); CloudWatch metric-filter alarms page via **SNS**;
**EC2 auto-recovery** reboots-in-place on host failure (D4). ~**$12–15/mo** on-demand
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
- The repo reachable by URL (public, or a deploy key on the box).
- API keys: `ANTHROPIC_API_KEY`, `DEEPSEEK_API_KEY`, `RESEND_API_KEY`; `RH_ACCOUNT_NUMBER`, `NOTIFY_TO`.

## 1. Create the secrets in SSM (out-of-band — never in tfstate)
```bash
P=/quiver
for k in ANTHROPIC_API_KEY DEEPSEEK_API_KEY RH_ACCOUNT_NUMBER RESEND_API_KEY NOTIFY_TO; do
  read -rs -p "$k: " v; echo
  aws ssm put-parameter --name "$P/$k" --type SecureString --value "$v" --overwrite
done
# RH_OAUTH_TOKEN is set after the interactive auth in step 4; seed a placeholder for now:
aws ssm put-parameter --name "$P/RH_OAUTH_TOKEN" --type SecureString --value "PENDING" --overwrite
```

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
- **Logs:** `journalctl -u quiver.service -f` on the box, or the `/quiver/tick`
  CloudWatch log group.
- **Cost:** EC2 ~$12 + EBS ~$1.6 + CloudWatch ~$1–3 + SSM $0 ≈ **$15/mo**; a Savings
  Plan trims the EC2 floor toward ~$8.
