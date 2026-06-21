# Quiver — GCP deployment (Compute Engine, on-server-browser OAuth)

The Quiver deploy, on GCP Compute Engine. A single hardened **`e2-medium` Ubuntu 24.04 (x86_64)**
Compute Engine VM runs a **systemd timer** that drives one tick per weekday market-hours
wake by running the `claude` CLI headless over `TICK.md`. Secrets live in **Secret
Manager**; the **Google Cloud Ops Agent** ships the per-tick log to Cloud Logging where
**log-based metrics → Monitoring alert policies** page an email channel. GCE **auto-restart
+ live-migration** is the host-failure recovery. ~**$25–30/mo** + your Claude subscription
+ GLM usage.

The box runs **XFCE + Chrome + Chrome Remote Desktop** for the periodic Robinhood/Claude
OAuth (Robinhood's token fully expires ~every 3.8 days, no headless refresh — re-auth in the
on-box browser). Execution-only: Python owns every decision, and the **PreToolUse order
guard** denies any order whose `ref_id` the Python `plan` did not reserve.

> **No public inbound.** The custom VPC default-denies ingress; the ONLY inbound rule is
> SSH from Google's IAP range (`35.235.240.0/20`) scoped to the box's service account. Admin
> is `gcloud compute ssh --tunnel-through-iap`; Chrome Remote Desktop dials OUT to Google's
> relay. The external IP is egress-only.

---

## 0. Prerequisites
- `gcloud` authed (`gcloud auth login`) and a project with **billing enabled**
  (default `your-gcp-project-id`). Terraform ≥ 1.5.
- Required APIs enabled (compute, secretmanager, iap, logging, monitoring,
  cloudresourcemanager) — `gcloud services enable ...`.
- The repo is **public**, so the box clones over HTTPS — no deploy key / SSH / `gh` needed.
- Terraform auth: either `gcloud auth application-default login`, OR (no browser) export a
  short-lived token: `export GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token)`.

## 1. Seed Secret Manager (out-of-band — never in tfstate)
```bash
./deploy/gcp/bootstrap-gcp.sh      # pushes .env + strategy.yaml -> quiver-<KEY> secrets
```
Creates `quiver-GLM_API_KEY`, `quiver-DEEPSEEK_API_KEY`, `quiver-RH_ACCOUNT_NUMBER`,
`quiver-RESEND_API_KEY`, `quiver-NOTIFY_TO`, `quiver-RESEND_FROM`, `quiver-STRATEGY_YAML`
(+ optional `quiver-NOTIFY_ALERTS_TO` / `quiver-CLAUDE_CODE_OAUTH_TOKEN` if set in `.env`). Claude auth
is OPTIONAL — the on-box `claude login` (step 4) covers it.

## 2. Provision with Terraform
```bash
cd deploy/gcp/terraform
export GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token)   # or ADC login
terraform init && terraform apply
```
The instance `startup-script` clones the public repo and runs `deploy/gcp/setup.sh` (trading
stack: venv, claude CLI, secrets→`/etc/quiver/quiver.env`, systemd timer, Ops Agent) then
`deploy/setup-desktop.sh` (XFCE + Chrome + CRD).

**Click the GCP Monitoring verification email** Google sends to `alert_email` — the email
notification channel is created UNVERIFIED and delivers nothing until confirmed. (The Resend
pager — in-tick + last-resort — is the primary, cloud-agnostic alert path; these Cloud
Monitoring policies are a secondary net.)

## 3. Verify the box (admin via IAP — no public inbound)
IAP-tunneled SSH is the ONLY shell (no public SSH). The principal running it needs
**`roles/iap.tunnelResourceAccessor`** + an OS Login role — a project **Owner** has both
implicitly (so `ops@yourdomain.com` works out of the box); a non-owner operator must be granted
them, or they're locked out.
```bash
gcloud compute ssh quiver --zone us-central1-a --tunnel-through-iap
sudo tail -50 /var/log/quiver-startup.log          # provisioning log
sudo -u quiver bash -c 'set -a; . /etc/quiver/quiver.env; set +a; \
  /opt/quiver/.venv/bin/python /opt/quiver/deploy/runner/healthcheck.py'   # -> ok:true
systemctl status quiver.timer
# PAGER ACCEPTANCE GATE (really sends via Resend):
sudo -u quiver bash -c 'set -a; . /etc/quiver/quiver.env; set +a; \
  /opt/quiver/.venv/bin/python /opt/quiver/tick.py send-test --kind auth_error'  # -> sent:true
```

## 4. Link Chrome Remote Desktop (one-time, human — tied to YOUR Google account)
1. Open <https://remotedesktop.google.com/headless> → "Set up another computer" → Debian
   Linux, copy the `start-host` command (has a one-time `--code="4/..."`).
2. Run it on the box as `quiver` (over IAP SSH), then set a 6-digit PIN:
   ```bash
   sudo -u quiver env HOME=/home/quiver CLAUDE_CONFIG_DIR=/opt/quiver/state/claude-config \
     /opt/google/chrome-remote-desktop/start-host --code="4/PASTE" \
     --redirect-url="https://remotedesktop.google.com/_/oauthredirect" --name=quiver-box
   ```
3. Connect at <https://remotedesktop.google.com/access> with the PIN.

## 5. On-box logins (one-time, then ~every 3.8 days for Robinhood)
In the CRD desktop, open a terminal:
```bash
claude            # sign into your Pro/Max plan (skip if a token was set in Secret Manager)
/mcp              # -> robinhood-trading -> authenticate in the browser
```
Both persist under `CLAUDE_CONFIG_DIR` (`/opt/quiver/state/claude-config`); the quiver
desktop shells export it via `~/.bashrc`/`~/.profile`. **Re-auth Robinhood (`/mcp`) every
~3.8 days** — you're paged (`AUTH_ERROR`) when it lapses; the bot stops, never trades blind.

## 6. Validate broker auth, then live
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

**⚠️ Terraform & the LIVE box — do not casually `apply`:** the instance carries
`lifecycle { prevent_destroy = true, ignore_changes = [boot image, shielded_instance_config] }`.
The boot image resolves from the `ubuntu-2404-lts-amd64` *family* (= latest), so without
`ignore_changes` every new Ubuntu image GCP publishes **forces a full instance replace** on the
next `apply` — wiping the ledger + the on-box Robinhood/Claude OAuth. `prevent_destroy` makes
`apply` **error** rather than recreate. To intentionally rebuild, remove those guards
deliberately (and migrate state first). Secret IAM bindings live in `locals.secrets`; if you
grant one out-of-band with `gcloud`, `terraform import` it (and `terraform state rm` a retired
one whose GCP binding you want to keep as a rollback) so `terraform plan` stays "No changes".

## Controls
- **Stop trading now:** IAP SSH in, `sudo -u quiver touch /opt/quiver/KILL` (or `sudo
  systemctl stop quiver.timer`).
- **Logs:** `journalctl -u quiver.service -f` / `tail -f /var/log/quiver/tick.log`; Cloud
  Logging log `quiver_tick` (where the alert policies key).
- **Admin shell:** `gcloud compute ssh quiver --zone us-central1-a --tunnel-through-iap`.
- **Desktop:** <https://remotedesktop.google.com/access> + PIN.
- **Cost:** e2-medium ~$25 + disk ~$2 + logging ~$1 ≈ **~$28/mo**; a CUD trims the floor.
