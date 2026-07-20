# Plan — Unified email alerts: run-complete + on-error (defense-in-depth)

> **SUPERSEDED (2026-07-19): alerting moved from EMAIL to TELEGRAM.** All alerts (digest + halt/
> auth_error/error) now go to the operator's Telegram chat via the bot — a plain HTTPS POST
> (`lib/telegram`), sent in-tick by `tick.py report-send` and last-resort by `run_tick.py`
> `_maybe_alert`; there is no more Resend MCP. The dedup / best-effort / at-least-once / stage-keyed
> `notifications` machinery described below still applies (it is channel-agnostic) — only the
> transport changed. `lib/mailer.py` + `RESEND_*` are kept as a dormant rollback. This doc is
> retained for the still-accurate dedup/severity/stage design.
>
> **Note (2026-06):** the deploy moved from AWS to GCP — see `docs/DEPLOY.md` and `deploy/gcp/`. AWS/EC2/SSM/CloudWatch references below are historical design notes.

**Status:** proposed (pending /autoplan review → implement e2e)
**Branch:** `feat/macro-strategy-layer`
**Author:** Claude (ultracode) · 2026-06-14

## 1. Goal

Guarantee the operator is emailed an HTML message, in **one consistent visual
pattern**, for two trigger families:

1. **Every full run that completes** — the daily run-complete digest (already
   exists; make it guaranteed + on the new design).
2. **Any error / abnormal stop** — broker creds expired (the named case), daily-loss
   halt, hard tick errors (preflight / plan / commit / analyze-all-failed), the
   headless orchestrator crashing or timing out, and best-effort hiccups
   (report / prune / reflect / protect). Each tells the operator *what happened* and
   *what to do* ("SSH into the box and run `/mcp`").

User decisions captured up front:
- **Delivery = defense-in-depth.** Rich in-tick emails via the Resend **MCP** (Claude
  orchestrator), PLUS a best-effort Python **last-resort sender** in `run_tick.py`
  (Resend HTTP API) for failures that kill the orchestrator before it can email.
- **Alert on everything** (auth, halt, hard errors, best-effort hiccups, any critical
  error) — but **deduped** so "everything" is at most once-per-day-per-failure-type,
  never hourly spam.

## 2. What already exists (do NOT rebuild)

- `lib/notify.py` — pure renderer, `build_digest(model) -> {subject, html, text,
  content_hash, kind}`. Already has 3 `kind`s: `digest` / `auth_error` / `halt`.
  But: **triplicated** kind branches across `_subject` / `_render_text` /
  `_render_html`, **ad-hoc inline HTML**, no shared chrome → visual drift, the
  "inconsistent pattern" problem.
- `tick.py report` renders + decides `should_send` (`notify.enabled` AND
  `content_hash != last_notified_hash(date, kind)`); `report-commit` persists the
  sent hash. **Python never sends**; the orchestrator sends via Resend MCP
  (`TICK.md` STEP 7b).
- Dedup table `notifications(trade_date, kind, content_hash, recipients, sent_at)`,
  one row per `(trade_date, kind)`, `INSERT OR REPLACE`. `last_notified_hash` /
  `mark_notified` in `lib/ledger.py`.
- `config.notify = {enabled, to, from_addr, subject_prefix}`; `to` from `NOTIFY_TO`
  env (fallback `notify.to`); fail-safe (off unless `enabled is True`).
- Two prod paths: headless `deploy/runner/run_tick.py` (systemd) → `tick.py
  preflight` → spawns `claude -p` over `TICK.md`; and a kept-open `/loop` session.
  `run_tick.py` imports **no** email code; surfaces AUTH_ERROR / halt / timeout /
  non-zero exit as JSON status lines for CloudWatch.

## 3. Architecture: two senders, ONE dedup table

```
                         ┌──────────────── lib/notify.py (PURE renderer) ───────────────┐
                         │  build_digest(model) -> {subject, html, text, content_hash}  │
                         │  ONE shared design system; kind+severity registry            │
                         └───────▲───────────────────────────────────▲──────────────────┘
  in-tick (rich)               │ renders                            │ renders
  ┌────────────────────────────┴─────────┐         ┌────────────────┴───────────────────┐
  │ Claude orchestrator (TICK.md)         │         │ run_tick.py last-resort (ops layer) │
  │ tick.py report -> Resend MCP send     │         │ lib/mailer.py -> Resend HTTP API    │
  │ covers: digest, auth_error, halt,     │         │ covers ONLY orchestrator-unreachable│
  │         error (preflight/plan/commit) │         │ failures: preflight err, claude     │
  │ when reached in-tick                  │         │ crash/timeout, AUTH_ERROR early-abort│
  └───────────────┬───────────────────────┘         └────────────────┬───────────────────┘
                  │ report-commit                                     │ mark_notified
                  └──────────────► notifications table (date,kind) ◄──┘
                          ONE dedup channel → whoever sends first wins,
                          the other dedups. No double-paging.
```

The unification trick: **both senders compute the same `content_hash` for the same
event and check/write the same `notifications` row.** So if the orchestrator already
emailed `auth_error` for the day, the last-resort sender sees the hash present and
skips; if the orchestrator died before emailing, the row is absent and the
last-resort sender fires. This requires the alert hash to be **stable across senders**
(see §6 — alert hash keys on `(kind, stage, dry_run)`, NOT the variable error text).

Invariant preserved: `notify.py` still never touches config/ledger/network. The new
`lib/mailer.py` is the *only* network egress and is **ops-layer only** (imported by
`run_tick.py`, never by `analyze.py`/`plan`/the trading brain).

## 4. Email taxonomy

| kind          | severity   | accent | fires when                                                        | sender(s)              |
|---------------|------------|--------|-------------------------------------------------------------------|------------------------|
| `digest`      | ok/dry_run | green/amber | a substantive tick reaches close-out (run complete)          | orchestrator (MCP)     |
| `halt`        | critical   | red    | daily-loss kill-switch fires (`plan.halt`)                        | orchestrator; last-resort if crash |
| `auth_error`  | critical   | red    | broker MCP token rejected (401)                                  | orchestrator; last-resort if early-abort |
| `error`       | critical   | red    | hard stop: preflight/plan/commit error, analyze-all-failed, claude crash/timeout | orchestrator if reached, else last-resort |
| `error`       | warning    | amber  | best-effort hiccup: report/prune/reflect/protect error           | orchestrator (MCP), best-effort |

`error` carries a `stage` field (`preflight`/`plan`/`commit`/`analyze`/`orchestrator`/
`report`/`prune`/`reflect`/`protect`/…) and a `severity` (`critical`/`warning`) that
drive copy + accent + dedup. `digest`/`halt`/`auth_error` keep working unchanged for
back-compat; `auth_error`/`halt` are conceptually `error` with fixed stage+severity
but keep their dedicated kinds so existing TICK.md/tests don't churn.

## 5. Design system (Mobbin-inspired, email-client-safe)

Source inspiration (examined via Mobbin MCP):
- **Portfolio/hero** (Acorns, Quicken, Fidelity, Copilot): big hero metric on a tinted
  header, color-coded P&L **pills** (green `+`, red `−`), label-left/value-right rows.
- **Alert/action-needed** (Zopa, Coinbase Wallet, LinkedIn "Action needed", Glassdoor):
  a **status-icon circle** in a severity color, bold problem headline, supporting text,
  and an explicit **what-to-do** / CTA block.
- **Receipt/summary** (CVS, Careem, Gojek): card with itemized rows + bold total.

All five email variants compose from ONE set of shared primitives so they share a
pattern (kills triplication + drift):

- `_shell(*, accent, header_html, body_html, footer_html)` — outer container:
  640px max-width, system font stack, a **status accent bar** (top border in the
  severity color), brand header (`Quiver` wordmark + date + mode badge), body, footer.
- `_status_banner(severity, title, subtitle)` — colored circle + icon
  (`✓ ok`, `⚠ warning`, `🔒 auth`, `🛑 halt`, `✕ critical`) + bold headline + subtitle.
  Used by every alert kind.
- `_hero(label, value, sub_html)` — big metric (equity) with label + colored sub
  (baseline / daily change). Used by digest + halt.
- `_card(title_html, inner_html, accent=None)` — rounded card; the per-ticker and
  per-section container.
- `_kv(label, value, value_color=None)` — label-left / value-right row.
- `_pill(text, tone)` — pill badge; `tone ∈ {pos, neg, neutral, warn, crit}`.
- `_action_block(title, steps)` — "What to do" list (the LinkedIn/Glassdoor pattern):
  e.g. auth_error → `["SSH into the box", "Run /mcp to re-authenticate", "The next
  wake resumes automatically"]`.
- `_footer(model)` — generated-at, mode, "full reports in state/analyze_logs/ · live
  thinking in logs/reasoning/".

Palette constants (module-level, single source of truth):
`OK=#047857`, `DRY=#b45309`, `WARN=#d97706`, `CRIT=#dc2626`, plus tints for cards.

**Per-kind composition:**
- `digest`: header(mode badge) → `_hero(equity, ±change pill)` → account-risk line →
  per-ticker `_card`s (signal pill + traded-line + Decision/Debate) → footer. The
  run-complete email.
- `halt`: `_status_banner(critical, "Trading halted", reason)` → `_hero(equity)` →
  `_action_block("What to do", ["Review logs/orchestrator.log", "Investigate the
  loss", "rm KILL to resume when ready"])` → optional plan card → footer.
- `auth_error`: `_status_banner(critical, "Broker auth expired", "Tick stopped — no
  orders placed")` → `_action_block("Fix it", ["SSH into the box", "Run /mcp to
  re-authenticate", "Trading resumes on the next wake"])` → optional `event_detail`
  `<pre>` → footer.
- `error/critical`: `_status_banner(critical, "Tick failed at <stage>", short)` →
  `event_detail` card → `_action_block(...)` → footer.
- `error/warning`: `_status_banner(warning, "<stage> hiccup (tick continued)", short)`
  → `event_detail` card → footer. (No alarmist CTA — it self-heals.)

Both `_render_html` and `_render_text` are driven by a `KIND_REGISTRY` keyed on
`(kind, severity)` → `{icon, title_fn, body_fn, severity}` so a new variant is one
registry entry, not three edits. `_subject` uses the same registry.

## 6. Dedup semantics (the crux)

Extend `digest_hash(model)`:
- `kind == "digest"` → **unchanged** decision-skeleton hash (so the existing
  no-op-wake dedup + the 169 tests are byte-identical).
- alert kinds (`halt`/`auth_error`/`error`) → hash over **`{kind, stage, dry_run}`
  only** — deliberately EXCLUDING `event_detail`, equity, timestamps. Rationale:
  1. **Cross-sender stability** — the orchestrator (full error text) and the
     last-resort sender (only sees "AUTH_ERROR" in the transcript) produce the SAME
     hash → the shared `notifications` row dedups them. No double email.
  2. **Anti-spam** — a plan error that recurs every hourly wake hashes identically →
     pages **once per day per stage**, not 12×. A *different* failure (different
     `stage`) gets a different hash → pages once more. Next trading day → new date →
     can page again.

`should_send` gains per-event toggle gating (see §7): digest requires
`notify.on_complete`; alert kinds require `notify.on_error`.

## 7. File-by-file changes

### `lib/notify.py` (refactor → design system)
- Add palette constants + shared primitives (`_shell`, `_status_banner`, `_hero`,
  `_card`, `_kv`, `_pill`, `_action_block`, `_footer`).
- Add `KIND_REGISTRY` + `_severity(model)` helper (`error` reads `model["severity"]`,
  defaults `critical`; `digest` → `ok`/`dry_run`; `halt`/`auth_error` → `critical`).
- Rewrite `_subject` / `_render_text` / `_render_html` to dispatch through the
  registry + primitives. Keep `summarize`, `digest_hash` (extended per §6),
  `build_digest` signatures stable. `build_digest` return adds nothing new
  (still 5 keys) — but **also surface `severity` + `stage`** inside? No: keep the 5
  keys; severity/stage live in the model only.
- `_render_*` read new model keys `stage`, `severity` (null-safe defaults).
- Update the stale module docstring (it's a *renderer for all email kinds*, not just
  a digest).

### `lib/mailer.py` (NEW — last-resort HTTP sender, ops layer)
- `send_email(*, api_key, from_addr, to, subject, html, text, timeout=10) -> dict`:
  POST `https://api.resend.com/emails` via `urllib.request` (stdlib, no new dep),
  `Authorization: Bearer <api_key>`. Returns `{"ok": True, "id": ...}` or
  `{"ok": False, "error": ...}`. **Never raises** (wraps everything). A `dry`/inject
  hook (or honoring a `QUIVER_MAILER_DISABLE` env) lets tests exercise payload
  construction with zero network.
- Tiny, dependency-free, no trading imports. Docstring states the invariant: ops-only,
  best-effort, the single network egress.

### `lib/config.py`
- `NotifyConfig`: add `on_complete: bool`, `on_error: bool`, `alerts_to: List[str]`.
- Parse (fail-safe): `on_complete = notify_d.get("on_complete", True) is not False`
  (default ON — preserve today's always-send digest); `on_error = ... is not False`
  (default ON). `alerts_to` resolves from `NOTIFY_ALERTS_TO` env → `notify.alerts_to`
  → falls back to `to` (so alerts go to the same place by default).
- Validation: when `enabled`, also validate `alerts_to` addresses (only if `on_error`).
- `from_addr` doc note: the **last-resort** Python sender needs a real verified
  `from`; blank still fine for the MCP path. No behavior change, just doc.

### `tick.py`
- Add `_run_report(cfg, led, data) -> dict` core (mirrors `_run_plan` etc.) and make
  `cmd_report` a thin file-reading wrapper → unlocks direct e2e testing.
- `_build_report_model`: thread `stage` + `severity` from `data` into the model
  (null-safe). For `error`/`auth_error` with no plan, the existing null-safe reads
  already yield a minimal alert.
- `_run_report` should_send gating: `digest` requires `cfg.notify.on_complete`;
  `halt`/`auth_error`/`error` require `cfg.notify.on_error`. Recipients =
  `cfg.notify.alerts_to` for alert kinds, `cfg.notify.to` for digest. Reasons:
  `notify_disabled` / `complete_disabled` / `error_disabled` / `already_sent` / `new`.

### `TICK.md`
- New "**STEP 0 / On any error → alert**" convention block: at each STOP point
  (STEP 1 preflight error, STEP 4 plan error, STEP 5 commit error, STEP 3
  analyze-all-failed) run the report procedure with `kind:"error"`,
  `severity:"critical"`, `stage:"<which>"`, `event_detail:<the {"error",...} text>`
  — **best-effort, then STOP**.
- Best-effort hiccups (report/prune/reflect/protect errors in STEP 6b/7): optionally
  fire `kind:"error"`, `severity:"warning"`, `stage:"<which>"` — best-effort, do NOT
  stop. (Keep the existing `*_SKIPPED` log lines.)
- STEP 2.1 auth_error / STEP 4 halt: add `stage` + `severity:"critical"` to their
  report inputs (otherwise unchanged).
- STEP 7b: note recipients now come from the report's `recipients` (alert vs digest)
  and the `should_send` reasons list expands.
- Update ABSOLUTE RULES: "On any error in STEP 1/2 → fire the best-effort alert, then
  STOP."

### `deploy/runner/run_tick.py` (last-resort wiring)
- After the claude subprocess (and at preflight-error / timeout / lock branches),
  detect a failure → build the model via `notify.build_digest` (auth_error / halt /
  error+stage) → dedup via `led.last_notified_hash(day, kind)` → if absent, send via
  `lib.mailer.send_email(api_key=RESEND_API_KEY, from_addr=RESEND_FROM|cfg.notify.from_addr, to=alerts_to)`
  → on success `led.mark_notified(...)`. **Best-effort: never changes the return
  code**; email status is a separate stdout line, NOT shipped to `tick.log` (respect
  `_FILE_SKIP_KEYS`, keep CloudWatch filters precise).
- Read recipients/from from `load_config()` (already importable) + `RESEND_API_KEY` /
  `RESEND_FROM` env. If key/from missing → log `mailer_unconfigured` and skip (degrade
  safe).
- Failure → kind/stage mapping: preflight error → `error`/`preflight`; non-zero exit →
  `error`/`orchestrator`; timeout → `error`/`orchestrator` (`"tick exceeded the
  timeout"`); `AUTH_ERROR` in transcript → `auth_error`/`broker_auth`; halted →
  `halt`/`daily_loss_halt`.

### `config.yaml`
- Under `notify`: add `on_complete: true`, `on_error: true`, and a comment block for
  `alerts_to` (+ `NOTIFY_ALERTS_TO` / `RESEND_API_KEY` / `RESEND_FROM` for the
  last-resort sender).

### `deploy/quiver.service` + `docs/DEPLOY.md`
- Document that the last-resort sender needs `RESEND_API_KEY` + `RESEND_FROM` (verified
  domain) in the systemd `EnvironmentFile` (`quiver.env`), alongside the existing MCP
  registration. Note it is optional — absent → last-resort silently disabled, in-tick
  MCP path still works.

## 8. Test matrix

### Unit (`tests/test_units.py`, offline plain-asserts — append before footer)
- **Shared shell**: for each kind (`digest`, `halt`, `auth_error`, `error`+critical,
  `error`+warning) assert html contains the brand header, the accent bar color for its
  severity, and the footer; `len(html) < 20000`.
- **Per-kind**: digest → hero equity + a P&L pill + per-ticker cards;
  `auth_error` → status banner + `/mcp` in the action block + "SSH"/"log into the box";
  `halt` → "halted" + reason + `rm KILL`; `error/critical` → stage in subject+body +
  action block; `error/warning` → "tick continued", amber, no alarmist CTA.
- **digest_hash dedup (§6)**: digest skeleton hash unchanged (re-run the existing
  noise/flip assertions). Alert hash: `(kind, stage, dry_run)` keyed — same
  stage+kind, different `event_detail`/equity/timestamp → SAME hash (cross-sender +
  anti-spam); different `stage` → different hash; `auth_error` vs `error/preflight`
  → different.
- **config**: `on_complete`/`on_error` default ON, `is not False` fail-safe; `alerts_to`
  resolves env → config → falls back to `to`; validation when enabled.
- **mailer**: with network disabled (inject/`QUIVER_MAILER_DISABLE`), `send_email`
  builds the correct JSON body (to/from/subject/html/text) + Bearer header; a transport
  error returns `{"ok": False, ...}` and never raises.

### e2e (`tests/test_e2e.py` or a new `tests/test_e2e_alerts.py`)
"WHEN are emails sent" — drive `T._run_report` against a temp cfg + temp ledger:
1. **run complete** → `digest`, `should_send True`, reason `new`; identical re-run →
   `should_send False` reason `already_sent`.
2. **on_complete False** → digest `should_send False` reason `complete_disabled`.
   **notify disabled** → `notify_disabled`.
3. **halt** → `kind=halt` `should_send True`; repeat same day → False.
4. **auth_error** → `should_send True`; repeat → False.
5. **hard error** (`kind=error`, stage `plan`) → True; same stage repeat → False;
   different stage (`commit`) → True (independent paging).
6. **best-effort hiccup** (`kind=error`, severity `warning`, stage `prune`) → True,
   and assert `on_error=False` suppresses it (`error_disabled`).
7. **cross-sender dedup**: orchestrator path `_run_report(auth_error)` → send →
   `mark_notified`; then simulate the `run_tick.py` last-resort recomputing the SAME
   `(kind, stage)` hash via `notify.build_digest` → assert `last_notified_hash`
   matches → last-resort would skip. Inverse (no prior commit) → last-resort sends.
8. **mailer integration** (no network): `run_tick.py`'s failure→model mapping builds a
   valid payload for each failure mode; `QUIVER_MAILER_DISABLE` short-circuits the POST.
- Re-run the **full existing suite** (`tests/test_units.py`, `tests/test_e2e.py`):
  all 169 + new must pass; the digest path must be byte-identical where asserted.

## 9. Rollout / prerequisites
- Set `RESEND_API_KEY` + `RESEND_FROM` (verified Resend domain) in `quiver.env` for the
  last-resort sender; the in-tick MCP path is unchanged. Confirm `NOTIFY_TO` (and
  optionally `NOTIFY_ALERTS_TO`) set, else config load raises when `enabled`.
- No new pip deps (mailer uses stdlib `urllib`).

## 10. Risks & mitigations
- **Double email across senders** → mitigated by the shared `(kind, stage)` hash +
  shared `notifications` table (§3, §6).
- **Alert spam (everything-on)** → per-day-per-stage dedup (§6).
- **Last-resort needs RESEND_API_KEY in Python env** (deviates from MCP-only) →
  isolated to `lib/mailer.py`, ops-layer only, best-effort, degrades to disabled if
  unset; `notify.py` purity invariant intact.
- **Email must never alter a tick** → mailer never raises; `_run_report` errors stay
  best-effort; last-resort never changes `run_tick.py` exit code; CloudWatch lines
  untouched.
- **Stale `from` / unverified domain** → both paths log a failure and continue; no
  silent tick change.

## 11. REVISIONS (post-/autoplan — these SUPERSEDE the sections above)

The 4-phase review (CEO/Design/Eng/DX, subagent-only — Codex unavailable) caught
real defects. Accepted changes, by the 6 decision principles:

**R1 (CRITICAL, supersedes §6 + adds to §7 `lib/ledger.py`) — `stage` joins the
dedup PK.** The alert hash keys on `(kind, stage, dry_run)`, but the `notifications`
table PK was only `(trade_date, kind)` → distinct stages sharing `kind="error"`
clobber one row (spam, or a real critical suppressed). Fix: migrate `notifications`
to PK `(trade_date, kind, stage)` (SQLite: recreate-table migration in
`lib/ledger._migrate`, default existing rows `stage=''`); change
`last_notified_hash(date, kind, stage)` and `mark_notified(date, kind, stage, hash,
recipients, now_iso)`; update the 2 call sites (`cmd_report`/`cmd_report_commit`) and
`run_tick.py`. `report-commit` gains `--stage`. Digest keeps `stage=''`. Also: the
row STORES the latest `event_detail` (overwrite) so a human inspecting the DB sees
the most-recent failure text even though the hash excludes it.

**R2 (CRITICAL/HIGH, adds to §7) — `tick.py send-test` + deploy acceptance gate.**
New subcommand `tick.py send-test [--kind digest|auth_error|halt|error] [--stage ...]
[--to ...]` builds a representative model via `notify.build_digest` and ACTUALLY
sends through `lib.mailer.send_email` using the EXACT production `RESEND_API_KEY` /
`RESEND_FROM` resolution the last-resort path uses; prints the Resend `id` or
`{"ok":False,error}` verbatim. It is the only thing that proves the from-domain is
verified + the key valid before a real 2am incident. DEPLOY.md: "after setup you
must receive a test alert within 30s, else your pager is broken — fix before live."
(Offline unit/e2e still use `QUIVER_MAILER_DISABLE`; send-test is the live gate.)

**R3 (HIGH, supersedes §7 run_tick.py) — last-resort robustness + testable seam.**
Extract `_maybe_alert(led, *, kind, stage, day, now_iso, event_detail, cfg=None,
send=mailer.send_email) -> dict`: build model → `last_notified_hash(day,kind,stage)`
→ if absent call injected `send` → `mark_notified` on success → return a status dict,
**never raises**. `main()` calls it per failure branch. Rules: (a) the WHOLE
last-resort block is `try/except Exception` and NEVER changes the return code; (b)
config is loaded defensively (`try: cfg=load_config() except Exception: cfg=None`)
and recipients/from fall back to `NOTIFY_ALERTS_TO`/`NOTIFY_TO`/`RESEND_FROM` env so
it works even when config validation is the thing that broke; (c) **no email on
`RunLockError`** (benign contention) and none on preflight `proceed:false`; (d) the
**timeout** branch fires `_maybe_alert` too; (e) email-status lines are emitted to
stdout AND **shipped to tick.log** (a `mailer_*` line is NOT in `_FILE_SKIP_KEYS`)
so CloudWatch can alarm on "the pager is down" — but they must not contain the
substring the plan-error filter keys on.

**R4 (HIGH, supersedes §7 mailer/config) — blank `from` on the HTTP path.** The MCP
path allows blank `from`; the Resend HTTP API does not. `lib.mailer` + the
last-resort resolve `from` = `RESEND_FROM` env → `cfg.notify.from_addr`; if BOTH
blank → `_emit({"event":"mailer_unconfigured","reason":"no verified from"})`, ship
it, and skip. Add a startup check in `run_tick.py`: if `on_error` is desired but
`RESEND_API_KEY`/`RESEND_FROM` absent → one loud `mailer_unconfigured` line
(shipped) so "the alerter itself is unconfigured" is an alertable state.

**R5 (HIGH, supersedes §5 + §7 notify) — make the email actually render.** `_shell`
emits a COMPLETE document, not a fragment: `<!DOCTYPE html>` + `<html lang="en">` +
`<head>` (charset utf-8, `viewport width=device-width`, `color-scheme light dark`,
`x-apple-disable-message-reformatting`, MSO DPI block). Layout is **table-based**
(outer 100% table → centered `width=640` inner table; cards/hero/banner are nested
tables with `role="presentation"`) so Outlook doesn't full-bleed. The status icon is
a **colored circle drawn with a table cell + border-radius + background** carrying an
ASCII-safe glyph — never emoji-dependent; emoji stay OUT of subjects. Add a hidden
**preheader** per kind (auth → "Broker auth expired — no orders placed. SSH in, run
/mcp."). Pills carry an explicit `+`/`−` sign (+ `▲`/`▼`) so severity/P&L is never
color-only (colorblind-safe); amber text uses `#b45309` (AA), `#d97706` for fills
only. `KIND_REGISTRY.body_fn` returns a **structured intermediate** (banner
title/subtitle, action steps, detail) that BOTH `_render_html` and `_render_text`
render from, so action steps can never diverge (test asserts `/mcp`+`SSH` in
`auth_error` text and `rm KILL` in `halt` text).

**R6 (HIGH, supersedes §6 subjects + §4) — front-load the signal; distinct alert
prefix.** Subjects lead with the payload, not `[Quiver] <date>`: digest →
`Quiver +1.8% · $102.40 — 1 buy/1 sell [DRY RUN]`; alerts →
`⚠ Quiver: broker auth expired — run /mcp` / `🛑 Quiver HALT: daily-loss kill`.
Critical alerts use a distinct, hard-filterable subject lead so they never read like
a routine digest. (Subject keeps it text-first; the emoji is decorative, not the
carrier.)

**R7 (MEDIUM, supersedes §4 + §7 config) — warning hiccups: USER DECISION (see
gate).** The default for best-effort `warning` hiccups (report/prune/reflect/protect)
is a user choice between (a) fold into the digest FYI section + opt-in separate
emails, (b) separate email per hiccup, (c) critical-only. Critical alerts
(auth/halt/hard-error) are ALWAYS on regardless. `config.notify` gains
`on_warning` accordingly.

**R8 (MEDIUM, adds to §7 + CLAUDE.md) — canonical stages + invariant carve-out.**
Define `STAGES` (frozenset) + fixed constants `AUTH_STAGE="broker_auth"`,
`HALT_STAGE="daily_loss_halt"` in `lib/notify.py`; import in `tick.py`/`run_tick.py`;
`build_digest` normalizes/rejects unknown stages; TICK.md lists the literal strings
inline at each STOP point (no global "infer the stage" convention — the cheap haiku
orchestrator copies constants, never infers). CLAUDE.md safety section states the
deliberate carve-out: "the TRADING path never sends; the ops supervisor MAY send
last-resort alerts via a separately-scoped RESEND key." A test asserts the trading
brain (`analyze`/`lib.signals`/`lib.memory`) never imports `lib.mailer`.

**R9 (MEDIUM, adds to §7) — deploy surface.** Update `.env.example` (RESEND_API_KEY,
RESEND_FROM, NOTIFY_ALERTS_TO — marked "required for the Python last-resort sender")
and `deploy/setup.sh` SSM fetch list; DEPLOY.md §0 elevates a verified `RESEND_FROM`
to a hard prerequisite (with the DNS-verify step) rather than "optional", being
honest that skipping it blinds orchestrator-crash paging.

**R10 (MEDIUM — sequencing, USER DECISION, see gate).** Land as one branch but
sequence: commit 1 = alerting plumbing (mailer, dedup-PK migration, toggles,
run_tick wiring, send-test, tests) with the EXISTING renderer untouched and the full
suite green; commit 2 = the design-system refactor on the now-stable interface. OR
one combined commit. (Safety-first split recommended.)

**Revised test matrix additions** (supersede/extend §8):
- Ledger: `last_notified_hash`/`mark_notified` isolate by `(date,kind,stage)` —
  `error/plan` and `error/prune` (warning) coexist WITHOUT clobbering; migration
  preserves old rows.
- run_tick `_maybe_alert`: dedup via temp ledger; injected `send` captures payload;
  a RAISING `send` is swallowed and the return status is unaffected; `RunLockError`
  and `proceed:false` → no send; config-load-raises → env fallback still sends.
- mailer: blank-from short-circuits; transport error → `{ok:False}` never raises.
- notify: full-doc (DOCTYPE/head/table) present; colored-circle (no emoji
  dependency); preheader per kind; pill carries `+/−`; HTML+text action-step parity;
  digest hash byte-identical to today (169 tests unchanged).
- import-graph: `lib.mailer` absent from trading-brain module imports.
- send-test: builds a valid payload through the production resolution (network off).

## 12. Out of scope
- S3 archival, SMS/Slack, broadcast/audience emails, per-trade (per-order) emails,
  changing trading logic, dry-run silencing of emails (digest still emails in dry-run,
  labeled).

---

## Decision Audit Trail

| # | Phase | Decision | Class | Principle | Rationale |
|---|-------|----------|-------|-----------|-----------|
| 1 | Eng | Add `stage` to `notifications` PK | Mechanical | P1/P5 | Dedup grain must match alert grain or it clobbers; only sound fix |
| 2 | DX/CEO | Add `tick.py send-test` live gate | Mechanical | P1 | Only way to prove the pager works before a real incident |
| 3 | Eng | Testable `_maybe_alert` + try/except in run_tick | Mechanical | P5 | Never change exit code; make every branch testable w/o claude |
| 4 | Eng/DX | Resolve `from` for HTTP, loud skip if blank | Mechanical | P5 | HTTP API 422s on blank from; silent loss at worst moment |
| 5 | Design | Full email doc + table layout + colored-circle icon | Mechanical | P1 | Email renders broken in Outlook/mobile as specified |
| 6 | Design | Front-load subjects + distinct critical prefix | Mechanical | P1/P5 | Lede buried; critical reads like routine digest |
| 7 | CEO | Warning-hiccup delivery default | **User Challenge** | n/a | Models say "everything-on" trains archive-on-sight; user's call |
| 8 | Eng/CEO | `STAGES` consts + import-graph test + CLAUDE.md carve-out | Mechanical | P4/P5 | Prevent cross-sender drift; enforce invariant in CI not comment |
| 9 | DX | Ship mailer status to CloudWatch; digest "pager armed" footer | Mechanical | P1 | Email must stay additive to CloudWatch, never the only channel |
| 10 | DX | `.env.example` + setup.sh + DEPLOY.md prereq | Mechanical | P1 | 3 new vars undocumented at source |
| 11 | CEO | Safety-plumbing-first vs combined commit | **Taste** | P6 | Reasonable either way; split lowers blast radius |

## GSTACK REVIEW REPORT

**Pipeline:** CEO → Design → Eng → DX (subagent-only; Codex CLI unavailable on this host → degraded dual-voice, tagged `[subagent-only]`).
**Plan:** unified email alerts (run-complete digest + on-error), defense-in-depth delivery (Resend MCP in-tick + Python last-resort HTTP in run_tick.py), shared Mobbin-inspired design system, per-(date,kind,stage) dedup.

**Consensus (X/6 dims):**
- CEO: strategy SOUND, conditional on R1 (dedup-PK), R2 (prove-it-sends), staying additive to CloudWatch. 1 User Challenge (warning fatigue).
- Design: 4/10 as an email-rendering spec — vocabulary good, email-client reality missing. → R5/R6 accepted.
- Eng: architecturally sound shape, NOT mergeable as written — R1 (critical dedup PK), R3 (run_tick robustness/exit-code), R4 (blank-from). All accepted.
- DX: 5/10 — can't test the pager (R2), generic copy (R8/R9), silent pager-down (R6/mailer-status). All accepted.

**Cross-phase themes (flagged independently by 2+ phases):**
1. **Dedup PK `(date,kind)` ≠ alert grain** — Eng+CEO+DX. CRITICAL. → R1.
2. **The pager is unprovable / can be silently dead** — DX+CEO. → R2 + R6.
3. **Cross-sender stage-string drift double-pages** — Eng+DX. → R8.

**Status:** issues_open → resolved-in-plan (R1–R10). Two items routed to the user
(R7 User Challenge, R10 taste). Implement after the gate.
