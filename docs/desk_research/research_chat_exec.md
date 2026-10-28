# Quiver: chat side and execution side, as built (2026-10-27)

This is a read-only code map. Every claim cites `file:line` in `/Users/kvnlee/dev/quiver`.
Where a claim is inference and not code, it says "inference".
Evidence about the Robinhood MCP tool surface comes from the MCP server's own tool schemas, which
this session loaded (no broker call was made).

---

## 0. Short summary

- The chat bridge is one long-running, single-threaded Python daemon. For each allowlisted
  private message it spawns one new `claude -p` process, waits up to 240 s, and posts the
  `result` text back (`deploy/runner/chat_bridge.py:141-188`, `:335-369`, `:381-408`).
- "Read-only" comes from five walls: zero MCP servers, a fail-closed PreToolUse guard, a scrubbed
  child env, systemd `ProtectSystem=strict`, and a chat-id allowlist (`docs/CHAT.md:18-44`).
  A chat agent with write tools and a path to orders must change four of the five.
- The order path has one mechanical gate: `order_guard.py` lets a place/cancel call through only
  when its `ref_id` exists as a row in `orders` (`deploy/runner/order_guard.py:39-49`). It does
  not check the order body, the row state, or the date, and it only covers 4 tool names.
- No code knows about Robinhood's native trade-approval mode. A grep for `trade_approval`,
  `customer_approval`, `get_trade_approvals`, `decline_trade`, `approval_required` returns zero hits
  in `.py/.md/.json/.yaml/.mjs/.sh/.service` files. If approval mode is on, the current tick would
  record a pending approval as `placed` and then as `filled` (`tick.py:3182`). That is inference
  from the code plus the MCP schema text.
- `lib/telegram.py` can send to any chat at any time (only `sendMessage`). It is the reuse-ready
  base for proactive proposals. No code sends inline keyboards or reads callback queries.

---

## 1. Telegram chat bridge

### 1.1 Files

| File | Role |
|---|---|
| `deploy/runner/chat_bridge.py` (431 lines) | The daemon: long-poll, allowlist, spawn `claude`, reply |
| `deploy/runner/chat_guard.py` (409) | PreToolUse hook. The read-only policy (`evaluate()`) |
| `deploy/runner/chat_history.py` (186) | Per-chat thread memory (JSONL), bounded context block |
| `deploy/runner/chat_query.py` (83) | SELECT-only ledger reader (sqlite3 module, `mode=ro`) |
| `deploy/runner/chat_mcp.json` | `{"mcpServers": {}}`. Zero MCP servers (`:3`) |
| `deploy/runner/chat_settings.json` | Wires `chat_guard.py` as PreToolUse hook, matcher `.*` (`:4-14`) |
| `deploy/quiver-chat.service` | systemd unit, always-on, `ProtectSystem=strict` |
| `prompts/chat.md` | System prompt appended to the child (read-only analyst persona) |
| `docs/CHAT.md` | Operator doc and the security model |
| `tests/test_chat_guard.py`, `tests/test_chat_history.py` | Offline tests, run by `tests/run_e2e.sh:27-28` |

### 1.2 Config (env, read once at import)

- `TELEGRAM_BOT_TOKEN` (`chat_bridge.py:46`), `TELEGRAM_ALLOWED_CHAT_IDS` (comma or space list,
  `:47-50`).
- `CLAUDE_BIN` default `"claude"` (`:51`). On the box `setup.sh` pins it to
  `/opt/quiver/npm-global/bin/claude` (`deploy/gcp/setup.sh:156`).
- Model: `QUIVER_CHAT_MODEL` → `QUIVER_MODEL` → `"claude-sonnet-4-6"` (`chat_bridge.py:52-53`).
  Effort `QUIVER_CHAT_EFFORT` default `medium` (`:54`). Max turns 40 (`:55`). Timeout 240 s (`:57`).
  Long-poll 50 s (`:58`). Max input 2000 chars (`:59`).
- **Env-name collision (latent bug):** the EVE brain reads the SAME variable `QUIVER_CHAT_MODEL` as
  its GLM quick-model slug (`quiver_eve/run/decide.mjs:62`, default `z-ai/glm-4.7-flash`;
  documented in `deploy/runner/runner.env.example:20` and `.env.example:10`). Both services read
  `/etc/quiver/quiver.env`. If an operator sets it for one, the other breaks (`claude --model
  z-ai/glm-4.7-flash`, or GLM asked for `claude-sonnet-4-6`).
- History knobs: `QUIVER_CHAT_HISTORY` (default on), dir `<log dir>/history`, 30 turns, 12000 chars,
  1500 chars per turn, 500 stored turns (`chat_bridge.py:67-73`).

### 1.3 Message flow, end to end

1. **Start.** `run()` exits 0 (idle) without a token or without an allowlist (`:382-388`).
   `_drain()` calls `getUpdates` with `timeout:0` and moves the offset past any backlog, so a
   restart never answers stale messages (`:372-378`, `:391`).
2. **Long-poll loop.** `getUpdates` with `timeout=50`, `allowed_updates=["message"]`, socket timeout
   65 s (`:393-398`). Transport is stdlib `urllib` POST JSON (`_tg`, `:192-211`). Errors return
   `None` and are logged, except a quiet `getUpdates` timeout (`:203-207`).
3. **Offset handling.** The offset moves to `update_id+1` BEFORE the message is handled (`:402`).
   A crash mid-answer loses that message (at-most-once). One bad message cannot kill the loop
   (`:405-408`).
4. **Filter** (`_handle_message`, `:335-369`):
   - ignore no chat id or no text (`:339-340`). Photos, documents, voice and edits are dropped
     (only `text`, and only `message` updates).
   - private chats only: `type` must be `None`/`"private"` and id must be non-negative
     (`:344-346`).
   - allowlist: `str(chat_id) in ALLOWED` (`:347`). An unknown chat gets its own id once, capped at
     50 chats (`:350-353`).
   - built-in commands: `/start`, `/help` → static HELP (`:357-359`); `/clear`, `/reset`, `/new` →
     delete the thread file (`:360-366`).
5. **Answer.** One `sendChatAction typing` (`:368`, `:298-299`). Then
   `_answer_with_memory()` (`:314-332`): load the thread, render a `<thread_context>` block, call
   `answer()`, and append the user + assistant turns only when `ok` (`:329-331`).
6. **Spawn** (`answer()`, `:141-188`). The prompt is `compose_prompt(block, q)` (`:150`;
   `chat_history.py:181-186`). The exact argv (`_claude_cmd`, `:124-138`):

   ```
   <CLAUDE_BIN> -p --output-format json --model <MODEL> --effort <EFFORT>
     --mcp-config deploy/runner/chat_mcp.json --strict-mcp-config
     --settings deploy/runner/chat_settings.json --add-dir <repo>
     --max-turns 40 --append-system-prompt <prompts/chat.md text>
     --dangerously-skip-permissions <prompt>
   ```

   - `subprocess.Popen(..., cwd=<repo>, env=_child_env(), start_new_session=True)` (`:154-156`).
   - Env is an ALLOWLIST: `PATH HOME USER LOGNAME SHELL LANG LC_ALL LC_CTYPE TERM TZ
     CLAUDE_CONFIG_DIR npm_config_cache XDG_CACHE_HOME XDG_CONFIG_HOME CLAUDE_CODE_OAUTH_TOKEN
     ANTHROPIC_API_KEY` (`:110-121`). `TELEGRAM_BOT_TOKEN`, `RH_ACCOUNT_NUMBER`,
     `OPENROUTER_API_KEY` etc. never reach the child.
   - `communicate(timeout=240)`; on timeout `os.killpg(..., SIGKILL)` kills the whole process group
     (`:160-172`).
   - Parse the LAST stdout line as JSON; take `result`; `ok = returncode==0 and not is_error and
     text` (`:174-180`). Log `cost_usd`, `subtype`, the first 200 chars of the question (`:181-183`).
   - On failure the reply is a fixed hint that still says "/mcp on the box" (`:185-187`). That is
     stale: the box now uses `CLAUDE_CODE_OAUTH_TOKEN` (CLAUDE.md runbook).
   - No `--resume`, no session id: ONE NEW PROCESS PER MESSAGE. Continuity is the self-managed
     transcript, chosen on purpose over `--resume` (`chat_history.py:9-13`).
   - The full prompt (thread + question) and the system prompt are argv. Inference: same-uid
     processes can read them in `/proc/<pid>/cmdline`.
7. **Reply** (`send_message`, `:285-295`). Split on line boundaries at 3600 chars (`_chunks`,
   `:271-282`; limit `_MSG_LIMIT-400`, `:79`, `:288`). Each chunk is converted from markdown to
   Telegram HTML (`_to_telegram_html`, `:217-258`) and sent with `parse_mode=HTML`; on any failure
   it is resent as plain text (`:291-295`).

### 1.4 Capability check

| Capability | Today | Evidence |
|---|---|---|
| One process per message | Yes (`claude -p` per message) | `chat_bridge.py:154` |
| Concurrency | None. Serial loop; a 240 s answer blocks every later message | `:401-408` |
| Several outbound messages | Only as chunks of one answer | `:288-295` |
| Typing indicator | Sent once. Telegram clears it after ~5 s, so a long answer shows nothing (inference from Telegram behavior) | `:368` |
| Reactions | No | grep: no `setMessageReaction` |
| Inline keyboards / callback queries | No. `allowed_updates=["message"]` filters out `callback_query` | `:375`, `:394`; grep: no `reply_markup`/`callback_query` |
| Edit a sent message | No | grep: no `editMessage*` |
| Files, photos, voice in or out | No (text only) | `:338-340` |
| Proactive (bot-initiated) messages | Not from the bridge. `lib/telegram.py` can (see §2) | `lib/telegram.py:174` |
| Streaming partial answers | No (`--output-format json`, waits for the end) | `:126` |
| Webhook | No (long-poll, outbound only, IAP-only box) | `docs/CHAT.md:11-16` |

### 1.5 The guard (`chat_guard.py`)

- Wired with matcher `.*`, so every tool call runs it (`chat_settings.json:4-14`). Fails closed: a
  parse or guard exception denies (`chat_guard.py:385-396`), exit 2 on deny (`:405`).
- `DENIED_TOOLS`: write/edit/multiedit/notebookedit/applypatch/webfetch/websearch + 4 order tools
  (`:50-55`). Built-in Grep/Glob denied (recursion) (`:59`, `:361-364`).
- Read tools (`read, ls, bashoutput, todowrite, todoread`) allowed only inside the repo or
  `/var/log/quiver`, realpath-resolved, plus a secret-name regex (`:46`, `:61`, `:69-76`,
  `:122-136`, `:366-376`). Unknown tools (including any MCP tool, Task/Agent) are denied (`:378-379`).
- Bash: fail-closed allowlist. It rejects control chars, backslash, `$`/backtick, redirection,
  globs, subshells, `&`, inline env assignment, leading `~` (`:207-231`, `:139-204`, `:243-250`).
  Allowed: coreutils in `SAFE_BASH` (`:90-94`) with path containment (`:292-296`); read-only git
  subcommands (`:107-108`, `:299-303`); `python deploy/runner/chat_query.py`, `python tick.py
  {decision-proof,strategy-history,memory-show,flow-suggest}`, `python -m lib.market`
  (`:110`, `:318-347`). The `sqlite3` CLI is banned (`:305-310`).
- Design rule stated in the file: do not parse shell, remove the dangerous constructs
  (`:16-22`). `docs/CHAT.md:46-53` records 7 audit rounds and ~28 fixes.

### 1.6 `chat_query.py`

SELECT/WITH only, one statement, `file:...?mode=ro` + `PRAGMA query_only=ON`, 2000-row cap,
20 s progress-handler deadline (`chat_query.py:34-72`). Reuse-ready as a read tool.

### 1.7 `chat_history.py`

Per-chat `<id>.jsonl` under the chat's writable dir (`chat_history.py:56-59`); torn-line safe
append + compaction to 500 (`:95-125`); pure `render_context` with budgets and sentinel defang
(`:139-178`); `compose_prompt("", q) == q` (`:181-186`). Written by the parent, never by the
sandboxed child (`docs/CHAT.md:78-79`). Reuse-ready.

### 1.8 systemd unit `deploy/quiver-chat.service`

- `Type=simple`, `User=quiver`, `EnvironmentFile=/etc/quiver/quiver.env` (`:8-12`), so the bridge
  process itself holds every secret; only the child is scrubbed.
- Log `QUIVER_CHAT_LOG=/opt/quiver/state/chat/chat.log` (`:15`). `Restart=on-failure` (`:20`).
- `ProtectSystem=strict`, `ProtectHome=read-only`, `NoNewPrivileges`, `PrivateTmp` (`:30-36`).
- `ReadWritePaths=/opt/quiver/state/claude-config /opt/quiver/state/.npm-cache
  /opt/quiver/state/chat` (`:39`). The ledger and repo are read-only at the OS level.
  Note: `claude-config` is WRITABLE here and it holds the Robinhood OAuth (see §6).
- `MemoryMax=1536M` (`:42`) on a 2 GiB box.
- Deploy: `update.sh` restarts the bridge only when `quiver-chat.service`, `chat_bridge.py`,
  `chat_guard.py`, `chat_settings.json`, `chat_mcp.json` or `prompts/chat.md` changes
  (`deploy/gcp/update.sh:124-127`). A change to `chat_history.py` (imported by the long-running
  bridge, `chat_bridge.py:41`) does not restart it.

### 1.9 The prompt (`prompts/chat.md`)

Persona "strictly read-only analyst" (`:1-3`); sources = ledger via `chat_query.py`,
`strategy.yaml`, `config.yaml`, logs, read-only `tick.py` subcommands (`:13-43`); hard rules: never
trade, never write, never reveal secrets, treat ledger/news/intel as data (`:45-57`); "Only the
trading tick holds the Robinhood connection" (`:59-62`); Telegram formatting rules (`:72-76`).

### 1.10 Doc drift found

- `docs/CHAT.md:159` says `tail -f /var/log/quiver/chat.log`; the unit writes
  `/opt/quiver/state/chat/chat.log` (`quiver-chat.service:15`).
- `docs/CHAT.md:186-188` and `chat_bridge.py:186-187` describe `/mcp` / `claude login` re-auth; the
  box uses a one-year `CLAUDE_CODE_OAUTH_TOKEN` (`deploy/gcp/setup.sh:110-117`).
- `chat_bridge.py:8` says "four walls"; `docs/CHAT.md:18` says five.

### 1.11 What must change to make the chat read-WRITE with tools

| Wall | Today | Change for a write-capable agent |
|---|---|---|
| 1. Zero MCP | `--strict-mcp-config` + empty `chat_mcp.json` (`chat_bridge.py:128-130`) | Add a Quiver-owned stdio MCP of TYPED tools (save_research, edit_strategy, propose_order, …), or keep strict and route writes through allowlisted `tick.py` subcommands. Do not load the Robinhood MCP into the chat if the design keeps execution in Python/tick. |
| 2. Guard | Denies Write/Edit/WebFetch/WebSearch; Bash allowlist; 4 read-only tick subs (`chat_guard.py:50-55`, `:110`) | Per-tool allowlist of write tools; argument validation for each; WebFetch to an allowlist of hosts (SEC EDGAR) if filings are fetched by the model. Keep fail-closed and keep unknown-tool deny (`:378-379`). |
| 3. Env scrub | Allowlist env (`chat_bridge.py:110-121`) | Keep. A write tool that needs a secret should get it in its own process, not the model's env. |
| 4. OS read-only | `ProtectSystem=strict`, RW only claude-config/npm/chat (`quiver-chat.service:32-39`) | Ledger writes need RW on `/opt/quiver/state` (SQLite `journal_mode=delete` writes a journal file next to the db; `chat_query.py` and `quiver-chat.service:26-29` rely on it). Repo edits need RW on the repo or a separate store for research. Prefer a narrow RW dir (e.g. `state/research/`) + a write path that goes through Python. |
| 5. Allowlist | `TELEGRAM_ALLOWED_CHAT_IDS`, private only (`chat_bridge.py:344-355`) | Keep. A confirm tap must also check `callback_query.from.id`. |
| Runtime | Serial loop, 240 s, 40 turns, `allowed_updates=["message"]` | Research from filings needs longer runs and a worker so the poll loop stays responsive; add `callback_query` to `allowed_updates`; persistent typing or progress messages. |
| Prompt | "never write anything" (`prompts/chat.md:45-57`) | Rewrite. |
| Tick contention | Chat cannot write, so no lock is needed | A writing chat must respect `run_lock` (`lib/runlock.py:19-26`) or serialize writes; `Ledger._conn` waits 30 s on a busy db (`lib/ledger.py:313-317`). |

---

## 2. `lib/telegram.py` and the alert path

- Only API call: `sendMessage` (`lib/telegram.py:125-159`, URL `:56-57`). Params: `chat_id`,
  `text`, `disable_web_page_preview`, `disable_notification`, optional `parse_mode` (`:133-137`).
  No `reply_markup`, so no buttons.
- Never raises; returns `{ok, id}` or `{ok:false, error}` (`:142-159`).
- Chunking at 3500 chars (`:50-51`, `:93-109`), HTML then plain-text fallback per chunk
  (`:162-171`).
- Multi-chat: `ok` is true when the FIRST (primary) chat got all chunks; a secondary failure sets
  `partial` (`:174-227`).
- Chat ids: `TELEGRAM_ALERT_CHAT_IDS` else `TELEGRAM_ALLOWED_CHAT_IDS` (`resolve_env`, `:81-90`).
  Same bot token as the bridge.
- Test switch: `QUIVER_TELEGRAM_DISABLE=1` sends nothing and returns ok (`:118-122`, `:199-201`).
- Import wall: must never be imported by the brain; `tick.py` and `run_tick.py` import it locally;
  enforced by `tests/test_e2e_alerts.py` block J (`lib/telegram.py:9-15`).

Senders (all proactive, all bot-initiated):

| Sender | Where | What |
|---|---|---|
| `tick.py report-send` | `tick.py:3439-3484` | Builds via `_run_report` → `notify.build_digest`, sends, writes the dedup row `mark_notified` only after primary delivery (`:3463-3469`). Used by TICK.md STEP 7b (`TICK.md:418-457`) and the ALERT PROCEDURE (`TICK.md:17-44`). |
| `run_tick.py _maybe_alert` | `deploy/runner/run_tick.py:261-305` | Last-resort pager; dedups on the same `(date, kind, stage)` row (`:288-291`). |
| `tick.py send-test` | `tick.py:3503-3533` | Real send for deploy acceptance. |
| `backup_ledger.py` | `deploy/runner/backup_ledger.py:137-141` | Failure page. |

Rendering is `lib/notify.py` `_render_telegram` / `_render_telegram_digest` / `_render_telegram_alert`
(`lib/notify.py:742-867`), returned as `built["telegram"]`.

Reuse: `lib/telegram.post` is the right base for proactive proposals, but it needs a
`reply_markup` parameter for inline confirm buttons. The two processes that send today (tick and
bridge) do not share a poller, so a button tap reaches only the process that calls `getUpdates`
(the bridge). Inference: one poller must own all updates; Telegram allows only one `getUpdates`
consumer per bot token.

---

## 3. Orchestrator and order path

### 3.1 `run_tick.py` (systemd `quiver.service`)

Sequence inside `main()` (`deploy/runner/run_tick.py:308-673`):

1. Single-run lock, TTL = tick timeout + 600 s (`:326`).
2. `tick.py preflight` (`:334`); error → page + exit 1 (`:335-340`); `proceed:false` → exit 0
   (`:341-343`).
3. Orchestrator auth probe: `claude -p "reply with exactly: AUTH_OK"` (`:194-219`, `:352-358`).
4. Python analysis fan-out `run_analyses.run(pending)` → `state/tmp/analyses.json`
   (`:370-405`); brain-outage page when all ERROR (`:393-395`).
5. Event-risk sidecar `tick.py event-risk` (`:424-433`).
6. Orchestrator spawn (`:436-458`):

   ```
   <CLAUDE_BIN> -p --output-format stream-json --include-partial-messages --verbose
     --model <QUIVER_MODEL, default claude-sonnet-4-6> --effort <QUIVER_EFFORT, default low>
     --mcp-config deploy/runner/mcp.json            # NO --strict-mcp-config
     --settings deploy/runner/settings.json --add-dir <repo>
     --max-turns 400 --append-system-prompt <prompts/orchestrator.md>
     --dangerously-skip-permissions "Follow ./TICK.md exactly for today's tick."
   ```

   `subprocess.run(cmd, cwd=repo, timeout=..., capture_output=True)` with NO `env=` (`:484-485`),
   so the orchestrator inherits the full `quiver.env` (all secrets). No strict mode, so it loads the
   user-scope Robinhood MCP and its OAuth (`:440-445`; `deploy/gcp/setup.sh:162-171`).
7. After the run: auth-stop sentinel / ledger marker (`:503-521`), halt page (`:522-527`), generic
   crash page (`:534-543`), silent no-op guard (`:555-572`), then `goal-track`, `learn-review`,
   `bills-review`, `benchmark-fetch`, `benchmark-backfill` (`:581-648`).

Prompt `prompts/orchestrator.md:1`: execute only what Python authorized; stop on auth error; never
run `analyze.py`.

### 3.2 TICK.md order placement

- STEP 2 loads the Robinhood tools via ToolSearch, including `place_equity_order` and
  `cancel_equity_order` (`TICK.md:74-87`); auth error → `tick.py auth-stop` + alert + STOP
  (`:91-101`).
- STEP 4: write `plan_input.json`, run `tick.py plan` (`TICK.md:227-275`).
- STEP 5 per order (`TICK.md:277-333`): 5a tradability (`:281-282`); 5b cancel resting stops then
  `review_equity_order`; any blocking alert → `blocked_guardrail` (`:284-296`); 5c `dry_run` → never
  place (`:298-300`), live → `place_equity_order` with the SAME `ref_id`, retry once on transport
  error (`:301-305`); 5d `tick.py commit` with status `placed|dry_run|blocked_guardrail|error`
  (`:307-319`); 5e protective stop (now a dead path, `tick.py:3556-3560`).
- STEP 6 reconcile unfinalized orders by `ref_id` via `get_equity_orders`; re-place only with the
  same `ref_id` (`TICK.md:335-341`).
- ABSOLUTE RULES (`TICK.md:481-501`).

### 3.3 `ref_id` reservation (plan) → finalize (commit)

- `_run_plan` (`tick.py:433-1130`) mints a UUID (`Ledger.new_ref_id`, `lib/ledger.py:972-973`) and
  calls `reserve_order` BEFORE returning the order, ONLY when `not cfg.dry_run`. Six sites:

  | Line | Kind |
  |---|---|
  | `tick.py:757` | limit buy, `order_kind="entry"` |
  | `tick.py:784` | market buy, `entry` |
  | `tick.py:841` | model sell, `exit` |
  | `tick.py:938` | rebalance / reconcile sell, `order_kind` variable (`:940`) |
  | `tick.py:988` | `derisk_trim` |
  | `tick.py:1107` | `rebalance_buy` |

  In `dry_run` the order has `ref_id: None` (e.g. `:750-752`), so `order_guard` would deny any place.
- `plan` also writes `ticker_action` rows at plan time (e.g. `tick.py:1116-1117`).
- `reserve_order` = `INSERT OR IGNORE` with `state='reserved'` (`lib/ledger.py:975-995`). Schema
  `orders` (`lib/ledger.py:44-70`): `ref_id PK, trade_date, ticker, side, type, dollar_amount,
  quantity, submitted_at, broker_order_id, result_json, finalized, order_kind, limit_price,
  stop_price, parent_ref_id, state, fill_*`. State comment: `reserved|filled|partial|unfilled|
  cancelled|stop_placed|triggered` (`:61`).
- `cmd_commit` (`tick.py:3167-3210`): `finalize_order` sets `broker_order_id`, `result_json`,
  `finalized=1` (`lib/ledger.py:1016-1021`); state map `placed→filled, dry_run→filled,
  cancelled→cancelled, blocked_guardrail→unfilled, error→unfilled`, unknown status stored raw
  (`tick.py:3182-3187`); `record_action` + append-only `record_event` (`:3195-3209`).
- `unfinalized_orders(day)` (`lib/ledger.py:1023-1039`) feeds preflight; a name with an
  unfinalized row is removed from `pending` so it is not re-planned (`tick.py:260-287`).
- Real fills: `tick.py fills` matches `broker_order_id` against `get_equity_orders`; a missing id
  inside the window becomes `not_found` (`tick.py:3796-3844`); terminal states
  (`lib/ledger.py:1047-1048`).

### 3.4 What `order_guard.py` enforces

- `settings.json` matcher: `place_equity_order|cancel_equity_order|place_option_order|
  cancel_option_order` (`deploy/runner/settings.json:6`).
- `evaluate()`: a non-order tool → allow; an order tool with no `ref_id` → deny; a `ref_id` with no
  `orders` row → deny; else allow (`deploy/runner/order_guard.py:39-49`). Fails closed for order
  tools on error (`:62-70`). Tests: `tests/test_units.py:2725-2738`.
- Gaps (from the code):
  1. Existence only. It does not compare `symbol/side/quantity/dollar_amount/type` in
     `tool_input` with the reserved row, does not check `state`, `finalized`, or `trade_date`.
  2. Tool coverage. The Robinhood MCP also exposes `place_advanced_order`, `place_crypto_order`,
     `exercise_option`, `cancel_advanced_order`, `cancel_crypto_order`, `decline_trade_approval`
     (MCP tool list). None match the matcher, and `ORDER_TOOLS` (`:29-32`) would allow them as
     "non-order tool".
  3. The orchestrator runs `--dangerously-skip-permissions` with unrestricted Bash and the full
     env (`run_tick.py:456`, `:484`). The guard is the only mechanical gate.

### 3.5 Where a "human approved this proposal" check fits

Structurally there is one seam where all three conditions meet: the `orders` row exists before any
broker call, the guard reads it on every place, and commit advances its state.

1. **Plan → proposal.** `_run_plan` already returns a deterministic order list and reserves rows.
   Add a new initial state (e.g. `proposed`) at `reserve_order` (`lib/ledger.py:975-995` already
   takes `state=`), plus a proposal id and expiry.
2. **Human confirm.** A Telegram callback (or chat command) moves `proposed → approved` in Python,
   binding the exact body and an approver chat id + time.
3. **Guard.** `order_guard.evaluate` requires `state == 'approved'`, not expired, today's
   `trade_date`, and `tool_input` fields equal to the row (`order_guard.py:44-49`). Widen
   `ORDER_TOOLS` and the `settings.json` matcher to every place/cancel/exercise tool, or deny
   them outright.
4. **Execution.** Either the tick's STEP 5 places only approved rows (TICK.md change), or a
   short "execute approved" run does. Preflight already treats unfinalized rows as reconcile work
   (`tick.py:249-252`, `:260-287`): a `proposed` row would block re-analysis of that name, which is
   the desired dedup, but it also keeps preflight "proceeding" (inference; needs a state filter in
   `unfinalized_orders`).
5. Alternative or second layer: Robinhood's native approval (§4) makes the human tap happen in the
   Robinhood app, outside Quiver.

---

## 4. Robinhood native trade approvals

- Repo: zero references (grep in §0). No `get_trade_approval_setting` call, no handling of
  `customer_approval_required`, no `pending_approval` state.
- MCP schemas (loaded this session, not called):
  - `place_equity_order`: "When [review] comes back with `customer_approval_required` true and no
    order_checks alert, call this tool straight away … this call does not place the order: it goes
    to the customer to approve or decline in the Robinhood app."
  - `get_trade_approvals(account_number, approval_ids?)`: lists pending approvals; states pending,
    approved, declined, expired, auto-approved; "pending approvals expire on their own".
  - `get_trade_approval_setting(account_number)`: reads whether approval is required. "Only the
    customer can change this setting, in the Robinhood app."
  - `decline_trade_approval(account_number, approval_id)`: the agent can only decline; approve is
    app-only.
- How today's tick would behave if the agentic account had approval ON (inference from code):
  - `customer_approval_required` is not a blocking alert, so TICK.md 5b passes (`TICK.md:294-296`)
    and 5c calls `place_equity_order` (`:301-305`).
  - The orchestrator commits `status:"placed"` with whatever id comes back; commit maps it to
    `state='filled'` (`tick.py:3182`) and records a completed trade event (`tick.py:3201-3209`), so
    cooldown/dedup/consistency treat an unapproved request as a trade.
  - `tick.py fills` would then find no matching broker order and mark it `not_found`
    (`tick.py:3825-3832`), or record a non-terminal state, depending on the id returned (unknown).
  - A declined or expired approval leaves the target unmet; the next day plan re-plans with a NEW
    `ref_id` → a second approval request (inference).
- The repo cannot tell whether the agentic account has approval ON today. One read call
  (`get_trade_approval_setting` with the account number) answers it; this investigation did not
  make it.

---

## 5. systemd units and timers

| Unit | Runs | Schedule | Hardening | Broker creds? |
|---|---|---|---|---|
| `quiver.service` + `quiver.timer` | `run_tick.py` (`deploy/quiver.service:21`) | Mon–Fri 13..21:40 UTC (`deploy/quiver.timer:13`) | `ProtectSystem=full`, `ProtectHome=read-only`, RW `/opt/quiver/state /opt/quiver/logs /var/log/quiver` (`quiver.service:31-35`), `TimeoutStartSec=15000`, `MemoryMax=2500M` | YES. Orchestrator loads the user-scope Robinhood MCP + OAuth from `CLAUDE_CONFIG_DIR`; full env incl. `RH_ACCOUNT_NUMBER` |
| `quiver-chat.service` | `chat_bridge.py` (`:17`) | always on, `Restart=on-failure` | `ProtectSystem=strict`, RW `claude-config`, `.npm-cache`, `state/chat` (`:30-39`), `MemoryMax=1536M` | The OAuth file is on a path it can read AND write; the child is barred from it only by `--strict-mcp-config` + the guard's secret regex (`chat_guard.py:69-76`). Bridge process env holds `RH_ACCOUNT_NUMBER`; child env does not |
| `quiver-intel.service` + timer | `run_intel.py` → `tick.py` intel subcommands (`run_intel.py:27`) | daily 06:00 UTC (`quiver-intel.timer:10`) | `ProtectSystem=full`, RW state/logs (`:20-24`) | No MCP spawn (no `claude` in `run_intel.py`); has the full env file |
| `quiver-backup.service` + timer | `backup_ledger.py` | daily 22:15 UTC (`quiver-backup.timer:7`) | `ProtectSystem=full`, RW state + `/var/log/quiver` (`:21-25`) | No; full env file; sends a Telegram page on failure |
| `quiver-claude-update.service` + timer | `/usr/local/bin/quiver-claude-update` (= `deploy/claude-update.sh`, installed `setup.sh:203`, `update.sh:109`) | daily 05:00 UTC (`quiver-claude-update.timer:7`) | NONE: no `User=`, runs as root, no Protect* (`quiver-claude-update.service:12-17`) | Reads `CLAUDE_CODE_OAUTH_TOKEN` from `quiver.env` for its auth gate (`claude-update.sh:31-39`, `:72`) |

Installation: `setup.sh:194-217` copies and enables all units; the chat bridge is enabled only if
`TELEGRAM_BOT_TOKEN` is set (`setup.sh:219-222`). `update.sh:104-116` re-copies units on change.

---

## 6. Auth surfaces and broker reachability

| Credential | Where it lives | Who uses it | Expiry / refresh |
|---|---|---|---|
| Claude CLI auth `CLAUDE_CODE_OAUTH_TOKEN` (or `ANTHROPIC_API_KEY`) | `/etc/quiver/quiver.env`, from Secret Manager (`setup.sh:110-117`, `:131`) | tick orchestrator (inherits env, `run_tick.py:484`), chat child (allowlisted, `chat_bridge.py:115`), auth probe (`run_tick.py:209`), updater gate (`claude-update.sh:31-39`) | ~1 year, rotated by hand (CLAUDE.md runbook) |
| Robinhood MCP OAuth | `CLAUDE_CONFIG_DIR=/opt/quiver/state/claude-config` (`setup.sh:121`, `:126`, `:132`); server registered user-scope `https://agent.robinhood.com/mcp/trading` (`setup.sh:165-167`) | Only the tick orchestrator loads it (no strict mode, `run_tick.py:440-445`). The chat shares the same dir but excludes it with `--strict-mcp-config` (`chat_bridge.py:128-130`) | Days (3.8–7); human re-auth from the laptop with `deploy/gcp/rh-reauth.sh` (`claude mcp login robinhood-trading --no-browser` as `quiver`, `rh-reauth.sh:38-48`) |
| `RH_ACCOUNT_NUMBER` | `quiver.env` (`setup.sh:136`); `lib/config.py:336` | `tick.py preflight` returns it as `account_number` (`tick.py:190`) for the MCP calls | static |
| Telegram `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_CHAT_IDS`, `TELEGRAM_ALERT_CHAT_IDS` | `quiver.env` (`setup.sh:148-150`), Secret Manager, IAM grant by hand (`terraform/main.tf:35-41`) | chat bridge (poll + reply), `lib/telegram` senders | static |

Processes that can reach the broker: ONLY the `claude` orchestrator child of `quiver.service`.
Python never calls Robinhood (CLAUDE.md "Python decides, orchestrator executes"; no Robinhood
HTTP client in the repo). Any principal that can run `claude` as the `quiver` user with
`CLAUDE_CONFIG_DIR=/opt/quiver/state/claude-config` and without `--strict-mcp-config` can also
reach it (inference from `setup.sh:165` and `rh-reauth.sh:38`). That includes the chat child if its
spawn flags or guard were relaxed: it already has `CLAUDE_CONFIG_DIR` in its env
(`chat_bridge.py:112`) and RW on that dir (`quiver-chat.service:39`).

Note: `load_config` loads `<repo>/.env` with python-dotenv when present (`lib/config.py:324-329`).
A read-only `tick.py` subcommand run by the chat child would load secrets into that Python
subprocess if a `.env` exists in the repo dir (true on the laptop; unknown on the box).

---

## 7. Security walls that must change for a write-capable, order-proposing chat agent

1. **Zero-MCP wall** (`chat_bridge.py:128-130`, `chat_mcp.json`). If the agent gets broker READ
   tools (quotes, positions, SEC filings via `get_sec_filing*`), the chat must load the Robinhood
   MCP, which also brings every place/cancel tool. Then a chat-side guard must deny every
   order-mutating tool by name AND by default (unknown → deny), and the OAuth refresh races two
   processes on one `claude-config` (inference). Safer: keep the chat broker-free; give it a
   Quiver MCP or Python tools that read the ledger snapshot and write proposals only.
2. **Guard** (`chat_guard.py`). Today it is a read-only allowlist. It must become a typed
   write allowlist, per tool, with argument checks. The "eliminate, do not parse" Bash rule should
   stay; new write abilities should be Python subcommands, not shell.
3. **OS wall** (`quiver-chat.service:32-39`). Must open RW on the ledger dir and on any research
   store. That also widens the blast radius to `state/ledger.db` and `state/claude-config`.
4. **Order guard** (`order_guard.py:39-49`). Must bind to an `approved` state and the exact body,
   and cover all order tools.
5. **Orchestrator** (`run_tick.py:436-458`, TICK.md STEP 5). Must stop autonomous placement. It must
   place only rows a human approved (or retire it and use Robinhood native approvals).
6. **Telegram transport.** Add `callback_query` to `allowed_updates` (`chat_bridge.py:375`, `:394`),
   check `from.id` against the allowlist, add `reply_markup` to `lib/telegram.post`
   (`lib/telegram.py:133-137`). Proposals sent proactively by the tick and confirmed through the
   bridge's poller means one poller owns all updates.
7. **Env collision.** Rename the chat's `QUIVER_CHAT_MODEL` (conflicts with
   `quiver_eve/run/decide.mjs:62`).

---

## 8. Component disposition under a chat-first, human-confirmed design

| Component | What it does now | KEEP / ADAPT / RETIRE | Why |
|---|---|---|---|
| `chat_bridge.py` long-poll loop + allowlist + private-only filter (`:335-408`) | Outbound-only Telegram intake, IAP-safe | ADAPT | Correct base; needs `callback_query`, a worker so the loop does not block for 240 s+, progress/typing, and ownership of ALL updates |
| `chat_bridge._claude_cmd` / `answer()` (`:124-188`) | One `claude -p` per message, scrubbed env, process-group kill | ADAPT | Reuse the spawn and env scrub; change flags (MCP set, settings, max-turns, timeout), maybe `stream-json` for progress |
| `_child_env` allowlist (`:110-121`) | Keeps secrets out of the model process | KEEP | Still the right wall with write tools |
| `_to_telegram_html`, `_chunks`, `_strip_md` (`:217-295`) | Markdown → Telegram HTML, chunking, fallback | KEEP | Reuse-ready; dedupe with `lib/telegram._chunks` |
| `chat_history.py` | Bounded per-thread transcript | KEEP | Reuse-ready; becomes conversation memory, separate from research memory |
| `chat_query.py` | SELECT-only ledger reader | KEEP | Safe read tool for the agent |
| `chat_guard.py` | Read-only PreToolUse policy | ADAPT | Keep fail-closed and unknown-tool deny; add typed write tools; keep Bash eliminated |
| `chat_mcp.json` (empty) | Zero MCP | ADAPT | Add a Quiver-owned tool server, not the Robinhood MCP (recommended) |
| `chat_settings.json` | Hook wiring, matcher `.*` | KEEP | Same mechanism |
| `quiver-chat.service` | Always-on, `ProtectSystem=strict`, RW only claude-config/npm/chat | ADAPT | Needs narrow RW for ledger/research; consider its own `CLAUDE_CONFIG_DIR` without the broker OAuth (`docs/CHAT.md:55-58`) |
| `prompts/chat.md` | Read-only analyst persona | ADAPT (rewrite) | New role: research partner + proposer |
| `docs/CHAT.md` | Operator doc | ADAPT | Rewrite; fix drift (§1.10) |
| `tests/test_chat_guard.py`, `tests/test_chat_history.py` | Offline policy + memory tests | KEEP / ADAPT | Extend guard tests to the write allowlist |
| `lib/telegram.py` | Best-effort proactive `sendMessage` | ADAPT | Add `reply_markup`; becomes the proposal sender |
| `lib/notify.py` Telegram renderers | Digest + alert prose | KEEP / ADAPT | Add a proposal renderer |
| `tick.py report-send` / `_maybe_alert` | Digest + pages with dedup | KEEP | Monitoring stays proactive |
| `run_tick.py` supervisor (lock, preflight, auth probe, fan-out, pages) | Drives the autonomous tick | ADAPT | Keep lock/preflight/fan-out/pages; stop the autonomous orchestrator, or run it only for approved rows |
| Orchestrator spawn (`run_tick.py:436-458`) + `prompts/orchestrator.md` + TICK.md STEP 5/6 | LLM places Python-authorized orders | ADAPT or RETIRE | Under human confirm: place only `approved` rows; or replace with Robinhood native approval. Full env inheritance (`:484`) should be scrubbed |
| `tick.py plan` (`_run_plan`) | Deterministic orders + `ref_id` reservation | KEEP / ADAPT | It is the proposal engine; reserve as `proposed` instead of ready-to-place |
| `tick.py commit` + `orders` lifecycle | Finalize + state map | ADAPT | Add `proposed/approved/declined/expired/pending_customer_approval`; stop mapping `placed→filled` blindly (`tick.py:3182`) |
| `order_guard.py` | ref_id-exists gate on 4 tools | ADAPT (harden) | Require `approved`, body match, today, expiry; cover every place/cancel/exercise tool |
| `deploy/runner/settings.json` | Guard matcher for 4 tools | ADAPT | Widen matcher (or `.*` with an allowlist) |
| Robinhood native approval (not used) | — | ADOPT (evaluate) | Gives an out-of-band human tap in the RH app; needs `get_trade_approvals` reconcile and a ledger state |
| `quiver.timer` (hourly autonomous ticks) | Triggers trading | ADAPT | Keep for monitoring/analysis; no autonomous placement |
| `quiver-intel`, `quiver-backup` units | Intel refresh, ledger backup | KEEP | Independent of the change |
| `quiver-claude-update` unit (root, no hardening) | Keeps `claude` current | KEEP | Out of scope; note it runs as root |
| `rh-reauth.sh` | Human Robinhood re-auth | KEEP | Still required while any process holds the broker |
| `QUIVER_CHAT_MODEL` env name in the bridge | Chat Claude model | ADAPT (rename) | Collides with the EVE brain's GLM slug (`decide.mjs:62`) |
