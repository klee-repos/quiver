# Chat with Quiver over Telegram (read-only)

Ask the bot about itself from your phone — "how did today's run go?", "what did it do?",
"what's the current strategy?", "why did it buy NVDA?", "are we halted?" — and get an answer
built from the ledger, strategy, and logs. It is the same reasoning you'd get in a Claude Code
session, wrapped in a text-message front-end.

It is **strictly read-only**: it can look at everything but **cannot trade, move money, send
email, or change any file**. It runs on the box as `deploy/quiver-chat.service`.

## Why Telegram (not SMS)

The box has **no public inbound** (IAP-only firewall). Telegram's bot API is *long-polled* —
the box reaches OUT to Telegram over HTTPS (the same egress that already talks to the model and
Resend), so **no public endpoint, no phone number, and no A2P registration** are needed. SMS
would require a public webhook + Twilio + carrier registration; Telegram needs none of that.

## The security model — five independent walls

1. **No broker, structurally.** The chat assistant is launched with `--strict-mcp-config` and an
   empty `chat_mcp.json`, which overrides the box's user-scope Robinhood OAuth server. It loads
   **zero MCP servers** — there is literally no order tool to call.
2. **A fail-closed, argument-aware read-only guard.** A `PreToolUse` hook (`chat_guard.py`, wired
   by `chat_settings.json`) runs before *every* tool call and denies anything that writes,
   mutates, hits the network, reads a secret (`.env`, tokens, `claude-config`, `/proc/*/environ`,
   …), or is a Bash command not on a read-only allowlist (a SELECT-only `chat_query.py` ledger
   reader, read-only `tick.py` subcommands, `git log`, `cat`/`grep`/`tail`, …). It does not try to
   *parse* arbitrary shell (a hand-written parser inevitably diverges from bash) — it *eliminates*
   the dangerous constructs: control chars, backslashes, `$`/backtick substitution, globbing,
   subshells, redirection, and write/exec flags (`find -exec`, `sort -o`, `sed -i`) are all
   rejected outright. The raw `sqlite3` CLI is banned too (its `writefile()`/`load_extension()` SQL
   functions bypass `-readonly`); ledger reads go through a wrapper on the sqlite3 *module*, where
   those functions don't exist. Same hook mechanism as the trading tick's `order_guard.py`.
   Unit-tested in `tests/test_chat_guard.py`.
3. **A scrubbed child environment.** The bridge spawns `claude` with an explicit minimal `env=`
   (PATH/HOME/CLAUDE_CONFIG_DIR/Claude-auth only) — the trading secrets in the service's env
   (GLM/RESEND/`RH_ACCOUNT_NUMBER`/`TELEGRAM_BOT_TOKEN`/…) **never reach the assistant's process**,
   so even a hypothetical guard slip or `/proc/self/environ` read finds nothing to leak.
4. **OS-level read-only.** The systemd unit runs `ProtectSystem=strict` with `ReadWritePaths`
   limited to the Claude OAuth store, the npm cache, and the chat's **own** log dir — so the
   ledger (`state/ledger.db`, a `journal_mode=delete` DB that reads with zero writes), the whole
   repo, and the tick's log dirs are read-only to the process at the kernel level.
5. **An allowlist of who can talk to it.** Only chat IDs in `TELEGRAM_ALLOWED_CHAT_IDS` get
   answers; everyone else is ignored (told only their own chat id, once, so you can pair).

These walls were hardened across **seven adversarial audit rounds** (multi-agent) that found and
fixed ~28 issues — multiple RCEs (`find -exec`, newline/backslash shell-injection, sqlite
`writefile()`/`load_extension()`), secret exfils (`/proc/environ`, `Grep /etc`, `cat ~/.netrc`),
and file-write primitives (`sort -o`, `uniq` positional output, `git branch/tag/remote`). The
recurring lesson — **don't denylist or parse; eliminate and contain** — is why the guard bans whole
character classes, confines every file read to the repo by realpath, routes the ledger through the
sqlite3 *module*, and leans on the systemd read-only filesystem as the write-guarantee. All fixes
are pinned by regression tests in `tests/test_chat_guard.py`.

**Optional extra hardening:** the chat reuses the box's Claude login (`CLAUDE_CONFIG_DIR`), which
also holds the (strict-mode-excluded) Robinhood OAuth. For maximum isolation you can give the chat
bridge its **own** `CLAUDE_CONFIG_DIR` with a separate `claude login` and no broker MCP registered
— at the cost of a second ~weekly re-auth to maintain. The layered walls above make this optional.

One caveat worth knowing: Telegram cloud chats are TLS in transit but **not end-to-end
encrypted**, and answers include your positions/P&L (from the ledger). That's the normal tradeoff
for a bot front-end; if that matters to you, keep the chat to non-sensitive questions.

## One-time setup

### 1. Create the bot + get your chat id

1. In Telegram, message **@BotFather** → `/newbot` → pick a name + username. It gives you a
   **bot token** like `123456789:AA...`. Keep it secret.
2. Open a chat with your new bot and send it any message (e.g. `hi`).
3. Get your **numeric chat id**: easiest is to just deploy with an *empty* allowlist first and
   send the bot a message — it replies "Not authorized. Your chat id is `NNN`…". Or message
   **@userinfobot**, which echoes your id. That number is `TELEGRAM_ALLOWED_CHAT_IDS`.

### 2. Put the secrets in `.env`, then push them

Add to your local `.env` (gitignored):

```
TELEGRAM_BOT_TOKEN=123456789:AA...        # from BotFather
TELEGRAM_ALLOWED_CHAT_IDS=NNNNNNNNN       # your chat id (comma-separated for more than one)
```

Push both to Secret Manager:

```bash
./deploy/gcp/bootstrap-gcp.sh     # picks up the two new keys automatically
```

Grant the box's service account read access to the two new secrets (least-privilege; they're
optional so they're not in the terraform `local.secrets` list):

```bash
SA="quiver-box@eighth-duality-354701.iam.gserviceaccount.com"   # confirm with: gcloud iam service-accounts list
for S in TELEGRAM_BOT_TOKEN TELEGRAM_ALLOWED_CHAT_IDS; do
  gcloud secrets add-iam-policy-binding "quiver-$S" \
    --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor
done
```

### 3. Get the values + unit onto the box, then enable it

Deploy the code (installs `chat_bridge.py`, the guard, and the unit file):

```bash
./deploy/gcp/sync.sh
```

`quiver.env` is only written at provision time, so on the already-running box add the two keys to
it over IAP SSH (same pattern the CLAUDE.md runbook uses for a rotated key), then enable + start
the service:

```bash
gcloud compute ssh quiver --zone us-central1-a --tunnel-through-iap
# on the box, as root:
sudo bash -c '
  F=/etc/quiver/quiver.env
  for K in TELEGRAM_BOT_TOKEN TELEGRAM_ALLOWED_CHAT_IDS; do
    V=$(gcloud secrets versions access latest --secret=quiver-$K)
    grep -v "^$K=" "$F" > "$F.t"; echo "$K=$V" >> "$F.t"
    chmod 600 "$F.t"; chown quiver:quiver "$F.t"; mv "$F.t" "$F"
  done
  systemctl enable --now quiver-chat.service
  systemctl status quiver-chat.service --no-pager | head -5
'
```

Now message your bot: `how did the last run go?` — it should reply in a few seconds.
(On a *fresh* provision, `setup.sh` does steps 3's env-write + enable automatically once the
secrets exist — no manual refresh needed.)

## Operating it

```bash
# watch it
journalctl -u quiver-chat.service -f              # live daemon output
tail -f /var/log/quiver/chat.log                  # one JSON line per question/answer

# stop / start / disable
sudo systemctl stop quiver-chat.service
sudo systemctl disable --now quiver-chat.service  # turn the feature off entirely

# change who can talk to it: update quiver-TELEGRAM_ALLOWED_CHAT_IDS in Secret Manager,
# refresh that line in /etc/quiver/quiver.env (as above), then: systemctl restart quiver-chat.service
```

Send the bot `/help` for the list of example questions.

## Local smoke test (no Telegram, no token)

```bash
# ask one question against the local ledger (spawns the same sandboxed claude -p):
.venv/bin/python deploy/runner/chat_bridge.py --ask "what were the last 3 decisions?"
# show the exact read-only claude command the bridge runs:
.venv/bin/python deploy/runner/chat_bridge.py --print-cmd
# prove the guard denies writes/secrets and allows reads:
.venv/bin/python tests/test_chat_guard.py
```

## Notes

- **Cost:** each question is one `claude -p` pass (a few read tool-calls) — roughly $0.05–0.30 on
  Sonnet. Set `QUIVER_CHAT_MODEL`/`QUIVER_CHAT_EFFORT` in `quiver.env` to trade cost for depth.
- **Auth sharing:** the bridge reuses the box's Claude login (`CLAUDE_CONFIG_DIR`). If that login
  expires (the ~3.8-day Robinhood re-auth is separate), chat answers error until the next
  `/mcp`/`claude login` on the box — the bridge stays up and recovers automatically.
- **It never touches the trading path:** separate process, separate unit, no shared code with the
  brain; a chat crash just restarts and can't affect a tick.
