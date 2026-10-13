# Quiver — GCP deployment (Compute Engine, headless)

The Quiver deploy, on GCP Compute Engine. A single hardened **`e2-small` Ubuntu 24.04 (x86_64)**
Compute Engine VM runs a **systemd timer** that drives one tick per weekday market-hours
wake by running the `claude` CLI headless over `TICK.md`. Secrets live in **Secret
Manager**; the **Google Cloud Ops Agent** ships the per-tick log to Cloud Logging where
**log-based metrics → Monitoring alert policies** page an email channel. GCE **auto-restart
+ live-migration** is the host-failure recovery. ~**$18/mo** + your Claude subscription
+ GLM usage.

The box has **no desktop**. It needs two logins, and neither needs a browser on the box:

- **Claude (the orchestrator):** a one-year subscription token from `claude setup-token`,
  stored as the `quiver-CLAUDE_CODE_OAUTH_TOKEN` secret. No interactive login on the box.
- **Robinhood (the broker MCP):** an OAuth login with no headless refresh. Run
  `./deploy/gcp/rh-reauth.sh` on your laptop when the `AUTH_ERROR` page arrives.

Execution-only: Python owns every decision, and the **PreToolUse order guard** denies any
order whose `ref_id` the Python `plan` did not reserve.

> **No public inbound.** The custom VPC default-denies ingress; the ONLY inbound rule is
> SSH from Google's IAP range (`35.235.240.0/20`) scoped to the box's service account. Admin
> is `gcloud compute ssh --tunnel-through-iap`. The external IP is egress-only.

---

## 0. Prerequisites
- `gcloud` authed (`gcloud auth login`) and a project with **billing enabled**
  (set in `terraform.tfvars`, see `terraform.tfvars.example`). Terraform ≥ 1.5.
- Required APIs enabled (compute, secretmanager, iap, logging, monitoring,
  cloudresourcemanager) — `gcloud services enable ...`.
- The repo is **public**, so the box clones over HTTPS — no deploy key / SSH / `gh` needed.
- Terraform auth: either `gcloud auth application-default login`, OR (no browser) export a
  short-lived token: `export GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token)`.
- Claude Code ≥ 2.1.186 on your laptop and on the box (`claude mcp login --no-browser`).

## 1. Seed Secret Manager (out-of-band — never in tfstate)
First make the orchestrator's Claude token. On your laptop:
```bash
claude setup-token        # authorize in the browser; it prints sk-ant-oat01-...
```
Put the value in `.env` as `CLAUDE_CODE_OAUTH_TOKEN=...`. The token is valid one year. Then:
```bash
./deploy/gcp/bootstrap-gcp.sh      # pushes .env + strategy.yaml -> quiver-<KEY> secrets
```
Creates `quiver-OPENROUTER_API_KEY` (the live brain key), `quiver-RH_ACCOUNT_NUMBER`,
`quiver-STRATEGY_YAML`, `quiver-CLAUDE_CODE_OAUTH_TOKEN`, the Telegram secrets
(`quiver-TELEGRAM_BOT_TOKEN` / `quiver-TELEGRAM_ALLOWED_CHAT_IDS` — optional, see
docs/CHAT.md), plus the rollback-only `quiver-GLM_API_KEY` / `quiver-DEEPSEEK_API_KEY` /
`quiver-RESEND_API_KEY` / `quiver-NOTIFY_TO` if set in `.env`.

## 2. Provision with Terraform
```bash
cd deploy/gcp/terraform
export GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token)   # or ADC login
terraform init && terraform apply
```
The instance `startup-script` clones the public repo and runs `deploy/gcp/setup.sh` (trading
stack: venv, claude CLI, secrets→`/etc/quiver/quiver.env`, a 4G swapfile, systemd timer, Ops
Agent).

**Click the GCP Monitoring verification email** Google sends to `alert_email` — the email
notification channel is created UNVERIFIED and delivers nothing until confirmed. (The **Telegram
pager** — in-tick `tick.py report-send` + the last-resort sender in `run_tick.py` — is the primary,
cloud-agnostic alert path; these Cloud Monitoring policies are a secondary net.)

**Turn on the Telegram pager (one-time).** All alerting (daily digest + halt/auth/error pages) is a
Telegram HTTPS POST — no email, no MCP. It reuses the chat-bridge bot: set `TELEGRAM_BOT_TOKEN` +
`TELEGRAM_ALLOWED_CHAT_IDS` (see docs/CHAT.md to create the bot / find your chat id), then:
1. Push the secrets: `./deploy/gcp/bootstrap-gcp.sh` (already lists both).
2. **Grant the box SA `secretAccessor` on both** — `terraform/main.tf` excludes them by default, so:
   ```bash
   SA="quiver-box@eighth-duality-354701.iam.gserviceaccount.com"
   for S in TELEGRAM_BOT_TOKEN TELEGRAM_ALLOWED_CHAT_IDS; do
     gcloud secrets add-iam-policy-binding "quiver-$S" \
       --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor
   done
   ```
3. **Refresh `/etc/quiver/quiver.env` on the box** (setup.sh runs only at provision; update.sh never
   rewrites it) — over IAP SSH, for each of the two vars use the refresh one-liner below,
   then `sudo systemctl restart quiver.timer quiver-chat.service`.
4. **Gate it** with the send-test below — the `[6/6]` healthcheck can NOT see a missing token.

## 3. Verify the box (admin via IAP — no public inbound)
IAP-tunneled SSH is the ONLY shell (no public SSH). The principal running it needs
**`roles/iap.tunnelResourceAccessor`** + an OS Login role — a project **Owner** has both
implicitly (so your owner account works out of the box); a non-owner operator must be granted
them, or they're locked out.
```bash
gcloud compute ssh quiver --zone us-central1-a --tunnel-through-iap
sudo tail -50 /var/log/quiver-startup.log          # provisioning log
sudo -u quiver bash -c 'set -a; . /etc/quiver/quiver.env; set +a; \
  /opt/quiver/.venv/bin/python /opt/quiver/deploy/runner/healthcheck.py'   # -> ok:true
systemctl status quiver.timer
# ORCHESTRATOR LOGIN (the same probe the tick runs before its analysis fan-out):
sudo -u quiver bash -c 'cd /opt/quiver; set -a; . /etc/quiver/quiver.env; set +a; \
  "$CLAUDE_BIN" -p "reply with exactly: AUTH_OK" </dev/null'                # -> AUTH_OK
# PAGER ACCEPTANCE GATE (really sends a Telegram message to your chat):
sudo -u quiver bash -c 'set -a; . /etc/quiver/quiver.env; set +a; \
  /opt/quiver/.venv/bin/python /opt/quiver/tick.py send-test --kind auth_error'  # -> sent:true
```

## 4. Log in to Robinhood (from your laptop)
```bash
./deploy/gcp/rh-reauth.sh            # log in, then print the server status
./deploy/gcp/rh-reauth.sh --status   # print the server status only
```
The script opens an IAP SSH session and runs `claude mcp login robinhood-trading --no-browser`
on the box as `quiver`. Then:
1. The box prints a Robinhood authorization URL. Open it in a browser on your laptop. Log in
   and approve.
2. The browser then shows a connection error for `http://localhost:<port>/callback?code=...`.
   This is correct. Nothing listens on that port on your laptop.
3. Copy the full address of that page. Paste it at the prompt in the terminal.

The script ends with `OK: robinhood-trading is connected`. The login persists under
`CLAUDE_CONFIG_DIR` (`/opt/quiver/state/claude-config`). **Repeat this when the `AUTH_ERROR`
page arrives** — the Robinhood OAuth expires every few days and has no headless refresh. The
bot stops when it lapses and never trades blind.

## 5. Validate broker auth, then live
```bash
gcloud compute ssh quiver --zone us-central1-a --tunnel-through-iap
sudo systemctl start quiver.service     # off-hours: confirms the broker read path
sudo journalctl -u quiver.service -n 80 # expect a clean get_portfolio, no AUTH_ERROR
```
`config.yaml` ships **`dry_run: false` (LIVE)** — straight to live per the operator. Halt
anytime: `sudo -u quiver touch /opt/quiver/KILL`.

## Updating the running box (code / secrets)
**Code:** `./deploy/gcp/sync.sh` from your laptop — pushes `origin/main`, then runs
`deploy/gcp/update.sh` on the box (ff-only pull, conditional pip/systemd reinstall, offline
healthcheck gate, timer restart). Only committed + pushed `main` deploys; a box-side
`config.yaml` edit blocks the pull until reconciled.

**A new/rotated secret (e.g. the GLM key):** `update.sh` does **NOT** touch
`/etc/quiver/quiver.env` — that file is written only at provision (`setup.sh`). So a secret
*change* on a live box is three steps:
1. `./deploy/gcp/bootstrap-gcp.sh` — push the new value to `quiver-<KEY>` in Secret Manager.
2. Grant the box SA read access **if the key is new** (add it to `locals.secrets` for IaC truth,
   but see the terraform note before `apply`; for a live box do the grant out-of-band):
   `gcloud secrets add-iam-policy-binding quiver-<KEY> --role=roles/secretmanager.secretAccessor \
     --member=serviceAccount:quiver-box@<proj>.iam.gserviceaccount.com`
3. Refresh `quiver.env` **on the box** (over IAP SSH) — never re-run the whole `setup.sh` on a
   live box (it re-touches the MCP/OAuth layer):
   ```bash
   sudo bash -c 'K=$(gcloud secrets versions access latest --secret=quiver-<KEY>); \
     F=/etc/quiver/quiver.env; grep -v "^<KEY>=" "$F" > "$F.t"; echo "<KEY>=$K" >> "$F.t"; \
     chmod 600 "$F.t"; chown quiver:quiver "$F.t"; mv "$F.t" "$F"'
   ```
   The next tick (or `sync.sh`) then runs with the new env.

**Rotate the Claude token (once a year):** run `claude setup-token` on your laptop, put the new
value in `.env`, then do the three steps above with `<KEY>` = `CLAUDE_CODE_OAUTH_TOKEN`. Restart
the chat bridge so it reads the new env (`sudo systemctl restart quiver-chat.service`) and run
the `AUTH_OK` probe from section 3. The token takes precedence over any login stored under
`CLAUDE_CONFIG_DIR`. A tick with a dead token fails its auth gate before the analysis fan-out
and pages `orchestrator`.

**⚠️ Terraform & the LIVE box — do not casually `apply`:** the instance carries
`lifecycle { prevent_destroy = true, ignore_changes = [boot image, shielded_instance_config] }`.
The boot image resolves from the `ubuntu-2404-lts-amd64` *family* (= latest), so without
`ignore_changes` every new Ubuntu image GCP publishes **forces a full instance replace** on the
next `apply` — wiping the ledger + the on-box Robinhood OAuth. `prevent_destroy` makes
`apply` **error** rather than recreate. To intentionally rebuild, remove those guards
deliberately (and migrate state first). Secret IAM bindings live in `locals.secrets`; if you
grant one out-of-band with `gcloud`, `terraform import` it (and `terraform state rm` a retired
one whose GCP binding you want to keep as a rollback) so `terraform plan` stays "No changes".

**Resize the box:** a `machine_type` change is an in-place stop / resize / start
(`allow_stopping_for_update = true`). The boot disk, the ledger, and both logins are kept.
Do it when `quiver.service` is inactive:
```bash
gcloud compute instances stop quiver --zone us-central1-a
gcloud compute instances set-machine-type quiver --zone us-central1-a --machine-type e2-small
gcloud compute instances start quiver --zone us-central1-a
```
Then set the same `machine_type` in `terraform.tfvars` so `terraform plan` stays "No changes".

## Sizing (measured, Sept-Oct 2026)
Cloud Monitoring `agent.googleapis.com/memory/bytes_used` (state `used`), 28 days:
- **The tick:** the analysis fan-out holds the box near 0.9 GiB. The first minute spikes up
  to about 1.3 GiB above idle. CPU averages 0.1 vCPU. It fits in 2 GiB of RAM.
- **The nightly jobs** (`quiver-claude-update` npm install, `quiver-intel`): up to about
  2.7 GiB on a heavy night. On `e2-small` these swap for some minutes. They run outside
  market hours. The 4G swapfile exists for them.
- **The old desktop** (XFCE + Chrome + Chrome Remote Desktop): about 1 GiB resident and
  swapped, plus about 2 GiB during a human re-auth session. It is gone.

Do not go below 2 GiB (`e2-micro`) without a new measurement. If a tick logs an OOM kill
(`journalctl -k | grep -i oom`), resize up with the commands above.

## Ledger backup (nightly, Cloud Storage)
`state/ledger.db` is the only copy of the decisions, the outcomes, the orders and the targets.
`quiver-backup.timer` runs `deploy/runner/backup_ledger.py` each day at 22:15 UTC. The job
takes a consistent snapshot (SQLite online backup API), runs `integrity_check`, gzips it and
uploads it as `gs://quiver-state-<project id>/ledger/<UTC time>.db.gz`. Each object is
create-only. A failure sends one Telegram page and does not stop a tick.

One-time setup of the bucket (out-of-band, not Terraform):
```bash
P=$(gcloud config get-value project); B=gs://quiver-state-$P; SA=quiver-box@$P.iam.gserviceaccount.com
gcloud storage buckets create $B --location us-central1 --default-storage-class STANDARD \
  --uniform-bucket-level-access --public-access-prevention
gcloud storage buckets update $B --versioning
# Create + read, NO delete: a faulty box cannot remove backup history.
gcloud storage buckets add-iam-policy-binding $B --member serviceAccount:$SA --role roles/storage.objectCreator
gcloud storage buckets add-iam-policy-binding $B --member serviceAccount:$SA --role roles/storage.objectViewer
```
Set `QUIVER_BACKUP_BUCKET` in `/etc/quiver/quiver.env` only for a different bucket name.

```bash
sudo systemctl start quiver-backup.service && tail -1 /var/log/quiver/backup.log   # one backup now
gcloud storage ls -l gs://quiver-state-$P/ledger/ | tail -3                         # the newest copies
gcloud storage cp gs://quiver-state-$P/ledger/<name>.db.gz - | gunzip > ledger.db  # restore
```
Stop `quiver.timer` before you replace the live ledger with a restored copy.

## Controls
- **Stop trading now:** IAP SSH in, `sudo -u quiver touch /opt/quiver/KILL` (or `sudo
  systemctl stop quiver.timer`).
- **Logs:** `journalctl -u quiver.service -f` / `tail -f /var/log/quiver/tick.log`; Cloud
  Logging log `quiver_tick` (where the alert policies key).
- **Admin shell:** `gcloud compute ssh quiver --zone us-central1-a --tunnel-through-iap`.
- **Robinhood login:** `./deploy/gcp/rh-reauth.sh` (section 4).
- **Cost:** e2-small ~$12 + 20 GB disk ~$2 + external IPv4 ~$4 ≈ **~$18/mo**.
