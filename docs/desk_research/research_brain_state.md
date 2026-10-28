# Quiver: analysis + state map (for the chat-first redesign)

Investigated 2026-10-27, read-only, repo `/Users/kvnlee/dev/quiver` @ `889a2ad` (clean `main`).
Every claim cites `file:line`. Ledger facts come from the DEV copy `state/ledger.db` (mtime
2026-08-13). Production is `/opt/quiver/state/ledger.db` on the box. The schemas match the code.
The row counts do NOT reflect production.

---

## 0. Headline answer: is "EVE" a general agent framework we can build the chat agent on?

**Short answer: the framework is installed, but Quiver does not use it. The live "EVE brain" is a
fixed 7-call pipeline script on the Vercel AI SDK. It has no tool registry, no memory, no
sessions, and no human-in-the-loop.**

Evidence:

1. **"EVE" is a third-party npm package: Vercel's `eve`, v0.20.0, a preview release.**
   - `quiver_eve/package.json:14-19`: the dependencies are `"eve": "latest"`, `"ai": "latest"`,
     `@ai-sdk/openai ^4.0.8`, and `zod`. The pin is `latest`, so a fresh `npm install` can pull a
     different version.
   - `quiver_eve/package-lock.json:529-531`: the lockfile resolves `eve` to 0.20.0.
   - `quiver_eve/node_modules/eve/package.json:3,5`: version 0.20.0, "Filesystem-first framework
     for durable backend AI agents that run anywhere". Repo: github.com/vercel/eve, Apache-2.0.
   - `quiver_eve/node_modules/eve/README.md:9-17`: "eve is currently a preview and subject to
     the Vercel beta terms".
   - It is NOT CopilotKit and NOT OpenMuse.

2. **The framework itself is general and has every primitive a chat agent needs.**
   - A tool registry with typed `defineTool`, plus skills, hooks, channels, subagents, and
     schedules: `node_modules/eve/README.md` "Authored Directory" and "Authoring Helpers".
   - HITL approvals (`approval: always()|once()|policy`) and agent questions. A run parks
     durably at `session.waiting`: `node_modules/eve/docs/tools/human-in-the-loop.md:8-14,18-30`.
   - Durable sessions → turns → steps on the Workflow SDK. A local on-disk "world" is used when
     self-hosted: `docs/concepts/execution-model-and-durability.md:6-16`.
   - Per-session durable state (`defineState`): `docs/guides/state.md:6`. It has NO long-term
     memory subsystem. "eve does not have a tenant-aware memory subsystem. You can build one":
     `docs/patterns/multi-tenant-memory.md:6-10`.
   - A built-in **Telegram channel** that renders HITL as inline-keyboard buttons:
     `docs/channels/telegram.mdx:7,45-47`. It is **webhook-only**. It mounts
     `POST /eve/v1/telegram` and you register a public URL: `telegram.mdx:24-32`. The docs
     mention no long-poll mode.
   - It can self-host (`eve start`, a Nitro Node server): `docs/guides/deployment.md:22,130-139`.

3. **Quiver's live path bypasses eve completely.**
   - `quiver_eve/run/decide.mjs:31-37`: it imports only `@ai-sdk/openai`, `ai`
     (`generateText`, `tool`), and `zod`. It never imports `eve`.
   - `decide.mjs:19-25`: the reason for the bypass. "EVE's HTTP stream proxy stalls on long
     generations … this script deliberately bypasses that proxy and calls the ai SDK in-process."
     `analyze.py:577-579` gives the same reason.
   - `quiver_eve/agent/` is the only eve-authored code. It is **dead and partly broken**:
     - `agent/agent.ts:33-57` is only a model config.
     - `agent/instructions.md:1-4` says "DOCUMENTATION ONLY — NOT loaded at runtime".
     - `agent/skills/` and `agent/subagents/` are empty directories.
     - `agent/validate.ts:8` imports `REQUIRED_LABELS`, which `agent.ts` does not export.
       `analyze.py:359-362` confirms: "validate.ts:8's import is broken".
     - `agent/tools/*.ts:2` (example `market_data.ts:16`, `fundamentals.ts:2`) import
       `../../run/quill_data.js`, but only `run/quill_data.mjs` exists.
     - `.eve/` and `.workflow-data/` (8,498 files) are dated Jul 6. That is one `eve dev`
       session on the migration day, and nothing after it.
   - Box deploy runs `npm ci` only when `quiver_eve/` changes: `deploy/gcp/update.sh:86-89`.

4. **What the live "brain" is: a fixed, sequential pipeline.** See §1. The only "tools" are 6
   read-only data functions that the gather turn can call (`decide.mjs:229-236`). There is no
   conversation, no memory beyond a stdin text blob (`decide.mjs:41,173-175`), and no HITL. The
   output is a markdown label contract that `analyze.py` parses.

5. **The repo already has a working chat agent, built on the Claude Code CLI, not on eve.**
   - `deploy/runner/chat_bridge.py:1-15`: a Telegram **long-poll** bridge. Outbound HTTPS only,
     because the box firewall is IAP-only with no public inbound. Each message runs
     `claude -p` read-only with zero MCP servers (`chat_bridge.py:125-140`). The model is
     `claude-sonnet-4-6` (`chat_bridge.py:52-53`).
   - Per-thread memory: a self-managed JSONL transcript per chat id
     (`deploy/runner/chat_history.py:1-12,59`; `chat_bridge.py:63-70`).
   - The intel chain also uses `claude -p` (`lib/intel/chain.py:27-37`). The TICK orchestrator
     is `claude -p` over `TICK.md` (`deploy/runner/run_tick.py:6-12`).
   - So the agent runtime the repo actually relies on is **headless Claude Code** (tools, MCP,
     PreToolUse hooks such as `order_guard.py`). The GLM "EVE" brain is only the per-ticker signal
     generator.

**Implication for "if we don't already have an agent framework, base it on CopilotKit/OpenMuse":**
- Quiver has an agent framework *installed* (eve) but has *no agent built on it*.
- The functional agent runtime in production is headless Claude Code (`claude -p`).
- If you adopt eve for chat, plan for these constraints:
  (a) a preview/beta dependency pinned to `latest`;
  (b) a webhook-only Telegram channel versus the IAP-only box (`chat_bridge.py:4-5`), so you
      need a public ingress or a custom channel;
  (c) the stream-proxy stall that the team already hit (`decide.mjs:19-23`);
  (d) no long-term memory store, so you write your own.
- The existing `agent/` scaffold gives almost nothing to reuse.

---

## 1. The EVE brain in detail (`quiver_eve/`)

| File | Lines | Role |
|---|---|---|
| `run/decide.mjs` | 444 | The live brain. Top-level-await script with zero exports. |
| `run/contract.mjs` | 153 | Assembles and sanitizes the markdown contract. Adds a producer-emitted `## data_availability` section (`contract.mjs:1-24`). |
| `run/quill_data.mjs` | 64 | Spawns `quill_data.py` and parses its JSON. Any failure becomes `UNAVAILABLE` (`quill_data.mjs:41-63`). |
| `run/quill_data.py` | 284 | Python data helper. Kinds: market/sentiment/news/trend/macro/fundamentals (`quill_data.py:255-272`). |
| `run/retry.mjs` | 53 | Bounded retry with an optional fallback for advisory turns (`retry.mjs:24-53`). |
| `agent/` | 323 | Dead eve scaffold (see §0). |
| `test/*.mjs` | 3 files | Contract/retry unit tests. |

**Pipeline (`decide.mjs`), one Node process per ticker:**
1. Gather: the quick model (`glm-4.7-flash`) with 6 tools and up to 12 steps writes 6 `###` reports (`decide.mjs:209-240`, tools `:229-236`, retry `:250-252`).
2. Bull and bear debate, quick model, `ROUNDS` rounds (`:256-277`).
3. Research plan, deep model (`glm-5.2`, reasoning high) (`:280-294`).
4. Trader proposal with 8 labels (`Action`, `Entry Price`, `Stop Loss`, `Position Sizing`, `Position Pct`, `Strategy Basis`, `Catalyst`, `Target Price`) (`:297-320`).
5. Risk debate, aggressive and conservative (`:323-343`).
6. PM decision with 4 labels (`Rating`, `Next Review Hours`, `Conviction`, `Uncertainty`) (`:346-370`).
7. Lever-proposal regex (`:374-376`), then `assembleContract` writes to stdout (`:423-435`).

The system prompt `HORIZON` frames the brain as a **long-horizon technical TREND FOLLOWER**
("price above a RISING 200-day SMA, golden cross, 12-1 momentum"), at `decide.mjs:154-171`. It has
no fundamental-valuation framing. "Fundamentals" enters only as one of six gathered reports.

**Invocation (`analyze.py`):**
- `_run_eve` spawns `node decide.mjs TICKER DATE` (`analyze.py:568-605`). Stdin carries
  `past_context`, the ledger scorecard plus the strategy block (`analyze.py:597-598,813-818`).
  The timeout is `analyze_timeout_sec` = 3600 (`analyze.py:628`; `config.yaml:108`).
- The markdown is split by the `_EVE_SECTIONS` allow-list (`analyze.py:353-357`). The F6 label
  gate is binding (`analyze.py:359-372`). The rating comes from `lib/rating.py`.
- `extract_fields` returns one JSON line (`analyze.py:262-321`) with: `signal` (5-tier or
  ERROR), `action`, `position_sizing`, `position_pct`, `entry_price`, `stop_loss`, `basis`,
  `target_price`, `catalyst`, `next_review_hours`, `conviction`, `uncertainty`,
  `data_quality`, `rationale_summary` (600 chars), and `error`.
- Fail-safe: if core market data is unavailable, the signal becomes ERROR (`analyze.py:272-286`).
- Output: `print(json.dumps(result))` (`analyze.py:845`). On an exception the ERROR JSON goes to
  `:842`. The full report set is dumped to `state/analyze_logs/<date>_<T>.json`
  (`analyze.py:323-333`).
- Fan-out: `scripts/run_analyses.py` (432 lines) runs `analyze.py` in parallel off the LLM
  (`scripts/run_analyses.py:1-20`). `run_tick.py:381` calls it.

**Persistence of research today:** this is weak, and it matters for the new design.
- The full reports in `state/analyze_logs/` and `logs/reasoning/` are **pruned after 30 days**:
  `tick.py:63-68` (`PRUNE_TARGETS`), `config.yaml:130` (`retention_days: 30`), and
  `tick.py:3851`.
- Only the `decisions` row survives. It holds `rationale` (≤600 chars), `basis`, and
  `proof_json` (`lib/ledger.py:614-650`).
- `state/memory/reflect/*.md` holds derived scorecards, not research (`lib/reflect_memory.py:1-23`).

---

## 2. `lib/intel/`: the legislation/regulation service

**What it ingests:**
- **US bills**: Congress.gov poll plus govinfo USLM section XML (`tick.py:3999-4012`;
  `lib/intel/sections.py:73`). This needs a Congress API key (`tick.py:4008-4010`).
- **Federal Register final rules**: keyless API (`lib/dataflows/fedreg.py:1-11,61`;
  `sections.py:105-111`).
- It does NOT ingest SEC filings, earnings, or company documents of any kind.

**Pipeline** (`lib/intel/refresh.py:23-111`, glue `tick.py:3961-3997`):
- Fetch the XML, then split it into deterministic sections (`sections.py`, 163 lines).
- Run a keyword prefilter with a high-recall vocabulary (`prefilter.py:1-20,30,91`). Rules from a
  relevant agency skip the vocabulary (`refresh.py:58-61`).
- Run the LLM chain, one `claude -p` call per kept section (`chain.py:27-37,91-128`), capped at
  `max_sections_per_run: 120` (`config.yaml:225`).
- Run a deterministic **verbatim-span check**: a claim whose quoted span is not character-for-
  character in the section is dropped (`chain.py:11-15,95,128`).
- Write one transaction per document (`refresh.py:82-104`).
- Then three more steps run: `aggregate.py` scores per sponsor and ticker (pure, `aggregate.py:1-14`),
  `profiles.py` renders legislator markdown (`profiles.py:1-14`), and `propose.py` makes
  shadow/tier `intel` proposals (`propose.py:1-18`).
- Scheduler: `deploy/runner/run_intel.py:49-55` runs intel-refresh (6000 s), then intel-profile,
  then intel-propose.
- Config: `intel.enabled: true` (`config.yaml:214-226`).
- The book description is a **hardcoded string** (`tick.py:3975`).
- The chain prompt is specific to bills: "ONE SECTION of a US bill … U.S. Code cite"
  (`chain.py:40-55`).

**Schemas** (owned by `lib/intel/store.py:17-89`, created through `Ledger.ensure_schema` at
`lib/ledger.py:359-360`, connection at `lib/ledger.py:333`):

```sql
CREATE TABLE intel_documents (doc_id TEXT PRIMARY KEY, kind TEXT NOT NULL /*bill|rule*/, title TEXT,
  published TEXT NOT NULL /*as-of anchor*/, sponsor TEXT, congress INTEGER, agency TEXT, url TEXT,
  digest TEXT, fetched_at TEXT);                                   -- store.py:19-30
CREATE TABLE intel_sections (doc_id TEXT NOT NULL, sec TEXT NOT NULL, header TEXT, usc_cites TEXT /*JSON*/,
  deadline TEXT, kept INTEGER NOT NULL DEFAULT 0, PRIMARY KEY (doc_id, sec));   -- store.py:33-41
CREATE TABLE intel_section_impact (doc_id, sec, ticker NOT NULL, direction /*helps|hurts|ambiguous*/,
  confidence /*high|medium|low*/, step1_what_changes, step1_span /*VERBATIM*/, step2_mechanism, reasoning,
  verify_verdict, span_verbatim INTEGER, model, prompt_version, scored_at,
  PRIMARY KEY (doc_id, sec, ticker));  INDEX idx_intel_impact_ticker(ticker)   -- store.py:45-62
CREATE TABLE intel_power (bioguide, congress, role, committee, chamber, name, source,
  PRIMARY KEY (bioguide, congress, committee));                    -- store.py:67-76
CREATE TABLE intel_shadow_proposals (id INTEGER PK AUTOINCREMENT, created_at, kind /*ADD|SLEEVE|WEIGHT*/,
  ticker, sleeve, target_weight REAL, direction, evidence_json, applied INTEGER DEFAULT 0);  -- store.py:79-89
```
The DEV row counts are 0 for all five.

**Can it serve as a store for company-filing research?** The *pattern* can. The *tables* are a
partial fit:
- Good fit:
  - document → section → evidence-claim keying.
  - `published` as the look-ahead-safe as-of anchor (`store.py:7-9,23`).
  - The verbatim-span honesty gate (`chain.py:11-15`).
  - An injectable LLM backend for offline tests (`chain.py:9-11`).
  - Per-document commits with no lock held during LLM I/O (`refresh.py:29-32`).
- Gaps:
  - **Section text is not stored.** `intel_sections` keeps only header, cites, deadline, and
    kept (`store.py:33-41`; `refresh.py:89-90`), so research cannot be retrieved later.
  - `digest` is never filled (`refresh.py:84-88` passes no digest).
  - The columns are bill/rule-shaped (`sponsor`, `congress`, `usc_cites`).
  - There are no numeric-fact tables (revenue, margins, TAM).
  - There is no full-text or vector search.
  - The impact semantics are "does this law help or hurt ticker X", not "what does this 10-K say".
- Reuse: `sections.py` USLM/FR parsers do not transfer to 10-K/10-Q HTML/XBRL. `prefilter`,
  `aggregate`, `profiles`, and `power` are legislator-specific.
- Prior verdict: the owner's own research said not to build the legislator-profile → portfolio
  path (`~/.claude/projects/-Users-kvnlee-dev-quiver/memory/political-signal-verdict.md`, a
  memory note, not repo code).

The older `lib/legislative.py` tables (`bills`, `bill_impacts`, `bill_cursor`,
`congress_cache`; `legislative.py:41,69,85,96`) are the predecessor. They are OFF:
`legislative.enabled: false` at `config.yaml:189`.

---

## 3. Ledger (`lib/ledger.py`, 1542 lines): every table

There are 28 tables, plus `sqlite_sequence`. Schema source: `lib/ledger.py:26-295` unless noted.
Migrations run at `ledger.py:340-480`. The full `.schema` dump is in the Appendix.

| Table | Created at | DEV rows | One-line purpose |
|---|---|---|---|
| `day_baseline` | ledger.py:27 | 10 | Opening equity per trade date, plus `halted` / `halt_reason` (daily-loss halt). |
| `ticker_action` | ledger.py:34 | 51 | PK (trade_date, ticker): latest state, classic ≤1 action/ticker/day dedup (`already_acted` :541). |
| `orders` | ledger.py:44 | 33 | PK `ref_id` UUID reserved before the broker call. Lifecycle/fill columns. Idempotency (`reserve_order` :975, `finalize_order` :1016, `unfinalized_orders` :1023). |
| `notifications` | ledger.py:73 (rebuilt :472) | 9 | PK (trade_date, kind, stage) + `content_hash`: at-least-once alert/digest dedup. |
| `decisions` | ledger.py:86 | 66 | One row per analysis: signal, intent, sizing seeds, basis, `proof_json`, conviction, uncertainty, data_quality, 600-char rationale (`record_decision` :614). |
| `outcomes` | ledger.py:119 | 51 | 1:1 with decisions: directional/benchmark/alpha/realized/unrealized P&L, `adherence` (`record_outcome` :693). |
| `actions` | ledger.py:134 | 34 | Append-only action event log (intraday cooldown/cap/on-change gates). |
| `ticker_schedule` | ledger.py:150 | 0 | Per-ticker intraday cadence (`next_due_ts`, `analyses_today`). |
| `strategy_goal` | ledger.py:165 | 0* | Goal (15%/12mo), macro thesis JSON, active book, start equity. |
| `target_portfolio` | ledger.py:181 | 0* | PK (goal_id, ticker): sleeve, `target_weight`, `band`, status active/exiting/removed. The carried conviction target (`retarget_weights` :1365). |
| `goal_tracking` | ledger.py:195 | 0 | Daily glidepath snapshot (value, ahead/behind, alpha, regime). |
| `cash_flows` | ledger.py:213 | 0 | External deposits/withdrawals for flow-adjusted returns. |
| `thesis_state` | ledger.py:221 | 0 | Macro regime + confirmation hysteresis (pending_regime, confirm_count, regime_since). |
| `universe_change_log` | ledger.py:239 | 0 | Proposals with status `proposed→approved→applied/rejected/expired`, `decided_by`, unique open `content_hash`, `change_key`. |
| `strategy_change_log` | ledger.py:267 | 0 | Append-only audit of regime/book/weight/status/basis changes + `proof_json`. |
| `position_basis` | ledger.py:284 | 0 | Average buy price per ticker (from broker fills). |
| `run_lock` | ledger.py:290 | 0 | Single-row mutex for one tick at a time (`try_acquire_run_lock` :1500). |
| `eval_levers`, `eval_lever_uses` | levers.py:35,51 | 0, 0 | Self-learning "evaluation levers" the LLM proposes, scored by alpha. |
| `bills`, `bill_impacts`, `bill_cursor`, `congress_cache` | legislative.py:41,69,85,96 | 0,0,0,468 | Legislative catalyst (OFF). |
| `intel_*` (5) | intel/store.py:19-89 | 0 | See §2. |

\* The DEV copy is stale (mtime 2026-08-13). In production these are seeded by `tick.py strategy-set`.

**Under a chat-first, human-confirmed design:**
- **KEEP, essential:**
  - `orders`: ref_id idempotency and reserve-before-place. The broker dedups by ref_id. The
    orchestrator guard keys on it (`deploy/runner/order_guard.py:1-15`).
  - `day_baseline`: the halt circuit breaker.
  - `decisions` + `outcomes`: the track record the chat can cite.
  - `strategy_goal` + `target_portfolio`: the human-set book.
  - `strategy_change_log`: the audit trail.
  - `notifications`, `cash_flows`, `goal_tracking`, `position_basis`, `run_lock`.
- **ADAPT:**
  - `universe_change_log` is the closest existing **proposal/approval table**. It already has
    `proposed/approved/applied/rejected/expired`, `decided_at`, `decided_by`, an open-proposal
    unique index, and expiry (`ledger.py:239-265,1416-1459`). Generalize it, or clone it, into
    `trade_proposals`.
  - `ticker_action`: change per-day dedup to per-proposal state.
  - `decisions`: add a link to the research doc/claim ids that justified each one.
  - `intel_documents`/`intel_sections`/`intel_section_impact`: use them as the template for a
    filings research store, with text stored.
- **RETIRE (dead under the new design):**
  - `actions` + `ticker_schedule` (intraday autonomy; `intraday_enabled: false`, `config.yaml:110`).
  - `thesis_state` (PCE-driven macro regime hysteresis).
  - `eval_levers` + `eval_lever_uses`.
  - `bills`, `bill_impacts`, `bill_cursor`, `congress_cache`.
  - `intel_power` + `intel_shadow_proposals` (legislator-specific).

---

## 4. Fundamental and news data already fetched

- `lib/dataflows/y_finance.py` (238 lines):
  - `get_fundamentals` reads `yfinance .info`: name, sector, market cap, PE TTM/forward, PEG,
    P/B, EPS, beta, 52-week high/low, 50/200-day averages, revenue TTM, gross profit, EBITDA, net
    income, margins, ROE/ROA, D/E, current ratio, book value, FCF. Units are normalized
    (`y_finance.py:49-120`).
  - `get_balance_sheet` (`:123`), `get_cashflow` (`:154`), `get_income_statement` (`:185`), and
    `get_insider_transactions` (`:216`) give quarterly/annual statements with look-ahead filtering.
- Live use: the brain calls **only** `get_fundamentals` + `get_balance_sheet`
  (`quiver_eve/run/quill_data.py:198-209`). **`get_cashflow`, `get_income_statement`, and
  `get_insider_transactions` have no callers**: a repo-wide grep finds only their `def` lines.
  This gives no growth or revenue trend today.
- `lib/dataflows/yfinance_news.py` (233 lines): `get_news_yfinance` gives ticker headlines in a
  date window (`:69-123`). `get_global_news_yfinance` gives macro headlines, title and publisher
  only (`:126`; `quill_data.py:211-224`).
- `lib/dataflows/stocktwits.py` (83 lines): sentiment. `quill_data.py:173`.
- `lib/trend.py` (577 lines): long-horizon technical guideposts (Sharpe/Sortino/Calmar,
  drawdown, ADX, MA structure, regime). Context only, never sizing (`trend.py:1-8`). This fits
  the "technical as secondary input" role directly.
- `lib/screener.py` (105) + `lib/screener_data.py` (64): yfinance `EquityQuery` on sector and
  market-cap floor, ranked by momentum. Propose-only (`screener.py:1-16`; `screener_data.py:1-17`).
- `lib/universe.py` (193): pure allow-list/quotable gate (`validate_add` :68), add/remove with
  cash conservation (`:81-170`), and `validate_book` (`:171`). No market data.
- **SEC/EDGAR: none.** A repo-wide grep for `edgar|sec.gov|10-K|10-Q|8-K|xbrl|companyfacts|S-1`
  over py/mjs/ts/md/yaml finds 0 code hits. The only hit is "thesis-1" in a test. Earnings dates
  are not fetched either. `lib/event_risk.py:1-10` is the pure math, with an earnings date as an
  injected input.
- **Available but unused:** the Robinhood MCP the orchestrator already holds exposes
  `get_sec_filing_index` (10-K/10-Q/8-K by symbol and date), `get_sec_filing` (table of contents
  and section text), `get_sec_filing_facts` and `get_sec_filing_facts_catalog` (XBRL GAAP
  concepts), `get_financials` (revenue/GP/NI/margin by period), `get_earnings_results`, and
  `get_earnings_calendar`. It also exposes **`get_trade_approval_setting` / `get_trade_approvals`
  / `decline_trade_approval`**: a *broker-native* "agent-placed orders need the customer's manual
  approval in the app" mechanism. Source: MCP tool descriptions in this session. No repo code
  references any of them; the grep is empty. Note that `deploy/runner/chat_bridge.py:125-140`
  runs chat with ZERO MCP servers, so the chat cannot reach these tools today.

---

## 5. The Python decision wall: invariants and their fate

**The wall test** (`tests/test_units.py:1343-1442`):
- The roster comes from the filesystem (`lib/**/*.py`, `:1421`). A new module is inside the wall
  by default.
- Mechanism A, an AST import scan, forbids `lib.risk`, `lib.signals`, `lib.config`, and
  `lib.levers` (`:1367`).
- Mechanism B, a word-boundary symbol scan, forbids `buying_power`, `max_dollars_per_trade`,
  `get_portfolio`, `place_equity_order`, `get_equity_positions`, `get_equity_quotes`,
  `RiskConfig`, and `remaining_daily_cap` (`:1368-1370`).
- `_WALL_GRANTS` lists itemized opt-outs (`:1374-1404`): config, risk, signals, wall_replay,
  learn, ledger, and reflect_memory. Exactly 7 entries, asserted at `:5101`.
- Meta-guards: the roster must have ≥40 entries, grants must not be stale, and grants must be
  load-bearing (`:1423-1442`).
- Separate checks: signals never imports risk or reflect_memory (`:1338-1341`). `lib/intel/*`
  and `lib/trend.py` sit inside the wall with no grant.

**Invariants:**

| Invariant | Where | Under human-confirmed design |
|---|---|---|
| Long-only: Sell with no position is a no-op | `signals.py:22` `plan_action` | **KEEP** |
| Buy dollars = min(size, daily budget, buying_power − buffer, room_under_target, risk_cap). Never fails open. $100 fallback. | `signals.py:66-131` (clamp stack `:121`) | **KEEP**: compute proposal size; recheck at confirm time |
| Sell quantity / min-notional ($1 RH floor, `min_trade_notional: 5`) | `signals.py:132-258`, `:145`; `config.yaml:43`; `tick.py:773,820,919,977` | **KEEP** |
| Room under target weight | `signals.py:216`; `tick.py:318` | **KEEP** |
| Stop-distance risk budget (`per_trade_risk_pct: 1.0`) | `signals.py:488`; `tick.py:514-519`; `strategy.yaml:216` | **KEEP** as a sizing suggestion |
| Limit/stop pricing in Python | `signals.py:425-470` | **KEEP** (OFF by default, `config.yaml:57,66`) |
| R:R gate | `tick.py:408` `_rr_gate`; `signals.py:472` | **ADAPT**: show it as a metric in the proposal. The knob was removed for inverted polarity (`strategy.yaml:203-206`) |
| ref_id reserve-before-place; crashed names excluded from re-plan | `ledger.py:972-1050`; `tick.py:195,260-287`; `order_guard.py:1-15` | **KEEP, essential**: reserve only on human confirm. The guard still blocks any un-reserved ref_id |
| Daily-loss halt 20% + KILL file | `tick.py:572-575`; `ledger.py:508-520`; `tick.py:223-228`; `config.yaml:20` | **KEEP** (circuit breaker; "do not shrink", CLAUDE.md) |
| Market open + XNYS calendar + act-after-open | `tick.py:229-235`; `lib/market.py` | **KEEP** for execution |
| Config fail-safe (`dry_run` stays paper unless explicitly false; "verify" model ids raise) | `config.py:332,416-418` | **KEEP** |
| Per-name/sleeve caps + cash floor | `allocate.py:163` `_apply_caps`; `tick.py:1378-1382,1461-1462` | **KEEP** as validation of a human-chosen weight |
| Weights → dollars, drift, bands, rebalance intents | `portfolio.py:24-148` | **KEEP** |
| Conviction → target weight (EWMA vs prior, regime scalar) | `allocate.py:80-356`; `tick.py:1439-1442` | **RETIRE/ADAPT**: exists to turn LLM conviction into money. The human sets the weight |
| Conviction hysteresis (min-delta gate 2.0) | `allocate.py:358-397`; `tick.py:1458`; `config.yaml:44` | **RETIRE**: anti-churn for noisy LLM signals |
| Funded increases | `allocate.py:399-438`; `tick.py:1467` | **ADAPT**: useful math to show "funded by trimming X" |
| Calibration (hit-rate → 0.5-1.5× conviction multiplier) | `calibrate.py:1-100` (guards `:11-15`, min_n `:70`) | **RETIRE** as sizing. Keep the hit-rate as a displayed track record |
| Strategy-consistency gate (suppress an ungrounded buy↔sell reversal; basis-churn budget) | `signals.py:306-393`; `tick.py:335-406`; `config.yaml:49-50` | **ADAPT**: the human is the gate now. The "basis changed? catalyst named?" logic becomes the *change detector* that decides whether to message the human ("only when data shows something changed") |
| Intraday cooldown / action cap / material change / review clamp (48 h ceiling) | `signals.py:260-303,400-424`; `tick.py:668-674` | **RETIRE** (`intraday_enabled: false`) |
| Macro regime confirm N + dwell; book switch on PCE | `strategy.py:342-515`; `tick.py:1229`; `strategy.yaml:76-78,187-193` | **RETIRE** or keep as a human-run note |
| Downside-vol / trend-gate / derisk tiers | `signals.py:510-574` | **RETIRE**: already removed from config 2026-08-12 (`strategy.yaml:200-213`) |
| Core-data fail-safe (bad market data → ERROR, never a trade) | `analyze.py:272-286`; `contract.mjs` | **KEEP the principle**: a proposal on stale data must be flagged or refused |
| Self-learning levers | `levers.py`; `decide.mjs:374-376` | **RETIRE** |
| Universe add/remove gates (allow-list, quotable, cooldown, human `--approve`) | `universe.py:68-193`; `tick.py:2328-2490`; `strategy.yaml:172-182` | **KEEP** (already human-confirmed) |

---

## 6. How the strategy/premise is represented today

- **`strategy.yaml`** (216 lines, gitignored, `.gitignore:8`; on the box it comes from Secret
  Manager). The DEV copy has mtime Aug 12 and may differ from the box.
  - `goal`: +15%/12mo versus SGOV 3.6% (`strategy.yaml:22-27`).
  - `macro_thesis`: prose summary + AI-sovereignty addendum, catalysts, PCE triggers,
    `correlation_note` (the only field the model reads) (`:32-94`).
  - `rh_tradable_confirmed`: 35-symbol allow-list (`:110-114`).
  - `books.core_ai` (default) (`:122-138`):
    - ETFs/funds: **SMH 15, SOXX 7, URA 5, QQQ 8, SPCX 5 ("SpaceX — Starlink"), BOT 5 ("basket of
      PRIVATE robotics startups"), BSOL 4, SGOV 18 (cash residual)**.
    - Single names: **ANET 6, CEG 9, GEV 7, VRT 6, DLR 5**.
    - Each has a `band` (0.6–2.0).
    - **This matches the brief: 8 ETF/fund lines + 5 single names, sum = 100.**
  - `books.dial_up_ai` (`enabled: false`) (`:139-153`).
  - `sleeves."AI Sovereignty".screen` (`:165-167`).
  - `learning` gates (`:172-182`), `consistency` (`:187-193`), `risk_policy` (`:215-216`).
- **`lib/strategy.py`** (531): strict loader into a frozen `StrategyConfig` (`:117`). Holds the
  regime/book selection and `sleeve_thesis` (`:516`). It never sizes (`:1-12`).
- **Ledger copy:** `strategy-set` writes the yaml into `strategy_goal` + `target_portfolio`, and
  `construct` reads the ledger (`strategy.yaml:3-6`; `tick.py:1135-1228`). "yaml = source,
  ledger = runtime state."
- **`STRATEGY.md`** (83 lines, gitignored, `.gitignore:14`, commit `00b816a`) is human prose,
  §1–7. §7 "Agent learnings" is append-only (`STRATEGY.md:73`). It holds 5+ identical
  auto-bullets "[PROPOSE_DERISK] … 2026-06-14", which is noise.
- **`lib/strategy_doc.py`** (39): `append_learning` only (`:21-39`). One caller,
  `tick.py:1785` (learn-review).
- **`lib/strategy_context.py`** (105): renders a qualitative, digit-free advisory block of the
  book role per ticker into `past_context` (`:1-15,30,85`). Called from
  `lib/reflect_memory.py:114,391`.
- **What is missing for the new design:**
  - No per-company thesis record (TAM, growth, valuation, "why now").
  - The thesis is one shared prose blob (`macro_thesis.summary`) plus a single `basis` tag
    string per decision (`decisions.basis`).
  - 8 of 13 holdings are ETFs/funds with no 10-K of their own. Filing-based analysis applies
    directly only to ANET, CEG, GEV, VRT, and DLR. ETFs need a holdings-level or
    index-methodology approach.

---

## 7. Scale facts (to size the migration)

- **Python, non-test:** 24,389 lines in total (`find … -name '*.py'` excluding tests, .venv, and
  node_modules).
  - `tick.py` 4,447 (`_run_plan` alone is about `:433-1134`).
  - `analyze.py` 850.
  - `lib/*.py` 11,071, plus `lib/intel/` 1,015 and `lib/dataflows/` 1,337, giving **lib total
    13,423**.
  - `deploy/runner/` about 2,185.
  - `scripts/` 920.
- **Largest lib modules:** ledger 1,542, notify 870, config 803, trend 577, signals 574, strategy
  531, wall_replay 521, attribution 519, legislative 463, risk 445, allocate 438, reflect_memory
  430, goal 361, memory 279, telegram 227.
- **Runbook:** `TICK.md` 501. `deploy/runner/run_tick.py` 677.
- **EVE:** decide.mjs 444, contract.mjs 153, quill_data.py 284, quill_data.mjs 64, retry.mjs 53,
  agent/ 323 (dead). The node_modules tree is 65 MB with 33 top-level packages.
- **Tests:**
  - 28 Python test files, 14,793 lines. `test_units.py` alone is 5,955 lines.
  - `tests/run_e2e.sh` runs 27 suites, plus 1 `--live` GLM suite (`run_e2e.sh:24-62`).
  - The static assertion call-site count is about 2,527, of which about 1,471 are in
    test_units. The runtime count is higher because of loops, such as the wall roster loop.
  - There are 90 `intel` mentions in test_units.
  - 3 JS test files are in `quiver_eve/test/`.
- **Git:** 179 tracked files.

---

## 8. Component disposition

| Component | What it does now | Disposition | Why |
|---|---|---|---|
| `eve` npm framework (v0.20.0) | Installed, unused live (`decide.mjs:19-37`) | **DECIDE** (not reuse-by-default) | General framework with HITL/sessions/Telegram, but preview, `latest`-pinned, webhook-only Telegram vs IAP box, stream stall already hit |
| `quiver_eve/agent/` | Dead eve scaffold, broken imports | **RETIRE** | Never loaded (`instructions.md:1-4`); `validate.ts:8` and `tools/*.ts:2` are broken |
| `quiver_eve/run/decide.mjs` | Fixed 7-turn GLM trend-follower pipeline → label markdown | **RETIRE** as decider (optionally keep as a "technical second opinion" tool) | Prompt is technical-only (`:154-171`). No memory, no conversation, no documents |
| `run/contract.mjs` | Markdown contract + forgery sanitizer | **RETIRE** | Exists only for the analyze.py label-parsing seam |
| `run/retry.mjs` | Bounded retry/fallback | **KEEP** (port the pattern) | Generic, tested |
| `run/quill_data.py` | 6 read-only data channels with `core_available` | **ADAPT** into chat-agent tools | Reusable fetchers + honest availability flag |
| `analyze.py` | Spawn brain, parse labels, fail-safe, dump logs | **ADAPT/RETIRE** | Keep the core-data fail-safe principle; the label contract dies |
| `scripts/run_analyses.py` | Parallel per-ticker fan-out | **ADAPT** | Becomes the scheduled "did anything change?" scan |
| `lib/dataflows/y_finance.py` | Fundamentals + statements (statements unused) | **KEEP + extend** | Wire income/cashflow for growth. Add SEC via the RH MCP or EDGAR |
| `yfinance_news.py`, `stocktwits.py` | News headlines, sentiment | **KEEP** (secondary) | Cheap context |
| `lib/trend.py` | Long-horizon technical guideposts | **KEEP** | Exactly the "technical as secondary input" |
| SEC/EDGAR ingestion | Does not exist | **BUILD** | Zero repo hits. The RH MCP `get_sec_filing*` / `get_financials` / `get_earnings_*` exist and are unused |
| `lib/intel/` (sections, prefilter, chain, refresh, store) | Bill/rule → section → LLM impact with verbatim-span check | **ADAPT the pattern**, new parser + prompt | Document/section/evidence keying, as-of anchor, span honesty gate, and injectable LLM transfer. Text is not stored and the schema is bill-shaped |
| `lib/intel/` (aggregate, profiles, propose), `intel_power`, `intel_shadow_proposals` | Legislator profiles + shadow proposals | **RETIRE** | Legislator-specific. The owner's own verdict says not to pursue it |
| `lib/dataflows/fedreg.py`, `congress.py`, `lib/legislative*.py`, `bills*` tables | Regulation/bill feeds; catalyst (OFF) | **RETIRE** (fedreg optional) | Off (`config.yaml:189`), dead since 07-13 per CLAUDE.md |
| `lib/screener.py` + `screener_data.py` | Momentum screen → propose adds | **ADAPT** | Chat "find me candidates" tool; swap the momentum rank for fundamental filters |
| `lib/universe.py` | Allow-list/quotable gate, book conservation | **KEEP** | Pure, safety-relevant |
| `lib/memory.py` + `reflect_memory.py` | Ledger scorecard → `past_context`; markdown snapshots | **ADAPT** | Becomes the retrievable track record in chat |
| `lib/risk.py` | Proof-bearing Sharpe/Sortino/DD/hit-rate | **KEEP** | Chat can cite audited metrics |
| `lib/levers.py`, `eval_levers*` | LLM-proposed eval levers | **RETIRE** | Autonomy-only. 0 rows in DEV |
| `lib/learn.py` | Underperformance → proposals | **ADAPT** | Feeds the "something changed" trigger |
| `lib/signals.py`: sizing/sell/pricing/risk-cap | Deterministic clamp stack | **KEEP** | Proposal size must still be Python math |
| `lib/signals.py`: consistency gate | Suppress ungrounded reversals | **ADAPT** → change detector | The human is the gate. Reuse "basis/catalyst changed" to decide when to ping |
| `lib/signals.py`: intraday gates, vol/trend/derisk scalars | Cooldown/cap/on-change/clamps | **RETIRE** | Tame autonomous LLM cadence. Most are already OFF |
| `lib/allocate.py`: conviction→weights, hysteresis | LLM conviction drives target weights | **RETIRE/ADAPT** | Keep `_apply_caps` and `fund_increases` as validation/display |
| `lib/calibrate.py` | Hit-rate → conviction multiplier | **RETIRE** as sizing | Only amplifies LLM conviction. Show the hit-rate instead |
| `lib/portfolio.py` | Weights → dollars/intents/bands | **KEEP** | Core rebalance math for proposals |
| `lib/strategy.py` + `strategy.yaml` | Books/sleeves/weights/macro thesis | **ADAPT** | Keep books/weights/allow-list. Regime/PCE machinery RETIRE. Add a per-name thesis link |
| `STRATEGY.md` + `strategy_doc.py` | Human prose + auto-append | **ADAPT** | Replace with the persisted research/thesis store. Current §7 is duplicate noise |
| `lib/strategy_context.py` | Digit-free book-role block | **ADAPT** | Same idea, new consumer (chat context) |
| `lib/goal.py`, `benchmark.py`, `attribution.py` | Goal glidepath, SPY benchmark, beta | **KEEP** | Diagnostics the chat should answer from |
| `lib/event_risk.py` | Earnings/effective-date severity math | **ADAPT** | Warn "earnings in N days" on a proposal |
| Ledger `orders` + `order_guard.py` | ref_id idempotency + PreToolUse placement guard | **KEEP, essential** | Reserve on human confirm. The guard blocks anything else |
| Ledger `day_baseline` + KILL | Daily-loss halt | **KEEP** | Circuit breaker, not a decision |
| Ledger `decisions`/`outcomes` | Decision memory + scorecard | **KEEP/ADAPT** | Link to research ids |
| Ledger `universe_change_log` | Proposed→approved→applied workflow | **ADAPT** → trade-proposal queue | Already human-approval shaped |
| Ledger `strategy_change_log`, `notifications`, `cash_flows`, `goal_tracking`, `position_basis`, `run_lock`, `strategy_goal`, `target_portfolio` | Audit/alerts/goal/book state | **KEEP** | Needed by any design |
| Ledger `ticker_action` | Per-day dedup snapshot | **ADAPT** | Becomes per-proposal state |
| Ledger `actions`, `ticker_schedule`, `thesis_state` | Intraday log, cadence, macro hysteresis | **RETIRE** | Autonomy-only |
| `TICK.md` + `run_tick.py` (claude -p over the runbook) | Autonomous tick orchestration | **ADAPT** | Split into a scheduled scan → proposal, and confirm → execute |
| `deploy/runner/chat_bridge.py` + `chat_guard.py` + `chat_history.py` | Read-only Telegram chat on `claude -p`; long-poll; JSONL memory | **ADAPT** (strong candidate base) | Already chat-first, IAP-compatible, guarded. Needs research-store tools + a confirm action |
| Robinhood trade-approval tools (`get_trade_approval_setting` / `get_trade_approvals` / `decline_trade_approval`) | Unused | **EVALUATE** | Broker-native human confirmation of agent-placed orders could be the confirm step itself |

---

## Appendix: full DEV ledger `.schema` (verbatim, `sqlite3 state/ledger.db .schema`)

```sql
CREATE TABLE day_baseline (
    trade_date      TEXT PRIMARY KEY,
    baseline_equity REAL NOT NULL,
    captured_at     TEXT NOT NULL,
    halted          INTEGER NOT NULL DEFAULT 0,
    halt_reason     TEXT
);
CREATE TABLE ticker_action (
    trade_date TEXT NOT NULL,
    ticker     TEXT NOT NULL,
    signal     TEXT,
    intent     TEXT,
    status     TEXT NOT NULL,          -- decided|dry_run|placed|skipped|error|blocked_guardrail
    detail     TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (trade_date, ticker)
);
CREATE TABLE orders (
    ref_id          TEXT PRIMARY KEY,
    trade_date      TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    side            TEXT,
    type            TEXT,
    dollar_amount   REAL,
    quantity        REAL,
    submitted_at    TEXT,
    broker_order_id TEXT,
    result_json     TEXT,
    finalized       INTEGER NOT NULL DEFAULT 0
, order_kind TEXT DEFAULT 'entry', limit_price REAL, stop_price REAL, parent_ref_id TEXT, state TEXT DEFAULT 'reserved', fill_avg_price REAL, fill_quantity REAL, fill_fees REAL, fill_state TEXT, fill_captured_at TEXT);
CREATE TABLE decisions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date        TEXT NOT NULL,
    ticker            TEXT NOT NULL,
    run_id            TEXT,
    decided_at        TEXT NOT NULL,
    signal            TEXT,
    intent            TEXT,
    position_pct      REAL,
    entry_price       REAL,
    stop_loss         REAL,
    next_review_hours REAL,
    decision_price    REAL,
    rationale         TEXT
, basis TEXT, plan_trigger TEXT, target_price REAL, proof_json TEXT, conviction REAL, uncertainty REAL, data_quality TEXT);
CREATE TABLE sqlite_sequence(name,seq);
CREATE INDEX idx_decisions_ticker ON decisions(ticker, decided_at);
CREATE TABLE outcomes (
    decision_id        INTEGER PRIMARY KEY,    -- 1:1 with decisions.id
    resolved_at        TEXT NOT NULL,
    holding_days       INTEGER,
    directional_return REAL,                   -- (price_now - decision_price)/decision_price
    benchmark_return   REAL,
    alpha              REAL,
    realized_pnl       REAL,                    -- position-level (broker basis); NULL in dry-run
    unrealized_pnl     REAL,
    scored_against     TEXT                     -- 'directional' | 'both'
, adherence TEXT);
CREATE TABLE actions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date    TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    run_id        TEXT,
    ts            TEXT NOT NULL,
    signal        TEXT,
    intent        TEXT,
    status        TEXT,
    detail        TEXT,
    dollar_amount REAL
);
CREATE INDEX idx_actions_ticker ON actions(trade_date, ticker, ts);
CREATE TABLE ticker_schedule (
    trade_date     TEXT NOT NULL,
    ticker         TEXT NOT NULL,
    next_due_ts    TEXT,
    analyses_today INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (trade_date, ticker)
);
CREATE TABLE strategy_goal (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at           TEXT NOT NULL,
    status               TEXT NOT NULL DEFAULT 'active',   -- active|superseded
    target_return_pct    REAL,
    horizon_months       INTEGER,
    benchmark            TEXT,
    benchmark_annual_pct REAL,
    constraint_note      TEXT,
    macro_thesis_version TEXT,
    macro_thesis_json    TEXT,
    active_book          TEXT DEFAULT 'core_55_45',
    as_of                TEXT,
    start_date           TEXT,
    start_equity         REAL
);
CREATE TABLE target_portfolio (
    goal_id       INTEGER NOT NULL,
    sleeve        TEXT,
    ticker        TEXT NOT NULL,
    target_weight REAL NOT NULL,
    band          REAL DEFAULT 5.0,
    status        TEXT NOT NULL DEFAULT 'active',   -- active|exiting|removed
    book          TEXT DEFAULT 'core_55_45',
    quotable      INTEGER DEFAULT 1,
    proxy_ticker  TEXT,
    updated_at    TEXT,
    PRIMARY KEY (goal_id, ticker)
);
CREATE INDEX idx_target_portfolio_active ON target_portfolio(goal_id, status);
CREATE TABLE goal_tracking (
    id                     INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id                INTEGER NOT NULL,
    trade_date             TEXT,
    captured_at            TEXT,
    portfolio_value        REAL,
    glidepath_target_value REAL,
    cumulative_return_pct  REAL,
    ahead_behind_pct       REAL,
    alpha_vs_benchmark_pct REAL,
    active_book            TEXT,
    regime                 TEXT
);
CREATE INDEX idx_goal_tracking_goal ON goal_tracking(goal_id, trade_date);
CREATE TABLE thesis_state (
    goal_id         INTEGER PRIMARY KEY,
    as_of           TEXT,
    regime          TEXT NOT NULL DEFAULT 'neutral',     -- neutral|deploy|standdown
    active_book     TEXT DEFAULT 'core_55_45',           -- core_55_45|dial_up_63_37|derisked_cash
    last_trigger    TEXT,
    last_macro_json TEXT,
    updated_at      TEXT
, pending_regime TEXT, pending_since TEXT, confirm_count INTEGER NOT NULL DEFAULT 0, regime_since TEXT);
CREATE TABLE universe_change_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id      INTEGER NOT NULL,
    proposed_at  TEXT,
    kind         TEXT,                                   -- add|remove|derisk|book_switch|flag
    ticker       TEXT,
    sleeve       TEXT,
    from_book    TEXT,
    to_book      TEXT,
    target_weight REAL,
    tier         TEXT,                                   -- soft|derisk|universe
    content_hash TEXT NOT NULL,
    reason       TEXT NOT NULL,
    goal_gap_pct REAL,
    status       TEXT NOT NULL DEFAULT 'proposed',       -- proposed|approved|applied|rejected|expired
    decided_at   TEXT,
    decided_by   TEXT
, change_key TEXT);
CREATE INDEX idx_universe_change_status ON universe_change_log(goal_id, status, proposed_at);
CREATE UNIQUE INDEX idx_universe_open ON universe_change_log(goal_id, content_hash) WHERE status='proposed';
CREATE TABLE run_lock (
    id          INTEGER PRIMARY KEY CHECK (id = 1),      -- single-row mutex
    holder      TEXT NOT NULL,
    acquired_at TEXT NOT NULL
);
CREATE TABLE notifications (
                trade_date   TEXT NOT NULL,
                kind         TEXT NOT NULL,
                stage        TEXT NOT NULL DEFAULT '',
                content_hash TEXT NOT NULL,
                recipients   TEXT,
                sent_at      TEXT NOT NULL,
                PRIMARY KEY (trade_date, kind, stage)
            );
CREATE TABLE strategy_change_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    goal_id     INTEGER,
    changed_at  TEXT NOT NULL,
    change_type TEXT NOT NULL,                            -- regime|book|weight|status|basis
    ticker      TEXT,                                     -- NULL for goal-level (regime|book)
    from_value  TEXT,
    to_value    TEXT,
    trigger     TEXT,                                     -- what caused it (macro_reading, strategy-set, ...)
    reason      TEXT,
    proof_json  TEXT
);
CREATE INDEX idx_strategy_change_goal ON strategy_change_log(goal_id, changed_at);
CREATE TABLE eval_levers (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL,            -- data_source | analysis_angle | sentiment_weight | other
    config_json     TEXT NOT NULL DEFAULT '{}',
    rationale       TEXT,
    status          TEXT NOT NULL DEFAULT 'proposed',  -- proposed | active | kept | retired
    proposed_at     TEXT NOT NULL,
    first_used_at   TEXT,
    decisions_used  INTEGER NOT NULL DEFAULT 0,
    baseline_alpha  REAL NOT NULL DEFAULT 0.0,
    lever_alpha     REAL NOT NULL DEFAULT 0.0,
    score           REAL NOT NULL DEFAULT 0.0,   -- running mean of (lever_alpha - baseline_alpha)
    last_scored_at  TEXT,
    retired_reason  TEXT
);
CREATE TABLE eval_lever_uses (
    lever_id    INTEGER NOT NULL,
    decision_id INTEGER NOT NULL,
    used_at     TEXT NOT NULL,
    PRIMARY KEY (lever_id, decision_id),
    FOREIGN KEY (lever_id) REFERENCES eval_levers(id)
);
CREATE TABLE bills (
    bill_id         TEXT PRIMARY KEY,          -- e.g. '119-hr-3633'
    congress        INTEGER,
    chamber         TEXT,
    bill_type       TEXT,
    number          INTEGER,
    title           TEXT,
    status          TEXT,                      -- introduced|committee|passed_house|...|became_law|failed
    latest_action   TEXT,
    latest_action_date TEXT,
    policy_codes    TEXT,                      -- JSON list of Congress policyArea/legislativeSubjects codes
    content_hash    TEXT,                      -- hash(status|latest_action_date|update-incl-text): re-analyze only on change
    passage_prob    REAL,                      -- FINAL Python-owned probability, 0..1 (NEVER from the LLM)
    passage_source  TEXT,                      -- 'heuristic' | 'kalshi_blend'
    impact_json     TEXT,                      -- LLM thesis blob (advisory)
    analysis_model  TEXT,
    analyzed_at     TEXT,
    judged          INTEGER NOT NULL DEFAULT 0,
    judge_verdict   TEXT,                      -- pass|fail|hold
    judge_reason    TEXT,
    judge_votes_json TEXT,
    judged_at       TEXT,
    applied         INTEGER NOT NULL DEFAULT 0,
    first_seen      TEXT,
    updated_at      TEXT
);
CREATE INDEX idx_bills_actionable ON bills(judged, applied, passage_prob);
CREATE TABLE bill_impacts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    bill_id       TEXT NOT NULL,
    ticker        TEXT NOT NULL,
    sector        TEXT,
    direction     TEXT,                        -- benefit|suffer
    magnitude     REAL,                        -- 0..1 (LLM ADVISORY; never sizes)
    horizon       TEXT,                        -- near|medium|long
    rationale     TEXT,
    proposal_hash TEXT,                        -- -> universe_change_log.content_hash (NULL until minted)
    created_at    TEXT,
    UNIQUE(bill_id, ticker)
);
CREATE INDEX idx_bill_impacts_ticker ON bill_impacts(ticker);
CREATE INDEX idx_bill_impacts_hash ON bill_impacts(proposal_hash);
CREATE TABLE bill_cursor (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    last_update_date TEXT,
    last_update_including_text TEXT,
    polled_at TEXT
);
CREATE TABLE congress_cache (
    cache_key   TEXT PRIMARY KEY,
    body        BLOB NOT NULL,
    fetched_at  TEXT NOT NULL
);
CREATE TABLE cash_flows (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_date  TEXT NOT NULL,
    amount      REAL NOT NULL,
    note        TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_cash_flows_date ON cash_flows(trade_date);
CREATE TABLE intel_documents (
    doc_id      TEXT PRIMARY KEY,          -- '118-hr-1' | 'FR-2026-12345'
    kind        TEXT NOT NULL,             -- bill | rule
    title       TEXT,
    published   TEXT NOT NULL,             -- ISO date. THE as-of anchor. NEVER fetch time.
    sponsor     TEXT,                      -- bioguide (bills only)
    congress    INTEGER,
    agency      TEXT,                      -- rules only
    url         TEXT,
    digest      TEXT,
    fetched_at  TEXT
);
CREATE TABLE intel_sections (
    doc_id    TEXT NOT NULL,
    sec       TEXT NOT NULL,
    header    TEXT,
    usc_cites TEXT,                        -- JSON list
    deadline  TEXT,
    kept      INTEGER NOT NULL DEFAULT 0,  -- did it survive the prefilter cost-gate?
    PRIMARY KEY (doc_id, sec)
);
CREATE TABLE intel_section_impact (
    doc_id             TEXT NOT NULL,
    sec                TEXT NOT NULL,
    ticker             TEXT NOT NULL,
    direction          TEXT,               -- helps | hurts | ambiguous
    confidence         TEXT,               -- high | medium | low
    step1_what_changes TEXT,
    step1_span         TEXT,               -- VERBATIM quote — the audit anchor
    step2_mechanism    TEXT,
    reasoning          TEXT,
    verify_verdict     TEXT,               -- UPHELD | REJECTED | DOWNGRADED | (none)
    span_verbatim      INTEGER,            -- deterministic char-for-char check
    model              TEXT,
    prompt_version     TEXT,
    scored_at          TEXT,
    PRIMARY KEY (doc_id, sec, ticker)
);
CREATE INDEX idx_intel_impact_ticker ON intel_section_impact(ticker);
CREATE TABLE intel_power (
    bioguide  TEXT NOT NULL,
    congress  INTEGER NOT NULL,
    role      TEXT,                         -- chair | ranking | leadership | agency_head
    committee TEXT,
    chamber   TEXT,
    name      TEXT,
    source    TEXT,
    PRIMARY KEY (bioguide, congress, committee)
);
CREATE TABLE intel_shadow_proposals (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at    TEXT,
    kind          TEXT,                     -- ADD | SLEEVE | WEIGHT
    ticker        TEXT,
    sleeve        TEXT,
    target_weight REAL,
    direction     TEXT,
    evidence_json TEXT,                     -- the section_impact rows behind it
    applied       INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE position_basis (
    ticker           TEXT PRIMARY KEY,
    avg_buy_price    REAL NOT NULL,
    quantity         REAL,
    as_of            TEXT NOT NULL
);
```
