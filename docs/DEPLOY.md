# Quiver — AWS deployment (headless box with an on-server browser for OAuth)

The pattern: a single hardened **x86_64 `t3.medium` Ubuntu 24.04** box runs a **systemd
timer** that, on each weekday market-hours wake, drives **one tick** by running the
`claude` CLI headless over `TICK.md`. Secrets live in **SSM Parameter Store** (free); the
box runs the **CloudWatch agent** to ship the per-tick log, metric-filter alarms page via
**SNS**, and **EC2 auto-recovery** reboots-in-place on host failure. ~**$30/mo**
(t3.medium + EBS + CloudWatch), plus your Claude Pro/Max subscription and DeepSeek usage.

The box also runs a **lightweight desktop (XFCE) + Chrome + Chrome Remote Desktop**, used
only for the periodic interactive OAuth (below). Execution-only by construction: the Python
brain owns every decision, and the **PreToolUse order guard**
(`deploy/runner/order_guard.py`) mechanically denies any order whose `ref_id` the Python
`plan` did not reserve in the ledger.

> **The defining constraint (Robinhood OAuth):** Robinhood's agentic MCP authenticates via
> **OAuth with a localhost browser redirect**, and the token **fully expires ~every 3.8
> days with no headless refresh**. So re-auth is interactive on *any* host. We solve it by
> giving the box its own browser: you connect via **Chrome Remote Desktop**, do the login
> **on the box**, and the headless tick reuses the stored OAuth. Expect a ~1-minute re-auth
> every ~4 days; that is a Robinhood platform limit, not a deploy bug.

> **No inbound ports.** Chrome Remote Desktop dials *out* to Google's relay, and operator
> shell is via **SSM Session Manager** — so the box needs zero inbound security-group rules.

---

## 0. Prerequisites (you provide)
- An AWS account + region (`us-east-1`), AWS CLI configured locally, Terraform ≥ 1.5, `gh`.
- An EC2 key pair (`bootstrap-aws.sh` creates `quiver-box`) — optional now that admin is via
  SSM Session Manager, but kept for break-glass.
- The box clones over SSH with a **read-only deploy key** (created by `bootstrap-aws.sh`);
  `repo_url` is the SSH form `git@github.com:<you>/quiver.git`.
- API keys/values pushed to SSM: `DEEPSEEK_API_KEY`, `RESEND_API_KEY`, `RH_ACCOUNT_NUMBER`,
  `NOTIFY_TO`, and a **Resend-verified** `RESEND_FROM` (powers the Python last-resort pager).
  Optional `NOTIFY_ALERTS_TO`.
- **Claude Code auth is OPTIONAL up front** — either push `CLAUDE_CODE_OAUTH_TOKEN`
  (`claude setup-token`) / `ANTHROPIC_API_KEY` to SSM, **or** just `claude login` once in the
  on-box desktop (recommended — keeps everything in the same browser flow as Robinhood).
- **There is NO `RH_OAUTH_TOKEN`** any more — Robinhood auth is the on-box browser OAuth.

> **`./deploy/bootstrap-aws.sh`** pushes every SSM secret from your local `.env` + the
> private `strategy.yaml`, creates the read-only GitHub deploy key, and makes the EC2 key
> pair. It is idempotent. (For this account that is already done.)

## 1. Secrets in SSM (out-of-band — never in tfstate)
`bootstrap-aws.sh` handles this. Manual equivalent:
```bash
P=/quiver
for k in DEEPSEEK_API_KEY RH_ACCOUNT_NUMBER RESEND_API_KEY NOTIFY_TO RESEND_FROM; do
  read -rs -p "$k: " v; echo
  aws ssm put-parameter --name "$P/$k" --type SecureString --value "$v" --overwrite
done
# Your private strategy book (the trading universe); config.yaml is committed, so not needed:
aws ssm put-parameter --name "$P/STRATEGY_YAML" --type SecureString \
  --tier Intelligent-Tiering --value "$(cat strategy.yaml)" --overwrite
# Optional Claude auth (else do `claude login` on the box):
# aws ssm put-parameter --name "$P/CLAUDE_CODE_OAUTH_TOKEN" --type SecureString --value "<sk-ant-oat...>" --overwrite
```

## 2. Provision with Terraform
```bash
cd deploy/terraform
# terraform.tfvars is already filled (alert_email, key_name, repo_url, t3.medium).
terraform init && terraform apply
```
`user_data` runs `setup.sh` (trading stack) then `setup-desktop.sh` (XFCE + Chrome + CRD).
Confirm the **SNS email subscription** AWS sends you (until confirmed, no alarm pages).
Note the `instance_id` / `public_ip` outputs.

## 3. Verify the box (via SSM Session Manager — no SSH needed)
```bash
aws ssm start-session --target <instance_id>
sudo -u quiver bash -c 'set -a; . /etc/quiver/quiver.env; set +a; \
  /opt/quiver/.venv/bin/python /opt/quiver/deploy/runner/healthcheck.py'   # -> ok:true
systemctl status quiver.timer
/opt/aws/amazon-cloudwatch-agent/bin/amazon-cloudwatch-agent-ctl -a status # -> "running"
# PAGER ACCEPTANCE GATE — really sends via the box's RESEND_API_KEY + RESEND_FROM:
sudo -u quiver bash -c 'set -a; . /etc/quiver/quiver.env; set +a; \
  /opt/quiver/.venv/bin/python /opt/quiver/tick.py send-test --kind auth_error'  # -> sent:true
```

## 4. Link Chrome Remote Desktop (one-time, human — tied to YOUR Google account)
1. Open <https://remotedesktop.google.com/headless> → "Set up another computer" → Debian
   Linux, and copy the `start-host` command (it carries a one-time `--code="4/..."`).
2. Run it on the box as the `quiver` user, then set a 6-digit PIN:
   ```bash
   sudo -u quiver env HOME=/home/quiver CLAUDE_CONFIG_DIR=/opt/quiver/state/claude-config \
     /opt/google/chrome-remote-desktop/start-host --code="4/PASTE" \
     --redirect-url="https://remotedesktop.google.com/_/oauthredirect" --name=quiver-box
   ```
3. The box appears at <https://remotedesktop.google.com/access>. Connect with the PIN.

## 5. On-box logins (one-time, then ~every 3.8 days for Robinhood)
In the Chrome Remote Desktop session, open a terminal:
```bash
claude            # sign into your Pro/Max plan (skip if a token was set in SSM)
# then inside claude:
/mcp              # -> robinhood-trading -> authenticate in the browser that opens
```
Both credentials persist under `CLAUDE_CONFIG_DIR` (`/opt/quiver/state/claude-config`) —
the quiver desktop shells export this via `~/.bashrc`/`~/.profile` (set by
`setup-desktop.sh`), so the OAuth lands exactly where the headless tick reads it. **Re-auth
Robinhood (`/mcp`) every ~3.8 days** — you'll be paged (`AUTH_ERROR`) when it lapses; the
bot stops, never trades blind.

> Before a re-auth, optionally `sudo systemctl stop quiver.timer` and `start` it after, so a
> tick can't read the credential mid-rewrite. Not required — a tick that hits a half-written
> cred just AUTH-fails safe and retries next wake — but it avoids a spurious page.

## 6. Validate broker auth, then live
```bash
# Prove the headless tick can actually reach the broker on the stored OAuth (read-only):
aws ssm start-session --target <instance_id>
sudo systemctl start quiver.service     # off-hours: preflight no-ops, but confirms auth path
journalctl -u quiver.service -n 80      # look for a clean broker read, no AUTH_ERROR
```
`config.yaml` ships **`dry_run: false` (LIVE)** by design — **straight to live** per the
operator. The trading universe is your `strategy.yaml` book; the first eligible
market-hours tick deploys real capital via the validated per-ticker path. To halt at any
time: `sudo -u quiver touch /opt/quiver/KILL`.

> **Config on the box** comes from the committed `config.yaml` in the clone (NOT SSM —
> there is no `CONFIG_YAML` seed). To change it on a running box, edit
> `/opt/quiver/config.yaml` directly (`sudo -u quiver vi ...`); setup.sh re-runs never
> clobber it. `rebalance_enabled`/`reconcile_unmanaged` only activate after a `strategy-set`
> goal is seeded — until then the first ticks are buys-only, not liquidations.

---

## E2E drills (run before trusting it with money)
- **AUTH_ERROR:** let the Robinhood OAuth lapse (or revoke it) → a tick must place nothing,
  fire the `AUTH_ERROR` CloudWatch alarm, and exit non-zero.
- **Order guard:** the offline suite proves `order_guard.evaluate` denies an unreserved
  `ref_id`; on the box, confirm the PreToolUse hook is wired (`--settings
  deploy/runner/settings.json`) in a tick's tool-use log.
- **Crash recovery:** `aws ec2 reboot-instances`; on reboot, `preflight` reconciles any
  reserved-but-unfinalized order via `get_equity_orders` without minting a new `ref_id`.
- **Halt / KILL:** `sudo -u quiver touch /opt/quiver/KILL` → next tick no-ops; a daily-loss
  breach writes `KILL` and pages you.

## Controls
- **Stop trading now:** SSM in, `sudo -u quiver touch /opt/quiver/KILL` (or `sudo systemctl
  stop quiver.timer`).
- **Logs:** `journalctl -u quiver.service -f` (full detail) or `tail -f
  /var/log/quiver/tick.log` (clean status lines the CloudWatch alarms key on).
- **Admin shell:** `aws ssm start-session --target <instance_id>` (no inbound port).
- **Desktop:** <https://remotedesktop.google.com/access> + PIN (no inbound port).
- **Cost:** EC2 t3.medium ~$30 + EBS ~$2 + CloudWatch ~$1–3 + SSM $0 ≈ **$33/mo**; a
  Savings Plan trims the EC2 floor.
