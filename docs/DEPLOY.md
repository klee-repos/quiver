# Quiver Desk — GCP deployment (Compute Engine, headless)

Quiver Desk runs on one hardened **`e2-medium` Ubuntu 24.04 (x86_64)** Compute Engine VM
(4 GiB of RAM). Four desk services run on it:

| Unit | Program | Starts | What it does |
|---|---|---|---|
| `quiver-chat.service` | `deploy/runner/chat_bridge.py` | always on | The Telegram long poll, the desk agent, and the button taps. |
| `quiver-worker.path` → `quiver-worker.service` | `deploy/runner/run_worker.py` | while `state/triggers/worker` exists | The analyst jobs and the news classifier. |
| `quiver-broker.path` → `quiver-broker.service` | `deploy/runner/run_broker.py` | while `state/triggers/broker` exists | Snapshot and execute jobs. The only process with Robinhood access. |
| `quiver.service` + `quiver.timer` | `deploy/runner/run_monitor.py` | Monday to Friday, hourly at :40, 11:40 to 23:40 UTC | The monitor: EDGAR, news, levels, drift, the close brief. No LLM, no broker. |

Two housekeeping services also run: `quiver-backup.timer` (the nightly ledger backup, 22:15
UTC) and `quiver-claude-update.timer` (the daily Claude Code update, 05:00 UTC). The update
skips the day while the worker or the broker runs. After an update it restarts the chat
bridge onto the new binary.

Secrets live in **Secret Manager**. The **Google Cloud Ops Agent** ships
`/var/log/quiver/tick.log` to Cloud Logging, where log-based metrics drive Monitoring alert
policies. The Telegram pager is the primary alert path. GCE auto-restart and live migration
recover from a host failure. The cost is about **$30 a month** plus your Claude
subscription. No model is billed per token: every Claude process uses the subscription
token.

The box has **no desktop**. It needs two logins, and neither needs a browser on the box:

- **Claude (every model):** a one-year subscription token from `claude setup-token`, stored
  as the `quiver-CLAUDE_CODE_OAUTH_TOKEN` secret.
- **Robinhood (the broker MCP):** an OAuth login with no headless refresh. Only
  `quiver-broker` can read it. Run `./deploy/gcp/rh-reauth.sh` on your laptop when the
  `AUTH_ERROR` page arrives.

Nothing trades without an owner tap in Telegram. The **PreToolUse broker guard**
(`deploy/runner/broker_guard.py`) allows a place call only for a leg that Python reserved
and checked.

> **No public inbound.** The custom VPC denies all ingress by default. The ONLY inbound rule
> is SSH from Google's IAP range (`35.235.240.0/20`), scoped to the box's service account.
> Admin is `gcloud compute ssh --tunnel-through-iap`. The external IP is egress-only.

---

## The two Claude config directories

| Directory | Holds | Used by |
|---|---|---|
| `/opt/quiver/state/claude-config` | the Robinhood MCP OAuth | `quiver-broker` only |
| `/opt/quiver/state/claude-desk` | the desk and analyst session transcripts; no MCP server registered | `quiver-chat`, `quiver-worker` |

Each unit that runs Claude sets `CLAUDE_CONFIG_DIR` with `Environment=`. **Do not set
`CLAUDE_CONFIG_DIR` in `/etc/quiver/quiver.env`.** `EnvironmentFile=` overrides
`Environment=` (systemd.exec(5)), so a value there moves the desk onto the Robinhood OAuth
dir. The offline healthcheck fails when it is set.

## The unit sandbox

All four desk services run with `ProtectSystem=strict`. Only `ReadWritePaths=` is writable.
No unit can write the repo, `config.yaml`, or `KILL`.

| Unit | `ReadWritePaths=` | `InaccessiblePaths=` | `MemoryMax=` |
|---|---|---|---|
| `quiver-chat.service` | `state`, `logs` | `state/claude-config`, `/etc/quiver` | 2048M |
| `quiver-worker.service` | `state`, `logs` | `state/claude-config`, `/etc/quiver` | 1536M |
| `quiver-broker.service` | `state`, `logs`, `/var/log/quiver` | `state/claude-desk`, `/etc/quiver` | 1024M |
| `quiver.service` | `state`, `logs`, `/var/log/quiver` | `state/claude-config`, `state/claude-desk`, `/etc/quiver` | 512M |

(`state` and `logs` are under `/opt/quiver`.) Only the monitor and the broker can write
`/var/log/quiver/tick.log`, so a model process cannot forge the lines that the alerts read.
`InaccessiblePaths=/etc/quiver` does not block `EnvironmentFile=/etc/quiver/quiver.env`:
systemd reads that file before it builds the namespace. Every listed path must exist, or the
unit does not start. `setup.sh` and `update.sh` create them.

---

## 0. Prerequisites

- `gcloud` authed (`gcloud auth login`) and a project with **billing enabled** (set in
  `terraform.tfvars`; see `terraform.tfvars.example`). Terraform 1.5 or later.
- The APIs enabled: compute, secretmanager, iap, logging, monitoring,
  cloudresourcemanager (`gcloud services enable ...`).
- The repo is **public**, so the box clones it over HTTPS. No deploy key is needed.
- Terraform auth: `gcloud auth application-default login`, or (no browser) a short-lived
  token: `export GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token)`.
- An expired laptop login can still print a cached token. Prove the login with a real call:
  `gcloud compute instances describe quiver --zone us-central1-a --format='value(status)'`.
- Claude Code on the box must be 2.1.295 or later (the broker hook's `onFailure: "block"`
  needs it). `setup.sh` installs the latest, and the healthcheck checks the version.

## 1. Seed Secret Manager (out-of-band — never in tfstate)

Make the Claude token on your laptop:

```bash
claude setup-token        # authorize in the browser; it prints sk-ant-oat01-...
```

Copy `.env.example` to `.env` (`chmod 600 .env`) and fill in these keys:

| Key | Required | Read by |
|---|---|---|
| `CLAUDE_CODE_OAUTH_TOKEN` | yes | every Claude process, the update gate |
| `RH_ACCOUNT_NUMBER` | yes | the broker child, `lib/config` |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_IDS` | yes | the chat bridge (the only poller), the outbox, the pages |
| `SEC_USER_AGENT` | yes | `lib/desk/edgar.py` |
| `ALPHA_VANTAGE_API_KEY` | no | `lib/desk/transcripts.py` (25 calls a day on the free key) |
| `TELEGRAM_ALERT_CHAT_IDS` | no | `lib/telegram` (page-only chat ids) |

**Quote `SEC_USER_AGENT`.** It is "Quiver" plus your contact email, so it has a space.
`bootstrap-gcp.sh` sources `.env` with bash. Without quotes, bash runs the email as a
command and the script stops. Write it like this:

```
SEC_USER_AGENT="Quiver you@example.com"
```

Check that `.env` sources cleanly: `bash -c 'set -euo pipefail; set -a; . ./.env; set +a; echo ok'`
prints `ok`. Then push:

```bash
./deploy/gcp/bootstrap-gcp.sh      # .env keys + strategy.yaml -> quiver-<KEY> secrets
```

It pushes a fixed list, not every `.env` key: `RH_ACCOUNT_NUMBER CLAUDE_CODE_OAUTH_TOKEN
TELEGRAM_BOT_TOKEN TELEGRAM_ALLOWED_CHAT_IDS TELEGRAM_ALERT_CHAT_IDS SEC_USER_AGENT
ALPHA_VANTAGE_API_KEY`. It skips an empty key. Then it pushes `strategy.yaml` as
`quiver-STRATEGY_YAML`, which `tick.py desk-seed` reads once.

The model IDs have defaults in the code (`QUIVER_DESK_MODEL=claude-sonnet-5-5`,
`QUIVER_ANALYST_MODEL=claude-opus-5-5`, `QUIVER_CLASSIFIER_MODEL=claude-haiku-5-5`,
`QUIVER_BROKER_MODEL=claude-sonnet-5-5`). Do not put them in `quiver.env` unless you
override one.

## 2. Provision with Terraform

```bash
cd deploy/gcp/terraform
export GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token)   # or ADC login
terraform init && terraform plan && terraform apply
```

The box service account reads only the secrets in `locals.secrets` (`main.tf`). An unlisted
required secret returns 403, and `setup.sh` stops with `FATAL`.

The instance `startup-script` clones the public repo and runs `deploy/gcp/setup.sh`:

- [1/9] apt packages and a 4G swapfile;
- [2/9] and [3/9] Node 24 and the `claude` CLI;
- [5/9] the `quiver` user and the venv;
- [6/9] the secrets → `/etc/quiver/quiver.env` (each as `KEY="value"`, plus `CLAUDE_BIN`;
  never `CLAUDE_CONFIG_DIR`); the directories `state/claude-config`, `state/claude-desk`,
  `state/triggers`, `state/tmp`, `state/.cache`, `state/.npm-cache`, `logs/desk` (mode 700,
  owner `quiver`); the `robinhood-trading` MCP server under `state/claude-config` only;
- [7/9] the eleven unit files, with `enable --now` for `quiver.timer`,
  `quiver-worker.path`, `quiver-broker.path`, `quiver-claude-update.timer`,
  `quiver-backup.timer`, and `quiver-chat.service`;
- [8/9] the Ops Agent;
- [9/9] the offline healthcheck.

[6/9] stops when a required secret is missing. The Telegram secrets are required: the chat
is not optional.

**Click the GCP Monitoring verification email** that Google sends to `alert_email`. The
email channel is unverified until you confirm it. The alert policies key on three
`tick.log` patterns: `AUTH_ERROR`, `daily_halt`, and `stage_error`. They are a second net
behind the Telegram pages.

## 3. Verify the box

IAP-tunneled SSH is the ONLY shell. The principal needs `roles/iap.tunnelResourceAccessor`
and an OS Login role. A project Owner has both.

```bash
gcloud compute ssh quiver --zone us-central1-a --tunnel-through-iap
sudo tail -50 /var/log/quiver-startup.log          # the provisioning log
```

On the box, define a helper that runs a command as `quiver` with the env loaded:

```bash
q() { sudo -u quiver bash -c "cd /opt/quiver && set -a && . /etc/quiver/quiver.env && set +a && $*"; }
L=/opt/quiver/state/ledger.db
```

Then:

```bash
q .venv/bin/python deploy/runner/healthcheck.py          # offline checks -> "ok": true
q .venv/bin/python tick.py send-test --kind auth_error   # sends one real page -> "sent": true
```

`healthcheck.py --live` adds the live checks. The units are active and the walls are in
place. The ledger is seeded. Each model ID answers `AUTH_OK` under `claude-desk`, and
`claude-desk` has no Robinhood server. The Robinhood login is connected. EDGAR and Telegram
`getMe` answer. Run it after the seed (section 5) and after each re-auth:

```bash
q .venv/bin/python deploy/runner/healthcheck.py --live   # -> "ok": true
```

## 4. Log in to Robinhood (from your laptop)

```bash
./deploy/gcp/rh-reauth.sh            # log in, then print the server status
./deploy/gcp/rh-reauth.sh --status   # print the server status only
```

The script opens an IAP SSH session and runs `claude mcp login robinhood-trading
--no-browser` on the box as `quiver`, with `CLAUDE_CONFIG_DIR=/opt/quiver/state/claude-config`.
Then:

1. The box prints a Robinhood authorization URL. Open it in a browser on your laptop. Log in
   and approve.
2. The browser shows a connection error for `http://localhost:<port>/callback?code=...`.
   This is correct. Nothing listens on that port on your laptop.
3. Copy the full address of that page. Paste it at the prompt in the terminal.

The script ends with `OK: robinhood-trading is connected`. The login stays under
`/opt/quiver/state/claude-config`. **Only `quiver-broker` reads it.** The chat, the worker,
and the monitor cannot see that directory.

**Repeat this when the `AUTH_ERROR` page arrives.** The Robinhood OAuth expires every few
days and has no headless refresh. When it lapses, the broker runbook calls `tick.py
auth-stop`. The runner writes an `AUTH_ERROR` line to `tick.log`, sends the `auth_error`
page, and places nothing. `/status` in the chat shows the login state.

## 5. Seed the desk and take the first snapshot

Seed once (`desk-seed` is idempotent: a second run changes nothing). If a company load fails,
it prints `"ok": false` with a `failed` list and exits 1; run it again until it prints `"ok": true`:

```bash
q .venv/bin/python tick.py desk-seed
```

It writes the companies, the SEC overrides, premise version 1 (from `strategy.yaml` and
`STRATEGY.md`), the `desk_settings` start values, and the old chat threads. It loads each
holding (filing baselines, so the first monitor run creates no flood of events), queues one
deep-dive per holding, and sends the intro message. It can take some minutes (EDGAR at 5
requests a second).

Take a snapshot. This proves the broker path and reads the Robinhood approval setting:

```bash
sudo -u quiver sqlite3 $L "INSERT INTO broker_jobs(kind,status,requested_by,created_at) VALUES('snapshot','queued','operator',strftime('%Y-%m-%dT%H:%M:%SZ','now'));"
sudo -u quiver sh -c 'date -u +%FT%TZ > /opt/quiver/state/triggers/broker'
journalctl -u quiver-broker.service -f       # until the run exits
sudo -u quiver sqlite3 -readonly $L "SELECT captured_at,equity,buying_power,approval_setting FROM broker_snapshots ORDER BY id DESC LIMIT 1;"
```

The row is new and the equity matches the app. `approval_setting` must be `off`. If it is
`on`, turn it off in the Robinhood app (Agent Settings → Safety Controls), then run the
snapshot job again. `exec-prepare` refuses every execute job unless the latest snapshot
shows `off`.

Then run one monitor run: `sudo systemctl start quiver.service`, and check that
`tail -2 /var/log/quiver/tick.log` shows `"event": "monitor_ok"` and no `stage_error`.

---

## Updating the running box (code / secrets)

**Code:** run `./deploy/gcp/sync.sh` on your laptop. It refuses a commit that references an
untracked file (`tests/check_tracked_refs.py --rev HEAD`), pushes `main`, then runs
`deploy/gcp/update.sh` on the box over IAP. Only committed and pushed `main` deploys.

`update.sh` holds the deploy lock `/run/quiver-deploy.lock` for its whole run:

- `[0/6]` It stops `quiver-broker.path` and waits until `quiver-broker.service` is inactive.
  It never stops the broker mid-order.
- `[2/6]` and `[3/6]` It checks that the working tree is clean, then fast-forwards. A
  box-side edit of a tracked file blocks the pull.
- `[4/6]` and `[5/6]` It installs the dependencies (only `requirements.lock.txt`), the
  directories, and the units.
- `[6/6]` It runs the offline healthcheck. On a pass, it starts `quiver-broker.path`,
  restarts the chat bridge (unless only `docs/` or `tests/` changed), and restarts
  `quiver.timer`. On a fail, the broker path stays stopped, and nothing is restarted.

After a box-side fix, run `sync.sh` again. When the box is up to date and the broker path is
stopped, `update.sh` runs the healthcheck. On a pass, it starts the broker path.

**A new or rotated secret:** `update.sh` does NOT touch `/etc/quiver/quiver.env`. Only
`setup.sh` writes it, at provision. So a secret change on a live box is three steps:

1. `./deploy/gcp/bootstrap-gcp.sh` pushes the new value to `quiver-<KEY>`.
2. If the key is new, add it to `locals.secrets` in `main.tf` and run `terraform plan` (see
   the Terraform warning below) before `terraform apply`. Or grant it by hand:
   ```bash
   P=$(gcloud config get-value project); SA="quiver-box@$P.iam.gserviceaccount.com"
   gcloud secrets add-iam-policy-binding "quiver-<KEY>" --project="$P" \
     --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor
   ```
3. Refresh `quiver.env` **on the box**. The value goes in double quotes. Never run the
   whole `setup.sh` on a live box.
   ```bash
   sudo bash -c 'K=<KEY>; V=$(gcloud secrets versions access latest --secret=quiver-$K) || exit 1; F=/etc/quiver/quiver.env; grep -v "^$K=" "$F" > "$F.t"; printf "%s=\"%s\"\n" "$K" "$V" >> "$F.t"; chmod 600 "$F.t"; chown quiver:quiver "$F.t"; mv "$F.t" "$F"'
   ```
   Then restart the units that read the key at start. `quiver-chat.service` runs all the
   time: `sudo systemctl restart quiver-chat.service`. The worker, the broker, and the
   monitor read the env each time they start.

**Rotate the Claude token (once a year):** run `claude setup-token` on your laptop, put the
new value in `.env`, then do the three steps above with `<KEY>` = `CLAUDE_CODE_OAUTH_TOKEN`.
Restart the chat bridge, because its desk session stays connected:
`sudo systemctl restart quiver-chat.service`. Then run `healthcheck.py --live`: its
`desk_auth` check proves each model ID. When the token is dead, the chat answers "My Claude
login stopped working." and the bridge pages `auth_error` (stage `desk`).

**⚠️ Terraform and the LIVE box — do not casually `apply`:** the instance has
`lifecycle { prevent_destroy = true, ignore_changes = [boot image, shielded_instance_config] }`.
The boot image comes from the `ubuntu-2404-lts-amd64` *family* (the latest). Without
`ignore_changes`, each new Ubuntu image forces a full instance replace on the next `apply`.
A replace wipes the ledger and both logins. `prevent_destroy` makes such an `apply` stop with
an error. ALWAYS run `terraform plan` and confirm that no instance is replaced before
`apply`. A secret IAM binding made by hand with `gcloud` must be imported
(`terraform import`), or `terraform plan` does not say "No changes".

## Resize the box

A `machine_type` change is an in-place stop, resize, and start
(`allow_stopping_for_update = true`). The boot disk, the ledger, and both logins stay. Do it
when `quiver-broker.service` is inactive. With Terraform: set `machine_type` in
`terraform.tfvars`, run `terraform plan`, and confirm the instance shows `~ update in-place`
with no `must be replaced`. Or by hand:

```bash
gcloud compute instances stop quiver --zone us-central1-a
gcloud compute instances set-machine-type quiver --zone us-central1-a --machine-type e2-medium
gcloud compute instances start quiver --zone us-central1-a
```

Then set the same `machine_type` in `terraform.tfvars`, so `terraform plan` says "No
changes". On boot, the startup script starts `quiver.timer`, and the enabled units start.

## Sizing

The box is `e2-medium` (4 GiB). The desk session stays resident in `quiver-chat`, so a
reaction lands fast. An Opus analyst run and a broker run can overlap with it. The Agent SDK
hosting guide plans about 1 GiB per agent process. Three Claude processes do not fit in
2 GiB (`e2-small`).

Each unit has a memory limit: `quiver-chat` 2048M, `quiver-worker` 1536M, `quiver-broker`
1024M, `quiver` 512M. The 4G swapfile absorbs a peak above that, for example the nightly
npm update. Do not go below 4 GiB without a new measurement. If a unit logs an OOM kill
(`journalctl -k | grep -i oom`), resize up with the commands above.

## Ledger backup (nightly, Cloud Storage)

`state/ledger.db` holds the premise, the book, the theses, the research memory, the
proposals, the orders, and the chat. `quiver-backup.timer` runs
`deploy/runner/backup_ledger.py` each day at 22:15 UTC. The job takes a consistent snapshot
(the SQLite online backup API), runs `integrity_check`, gzips it, and uploads it as
`gs://quiver-state-<project id>/ledger/<UTC time>.db.gz`. Each object is create-only. A
failure sends one Telegram page.

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
```

**Restore.** Stop the four desk services first. Never stop the broker mid-order:

```bash
sudo -u quiver touch /opt/quiver/KILL
sudo systemctl stop quiver-broker.path quiver-worker.path quiver.timer
while systemctl is-active --quiet quiver-broker.service; do sleep 5; done
sudo systemctl stop quiver-chat.service quiver-worker.service quiver.service
gcloud storage cp gs://quiver-state-$P/ledger/<name>.db.gz - | gunzip > /tmp/ledger.restore.db
sudo install -o quiver -g quiver -m 644 /tmp/ledger.restore.db /opt/quiver/state/ledger.db
sudo systemctl start quiver-chat.service quiver-worker.path quiver-broker.path quiver.timer
```

Check the restored ledger in the chat (`/status`) before you `rm /opt/quiver/KILL`.

---

## Cutover from the old bot (one time)

**When:** the same day the build passes, in one sitting. Start after the 16:00 ET close
(20:00 UTC in EDT, 21:00 UTC in EST), after the old bot's last tick of the day. The `KILL`
file from S2 to S20 blocks every execution. Use the `q` helper and `L` from section 3.

**Before the switch**

- **F1 (laptop).** `grep -x 'claude-agent-sdk==0.2.174' requirements.lock.txt` matches.
  Merge the desk build to `main` as one merge commit. `tests/run_e2e.sh` and
  `.venv/bin/python tests/check_tracked_refs.py --rev HEAD` pass. Write down
  `git rev-parse origin/main` as `PRE` before the push.
- **F2 (laptop).** Quote `SEC_USER_AGENT` in `.env`. Then
  `bash -c 'set -euo pipefail; set -a; . ./.env; set +a; echo ok'` prints `ok`.
- **F3 (laptop).** `gcloud auth login`, then
  `gcloud compute instances describe quiver --zone us-central1-a --format='value(status)'`
  prints `RUNNING`.
- **F4 (box).** After the last in-session tick (19:40 UTC in EDT, 20:40 UTC in EST),
  reconcile the old bot. Each query returns no rows:
  ```bash
  sudo -u quiver sqlite3 -readonly $L "SELECT ref_id,trade_date,ticker,side,state FROM orders WHERE finalized=0 AND COALESCE(order_kind,'')!='protective_stop';"
  sudo -u quiver sqlite3 -readonly $L "SELECT ref_id,ticker,fill_state FROM orders WHERE broker_order_id IS NOT NULL AND (fill_state IS NULL OR fill_state NOT IN ('filled','cancelled','rejected','failed','voided','not_found'));"
  sudo -u quiver sqlite3 -readonly $L "SELECT * FROM run_lock;"
  ```
  If a row shows while the session is open, run one old tick
  (`q .venv/bin/python deploy/runner/run_tick.py`) and query again. If a row stays, do not
  cut over.
- **F5 (laptop).** `./deploy/gcp/rh-reauth.sh --status`. If it shows expired, log in again
  at S15.
- **F6 (phone).** Turn on Telegram two-step verification.

**The switch**

- **S1 (box) Back up.** `sudo systemctl start quiver-backup.service && tail -1 /var/log/quiver/backup.log`
  shows an upload. Then `sudo -u quiver sqlite3 $L ".backup /opt/quiver/state/ledger.pre-desk.db"`.
- **S2 (box) Stop the old bot.** `sudo -u quiver touch /opt/quiver/KILL`;
  `sudo systemctl disable --now quiver.timer quiver-intel.timer quiver-chat.service`;
  `systemctl is-active quiver.service quiver-intel.service` prints `inactive` twice.
- **S3 (box) Check the tree and the seed inputs.**
  `sudo -u quiver git -C /opt/quiver status --porcelain --untracked-files=no` prints nothing.
  `strategy.yaml` and `STRATEGY.md` exist. Diff the box `strategy.yaml` against the laptop
  copy. If they differ, put the one that seeds the premise on the box before S10.
- **S4 (laptop) Push the secrets.** `./deploy/gcp/bootstrap-gcp.sh` prints
  `quiver-SEC_USER_AGENT  <- set` and `quiver-ALPHA_VANTAGE_API_KEY  <- set`.
- **S5 (laptop) Resize and grant.** Import the two existing Telegram bindings, then plan:
  ```bash
  cd deploy/gcp/terraform
  export GOOGLE_OAUTH_ACCESS_TOKEN=$(gcloud auth print-access-token)
  P=$(gcloud config get-value project); SA="quiver-box@$P.iam.gserviceaccount.com"
  for S in TELEGRAM_BOT_TOKEN TELEGRAM_ALLOWED_CHAT_IDS; do
    terraform import "google_secret_manager_secret_iam_member.accessor[\"$S\"]" \
      "projects/$P/secrets/quiver-$S roles/secretmanager.secretAccessor serviceAccount:$SA"
  done
  terraform plan -out=desk.tfplan
  terraform show -json desk.tfplan | jq -r '.resource_changes[] | select(.change.actions != ["no-op"]) | "\(.change.actions | join(",")) \(.address)"'
  terraform show -json desk.tfplan | jq -e '[.resource_changes[] | select(.change.actions | index("delete"))] | length == 0'
  terraform show -json desk.tfplan | jq -r '.resource_changes[] | select(.address == "google_compute_instance.quiver") | .change | "\(.before.machine_type) -> \(.after.machine_type)"'
  ```
  Apply (`terraform apply desk.tfplan`) only if all four checks pass. The first command
  prints exactly the five lines below (any order). The second prints `true`. The third
  prints `e2-small -> e2-medium`. The text plan shows the instance as `~ update in-place`
  with no `must be replaced`:
  ```
  update google_compute_instance.quiver
  update google_logging_metric.f["daily_halt"]
  update google_logging_metric.f["tick_error"]
  create google_secret_manager_secret_iam_member.accessor["ALPHA_VANTAGE_API_KEY"]
  create google_secret_manager_secret_iam_member.accessor["SEC_USER_AGENT"]
  ```
  Wait until `gcloud compute instances describe quiver --zone us-central1-a --format='value(status,machineType)'`
  shows `RUNNING` and `e2-medium`.
- **S6 (box).** The startup script started the old timer: `sudo systemctl stop quiver.timer`.
  `free -g` shows about 4 GiB.
- **S7 (box) Refresh `quiver.env`.** Add the two new keys, keep a copy for the rollback,
  delete the retired lines and `CLAUDE_CONFIG_DIR`, and check:
  ```bash
  sudo bash -c 'K=SEC_USER_AGENT; V=$(gcloud secrets versions access latest --secret=quiver-$K) || exit 1; F=/etc/quiver/quiver.env; grep -v "^$K=" "$F" > "$F.t"; printf "%s=\"%s\"\n" "$K" "$V" >> "$F.t"; chmod 600 "$F.t"; chown quiver:quiver "$F.t"; mv "$F.t" "$F"'
  sudo bash -c 'K=ALPHA_VANTAGE_API_KEY; V=$(gcloud secrets versions access latest --secret=quiver-$K) || exit 1; F=/etc/quiver/quiver.env; grep -v "^$K=" "$F" > "$F.t"; printf "%s=\"%s\"\n" "$K" "$V" >> "$F.t"; chmod 600 "$F.t"; chown quiver:quiver "$F.t"; mv "$F.t" "$F"'
  sudo cp -p /etc/quiver/quiver.env /etc/quiver/quiver.env.pre-desk
  sudo sed -i -E '/^(OPENROUTER_API_KEY|GLM_API_KEY|DEEPSEEK_API_KEY|RESEND_API_KEY|RESEND_FROM|NOTIFY_TO|NOTIFY_ALERTS_TO|CONGRESS_API_KEY|ANTHROPIC_API_KEY|CLAUDE_CONFIG_DIR|QUIVER_MODEL|QUIVER_EFFORT|QUIVER_TICK_TIMEOUT_SEC|QUIVER_CHAT_[A-Z_]+)=/d' /etc/quiver/quiver.env
  sudo chown quiver:quiver /etc/quiver/quiver.env && sudo chmod 600 /etc/quiver/quiver.env
  sudo -u quiver bash -c 'set -euo pipefail; set -a; . /etc/quiver/quiver.env; set +a; for k in CLAUDE_CODE_OAUTH_TOKEN RH_ACCOUNT_NUMBER TELEGRAM_BOT_TOKEN TELEGRAM_ALLOWED_CHAT_IDS SEC_USER_AGENT CLAUDE_BIN; do [ -n "${!k:-}" ] || { echo "MISSING $k"; exit 1; }; done; [ -z "${CLAUDE_CONFIG_DIR:-}" ] && echo env-ok'
  ```
  The last line prints `env-ok`.
- **S8 (laptop) Deploy.** `./deploy/gcp/sync.sh` ends with `DONE — updated <PRE> -> <new>`
  and `"ok": true`. On a failure, stop and roll back. Then on the box run
  `sudo systemctl stop quiver.timer`: `update.sh` restarts it, and the monitor must not run
  before the seed.
- **S9 (box) Check the units.**
  ```bash
  systemctl list-unit-files 'quiver*'      # no quiver-intel; both .path units enabled
  sudo systemd-analyze verify /etc/systemd/system/quiver*.{service,path,timer}
  systemd-analyze calendar --iterations=3 'Mon..Fri *-*-* 11..23:40:00 UTC'
  ls -ld /opt/quiver/state/triggers /opt/quiver/state/claude-desk   # drwx------ quiver quiver
  test ! -e /opt/quiver/quiver_eve && echo eve-gone
  ```
- **S10 (box) Seed.** `q .venv/bin/python tick.py desk-seed` (section 5). Run it again: the
  second run changes nothing. If a load fails, it prints `"ok": false` with a `failed` list and
  exits 1: run it again until it prints `"ok": true` before S11.
- **S11 (box) Start the bridge and the timer.**
  `sudo systemctl enable --now quiver-chat.service quiver.timer`;
  `journalctl -u quiver-chat.service -n 30` shows the poll loop and no error.
- **S12 (box).** `q .venv/bin/python deploy/runner/healthcheck.py --live` prints `"ok": true`.
- **S13 (box) Prove the trigger path and start the deep-dives.**
  `sudo -u quiver sh -c 'date -u +%FT%TZ > /opt/quiver/state/triggers/worker'`. Within a few
  seconds `systemctl is-active quiver-worker.service` prints `active`, and
  `test ! -e /opt/quiver/state/triggers/worker && echo consumed` prints `consumed`.
- **S14 (box) Prove the wall in the running sandbox.**
  ```bash
  PID=$(systemctl show -p MainPID --value quiver-chat.service)
  sudo nsenter -t "$PID" -m -- ls -A /opt/quiver/state/claude-config | wc -l   # 0
  sudo nsenter -t "$PID" -m -- ls -A /etc/quiver | wc -l                        # 0
  sudo nsenter -t "$PID" -m -- sh -c 'test -w /opt/quiver/state/ledger.db && echo state-rw'
  sudo nsenter -t "$PID" -m -- sh -c 'test -w /opt/quiver/config.yaml || echo repo-ro'
  sudo nsenter -t "$PID" -m -- sh -c 'test -w /var/log/quiver/tick.log || echo ticklog-ro'
  ```
  `ls` runs as root, so an empty listing proves that the mount hides the files.
- **S15 (box) Robinhood and the approval setting.** The first snapshot of section 5.
  `./deploy/gcp/rh-reauth.sh --status` first; log in if it shows expired.
- **S16 (box) First monitor run.** `sudo systemctl start quiver.service`; then
  `journalctl -u quiver.service -n 60` and `tail -2 /var/log/quiver/tick.log` show one JSON
  line with `"event": "monitor_ok"`, the S10 name count, and no `stage_error`.
- **S17 (phone).** The intro message from S10 is in the chat. `chat_messages` holds it with
  `source='system'`.
- **S18 (phone).** Send "hi, what do we own?". The desk reacts, then answers with the S15
  holdings. Send `/status`. Then
  `sudo -u quiver sqlite3 -readonly $L "SELECT role,source,reaction,substr(text,1,60) FROM chat_messages ORDER BY id DESC LIMIT 6;"`
  shows your message with its reaction.
- **S19 (phone) Deep-dives.**
  `sudo -u quiver sqlite3 -readonly $L "SELECT status,COUNT(*) FROM jobs WHERE kind='deep_dive' GROUP BY 1;"`
  shows one per holding except SGOV. They run one at a time
  (`journalctl -u quiver-worker.service -f`). Each ends with a `set_thesis` card. Confirm
  it: change proposals work under `KILL`.
- **S20 (box).** `sudo -u quiver rm /opt/quiver/KILL`, then the live healthcheck again.
- **S21 (next weekday).** Watch the 11:40 UTC run (`tail -f /var/log/quiver/tick.log`), the
  first in-session snapshot, and the close brief.
- **S22 (first proposal).** Approve one small proposal in the session. The fills message
  arrives, and `orders` shows `filled` on the reserved `ref_id`.
- **S23 (10th trading day, no rollback).** Remove the six rollback names from
  `locals.secrets`. `terraform plan` shows exactly six `delete ...accessor[...]` lines and
  no instance change. Apply. `sudo rm /etc/quiver/quiver.env.pre-desk /opt/quiver/state/ledger.pre-desk.db`.
  Revoke the OpenRouter key at openrouter.ai. Then
  `for S in OPENROUTER_API_KEY GLM_API_KEY DEEPSEEK_API_KEY RESEND_API_KEY RESEND_FROM NOTIFY_TO NOTIFY_ALERTS_TO CONGRESS_API_KEY; do gcloud secrets delete "quiver-$S" --quiet || true; done`
  (a secret that never existed only prints an error).

**Rollback** (a failure from S8 to S22):

1. Box: `sudo -u quiver touch /opt/quiver/KILL`. Wait until
   `systemctl is-active quiver-broker.service` prints `inactive` (never stop it mid-order).
   Then `sudo systemctl disable --now quiver-chat.service quiver-worker.path quiver-broker.path quiver.timer`.
2. Box: `sudo cp -p /etc/quiver/quiver.env.pre-desk /etc/quiver/quiver.env`.
3. Laptop: `git revert --no-edit -m 1 <desk merge commit>`, then `./deploy/gcp/sync.sh`. The
   old `update.sh` reinstalls the old units (intel included), runs `npm ci` in
   `quiver_eve/`, and gates on the old healthcheck.
4. Box: the old `update.sh` does not know the new units:
   `sudo rm -f /etc/systemd/system/quiver-{worker,broker}.{path,service} && sudo systemctl daemon-reload`.
5. Box, the ledger. If no desk order left the box (`SELECT COUNT(*) FROM orders WHERE
   proposal_id IS NOT NULL AND state NOT IN ('proposed','skipped','expired','requoted')` is
   0), restore: `sudo -u quiver cp /opt/quiver/state/ledger.pre-desk.db $L`. Otherwise keep
   it, run
   `UPDATE orders SET finalized=1 WHERE proposal_id IS NOT NULL AND finalized=0 AND state IN ('proposed','approved','requoted','expired','skipped');`,
   and check each other desk leg (`checked`, `placing`, `placed`, `outcome_unknown`) in the
   Robinhood app. The old code ignores the new tables and columns.
6. Box: `sudo systemctl enable --now quiver.timer quiver-chat.service`;
   `sudo -u quiver rm /opt/quiver/KILL`. Keep `e2-medium`: the old code runs on it unchanged.

---

## Controls

- **Stop all execution now:** IAP SSH in, then `sudo -u quiver touch /opt/quiver/KILL`. The
  broker places nothing, and an Approve tap is refused. Research, the chat, and change
  proposals still work. `sudo -u quiver rm /opt/quiver/KILL` resumes. No unit can write or
  delete `KILL`: only you can.
- **The daily-loss halt:** each snapshot compares the equity with the day's opening
  baseline. A drop of `daily_loss_halt_pct` (20% to start) or more halts execution for the
  rest of the day. The broker runner writes a `daily_halt` line to `tick.log` and sends a
  `halt` page. The halt does not write `KILL`. To lift it the same
  day, ask the desk to propose `resume_trading`, then tap **Confirm**. Do not shrink the 20%
  start value.
- **Pause the broker only:** `sudo systemctl stop quiver-broker.path`. No snapshot or
  execute job starts. `exec-prepare` refuses an approved trade more than 10 minutes after
  its tap. `sudo systemctl start quiver-broker.path` resumes.
- **Logs:**
  ```bash
  journalctl -u quiver-chat.service -f       # the chat bridge
  journalctl -u quiver-worker.service -f     # the analyst jobs
  journalctl -u quiver-broker.service -f     # snapshot and execute jobs
  journalctl -u quiver.service -f            # the monitor runs
  tail -f /var/log/quiver/tick.log           # one status line per monitor or broker run
  tail -f /opt/quiver/logs/desk/bridge.log   # the bridge events
  ```
  In Cloud Logging, the log `quiver_tick` holds the `tick.log` lines (the alert policies key
  on it).
- **Admin shell:** `gcloud compute ssh quiver --zone us-central1-a --tunnel-through-iap`.
- **Robinhood login:** `./deploy/gcp/rh-reauth.sh` (section 4).
- **Cost:** e2-medium about $24 + 20 GB disk about $2 + external IPv4 about $4 = about
  **$30 a month**.
