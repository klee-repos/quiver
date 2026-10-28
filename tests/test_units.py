#!/usr/bin/env python3
"""Plain-assert unit tests for the pure desk logic (no pytest dependency).

Run: .venv/bin/python tests/test_units.py

The file covers the sizing clamps, the alert renderer, the config, the ledger rollups,
the decision wall (plan §14.10), and the kept pure math (portfolio, goal, trend,
attribution, benchmark). It uses no network, no broker and no LLM.
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from lib import notify, sizing  # noqa: E402
from lib.config import ConfigError, load_config  # noqa: E402
from lib.ledger import Ledger  # noqa: E402

PASS = 0
FAIL = 0
SKIP = 0


def skip(name, reason):
    """Record a test that could NOT run, and say so out loud.

    A silently-skipped test is indistinguishable from a passing one, which is how
    an optional dependency turns into fake green. Every skip prints and is counted
    in the summary line (which tests/run_e2e.sh parses).
    """
    global SKIP
    SKIP += 1
    print(f"  SKIP {name}: {reason}")


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
    else:
        FAIL += 1
        print(f"  FAIL {name}: got {got!r}, want {want!r}")


def check_true(name, cond):
    check(name, bool(cond), True)


def check_raises(name, fn, exc=Exception):
    global PASS, FAIL
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        if isinstance(e, exc):
            PASS += 1
        else:
            FAIL += 1
            print(f"  FAIL {name}: raised {type(e).__name__}, want {exc.__name__}")
        return
    FAIL += 1
    print(f"  FAIL {name}: did not raise {exc.__name__}")


# --- resolve_buy_dollars (plan §14.6): min(gap * fraction, available) ---------------
# The caller computes `available` (buying power - buffer - cash floor - spent, §10.2 step 3)
# and the gap (room under the target). The function only clamps. Below the floor
# max(min_notional, MIN_SELL_NOTIONAL_USD) it returns (0.0, "below_min"). Never negative.
_rb = sizing.resolve_buy_dollars
check("buy: the gap binds when it is under available (500 of 1000)", _rb(500.0, 1.0, available=1000.0)[0], 500.0)
check("buy: available binds when it is under the gap (250 of 500)", _rb(500.0, 1.0, available=250.0)[0], 250.0)
check("buy: the fraction scales the gap (400 x 0.5 = 200)", _rb(400.0, 0.5, available=9000.0)[0], 200.0)
check("buy: the fraction scales before the available clamp (1000 x 0.5 = 500, cap 300)",
      _rb(1000.0, 0.5, available=300.0)[0], 300.0)
check_true("buy: a sized buy carries a str reason",
           isinstance(_rb(500.0, 1.0, available=1000.0)[1], str)
           and _rb(500.0, 1.0, available=1000.0)[1] != "below_min")
check("buy: under min_notional -> (0.0, below_min)", _rb(4.0, 1.0, available=100.0, min_notional=5.0),
      (0.0, "below_min"))
check("buy: exactly min_notional is sized ($5.00)", _rb(5.0, 1.0, available=100.0, min_notional=5.0)[0], 5.0)
check("buy: min_notional 0 still keeps the $1 broker floor ($0.50 -> below_min)",
      _rb(0.5, 1.0, available=100.0, min_notional=0.0), (0.0, "below_min"))
check("buy: the floor is the larger of min_notional and MIN_SELL_NOTIONAL_USD",
      _rb(sizing.MIN_SELL_NOTIONAL_USD, 1.0, available=100.0)[0], sizing.MIN_SELL_NOTIONAL_USD)
check("buy: available under the floor -> below_min (gap 50, available $3, floor $5)",
      _rb(50.0, 1.0, available=3.0, min_notional=5.0), (0.0, "below_min"))
check("buy: available 0 -> below_min", _rb(50.0, 1.0, available=0.0), (0.0, "below_min"))
check("buy: a negative gap is never a negative buy", _rb(-50.0, 1.0, available=100.0), (0.0, "below_min"))
check("buy: a negative available is never a negative buy", _rb(50.0, 1.0, available=-10.0), (0.0, "below_min"))
check("buy: a zero fraction buys nothing", _rb(50.0, 0.0, available=100.0), (0.0, "below_min"))
check_true("buy: the result never exceeds available (rounding)",
           _rb(10.005, 1.0, available=10.004)[0] <= 10.004)
# The gap comes from room_under_target: a name at 30 of a 45 target has $15 of room.
check("buy: room_under_target feeds the gap (45 - 30 = 15)",
      _rb(sizing.room_under_target(45.0, 30.0), 1.0, available=100.0)[0], 15.0)
check("buy: a name at its target has no room -> below_min",
      _rb(sizing.room_under_target(45.0, 50.0), 1.0, available=100.0), (0.0, "below_min"))
# §14.6 DELETE: the prose sizing parser and the signal, cadence, consistency and PTJ
# helpers are gone. A model never sizes an order.
for _gone in ("parse_sizing_to_dollars", "VALID_SIGNALS", "plan_action", "cooldown_ok",
              "within_action_cap", "is_material_change", "direction_of", "is_reversal",
              "reversal_rate", "count_discretionary_reversals", "strategy_consistency_verdict",
              "HARD_REVIEW_CEILING_MIN", "clamp_review_minutes", "marketable_limit_price",
              "whole_shares_for_dollars", "resolve_stop_price", "reward_risk_ratio",
              "risk_budget_cap", "downside_vol_scalar", "trend_gate_scalar", "derisk_trim_fraction"):
    check_true(f"sizing: {_gone} is deleted (§14.6)", not hasattr(sizing, _gone))
for _kept in ("resolve_target_sell_quantity", "room_under_target", "resolve_sell_quantity_min_notional",
              "MIN_SELL_NOTIONAL_USD", "MIN_NOTIONAL_HEADROOM", "resolve_sell_quantity", "resolve_buy_dollars"):
    check_true(f"sizing: {_kept} moved from signals (§14.6)", hasattr(sizing, _kept))

# --- resolve_sell_quantity ---
check("full close", sizing.resolve_sell_quantity(3.0, 1.0), 3.0)
check("trim half", sizing.resolve_sell_quantity(3.0, 0.5), 1.5)
check("never oversell", sizing.resolve_sell_quantity(3.0, 2.0), 3.0)
check("nothing held", sizing.resolve_sell_quantity(0.0, 1.0), 0.0)


# --- resolve_sell_quantity_min_notional (sub-$1 fractional sell guard) ---
# RH rejects any fractional order with notional < $1. A model trim on a thin
# position can land below that floor; we bump the qty UP to clear $1 (clamped to
# held), or skip cleanly when the WHOLE position is under the floor (dust).
_mn = sizing.resolve_sell_quantity_min_notional
# trim already clears $1 -> unchanged
check("trim clears floor", _mn(2.0, 1.0, 140.0), (1.0, "trim"))
# trim under $1 but whole position >= $1 -> bumped to 1/quote, clamped to held



def _bump_ok(held, raw, quote, min_notional=1.0):
    """Assert the INVARIANT a bumped sell must hold, not a magic quantity.

    The old cases pinned ``round(1.0/140.0, 6)``. That constant only clears the
    floor because quote=140 happens to round UP. `round()` truncates downward at
    other prices: 7 of the 12 live names produced a sub-$1 order and Robinhood
    rejected every one. A test that pins one lucky quote cannot see that. Assert
    what the broker actually requires.
    """
    qty, reason = _mn(held, raw, quote, min_notional=min_notional)
    return (reason == "bumped_to_min"
            and qty * quote >= min_notional      # clears the broker floor
            and qty <= held + 1e-12)             # long-only: never oversell


check("bumped to $1", _bump_ok(0.02, 0.005, 140.0), True)
# REGRESSION (2026-08-11): sweep the REAL live quotes. Under the old `round()`
# form ANET/BSOL/CEG/GEV/SMH/SPCX/VRT all landed under $1 and were rejected.
_live_quotes = [197.85, 30.98, 10.33, 278.36, 190.81, 1011.88,
                718.45, 572.93, 534.20, 133.29, 45.17, 281.81]
check("no live quote yields a sub-floor sell",
      all(_bump_ok(100.0, 0.000001, q) for q in _live_quotes), True)
# bumped qty never exceeds held (long-only invariant): a $1 bump would need more
# than held -> capped at held. Here held=0.008 (=$1.12, whole >= $1) but the
# cheapest qty clearing $1 is 1/140=0.007143 (< held), so we bump to 0.007143.
check("bump clamped to held", _bump_ok(0.008, 0.004, 140.0), True)
# whole position under $1 (dust) -> skip_dust, no error
check("dust whole pos <$1", _mn(0.004, 0.002, 140.0), (0.0, "skip_dust"))  # 0.004*140=$0.56
# no usable quote -> defensive skip (never synthesize a price to force a sell)
check("no quote -> skip", _mn(2.0, 1.0, None), (0.0, "skip_no_quote"))
check("zero quote -> skip", _mn(2.0, 1.0, 0.0), (0.0, "skip_no_quote"))
# nothing held / nothing to trim
check("nothing held", _mn(0.0, 1.0, 140.0), (0.0, "skip_nothing"))
check("nothing to trim", _mn(2.0, 0.0, 140.0), (0.0, "skip_nothing"))
# custom floor ($2) respected
check("custom $2 floor", _bump_ok(0.02, 0.005, 140.0, min_notional=2.0), True)
# held just over the floor: whole pos >= $1 so not dust; bump to the cheapest qty
# clearing $1 (1/140=0.007143), which is < held (0.0072) -> a sliver stays.
check("bump leaves sliver", _bump_ok(0.0072, 0.003, 140.0), True)

# --- F2: bump_to_min flag (TRIM=skip vs EXIT=bump) + raised economic floor ---
# TRIM semantics (bump_to_min=False): a sub-floor trim (raw < $5) whose WHOLE position clears
# the floor SKIPS (churn guard) instead of bumping up. held 0.05*140=$7 (>=$5, not dust); raw
# 0.01*140=$1.40 (< $5) -> skip_below_min.
check("F2 trim below $5 floor -> skip_below_min",
      _mn(0.05, 0.01, 140.0, min_notional=5.0, bump_to_min=False), (0.0, "skip_below_min"))
# a trim that CLEARS the raised floor is unchanged ('trim'): 0.05*140=$7 >= $5.
check("F2 trim clears raised floor -> trim",
      _mn(0.05, 0.05, 140.0, min_notional=5.0, bump_to_min=False), (0.05, "trim"))
# skip_dust (whole pos under floor) still PRECEDES the bump/skip choice (the review-flagged
# ordering): 0.004*140=$0.56 < $5 -> skip_dust regardless of bump_to_min.
check("F2 dust precedes skip_below_min",
      _mn(0.004, 0.002, 140.0, min_notional=5.0, bump_to_min=False), (0.0, "skip_dust"))
# EXIT semantics (bump_to_min=True, $1 floor): a sub-$1 sliver still BUMPS so wind-downs finish.
check("F2 exit still bumps at the $1 floor",
      _bump_ok(0.02, 0.005, 140.0, min_notional=1.0), True)
# default (bump_to_min omitted) is the pre-F2 bump behavior (back-compat).
check("F2 default bump_to_min=True (back-compat)",
      _bump_ok(0.02, 0.005, 140.0), True)



# ============================ CONFIG + ALERTS ==================================
import copy  # noqa: E402
import dataclasses as _dcs  # noqa: E402
import os  # noqa: E402
import re as _re_alert  # noqa: E402


def _write_config(d: dict) -> str:
    tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
    yaml.safe_dump(d, tmp)
    tmp.close()
    return tmp.name


def _base_config() -> dict:
    """The smallest valid config.yaml: the account, the KILL path and the risk block."""
    return {"account_number": "12345678", "kill_switch_file": "/tmp/bw_kill_test",
            "risk": {"daily_loss_halt_pct": 5.0, "min_buying_power_buffer": 5}}


def make_config(notify_block=None):
    d = _base_config()
    if notify_block is not None:
        d["notify"] = notify_block
    return load_config(_write_config(d))


def _tmp_ledger():
    return Ledger(Path(tempfile.mkdtemp()) / "ledger.db")


# --- notify.summarize ---
check("summarize collapses whitespace", notify.summarize("a\n\n  b   c"), "a b c")
check("summarize truncates w/ ellipsis", notify.summarize("x" * 1000, 10), "x" * 10 + "…")
check("summarize None -> empty", notify.summarize(None), "")

# --- notify.build_alert (plan §14.9): the three alert kinds -----------------------
_ALERT_BASE = {"date": "2026-05-30", "now_iso": "2026-05-30T16:00:00-04:00"}
_da = notify.build_alert({**_ALERT_BASE, "kind": "auth_error", "event_detail": "401 token expired"})
check("build_alert returns {telegram, content_hash, kind}", set(_da), {"telegram", "content_hash", "kind"})
check("build_alert keeps the auth_error kind", _da["kind"], "auth_error")
check("build_alert maps an unknown kind to error", notify.build_alert({**_ALERT_BASE, "kind": "digest"})["kind"], "error")
check("build_alert content_hash == content_hash(model)", _da["content_hash"],
      notify.content_hash({**_ALERT_BASE, "kind": "auth_error", "event_detail": "401 token expired"}))
# §14.9 DELETE: the email and digest renderers are gone; digest_hash is now content_hash.
for _gone in ("build_digest", "is_silent", "_render_telegram", "_render_telegram_digest", "_render_html",
              "_render_text", "_subject", "_shell", "_card", "_pill", "_pnl_pill", "_banner", "_hero",
              "_action_card", "_detail_block", "_ticker_card", "_preheader", "_circle", "_palette",
              "_esc", "_PALETTE", "_FONT", "_MONO", "_DEBATE_CAP", "_account_risk_line",
              "_unexplained_jumps_note", "_counts", "_traded_line", "_tg_actions", "_tg_risk_sentence",
              "_tg_sized", "_tg_order_phrase", "_tg_humanize", "_tg_join_clauses", "digest_hash"):
    check_true(f"notify: {_gone} is deleted (§14.9)", not hasattr(notify, _gone))
for _new in ("build_alert", "content_hash", "render_trade_card", "render_change_card", "render_brief",
             "render_execution_result"):
    check_true(f"notify: {_new} exists (§14.9)", callable(getattr(notify, _new, None)))

# Telegram alert prose: a human text, never a numbered list; dynamic text is escaped.
for _k, _ex in (("auth_error", {"event_detail": "401 <t> & x"}),
                ("halt", {"halted": True, "halt_reason": "-6% <x>"}),
                ("error", {"severity": "critical", "stage": "execute", "event_detail": "<boom> & bust"})):
    _r = notify.build_alert({**_ALERT_BASE, "kind": _k, **_ex})["telegram"]
    check_true(f"telegram {_k} under the cap", 0 < len(_r) <= 4096)
    check_true(f"telegram {_k} escapes raw < in detail",
               "<t>" not in _r and "<boom>" not in _r and "<x>" not in _r)
    check_true(f"telegram {_k} is prose, not a numbered step list",
               "1. " not in _r and "2. " not in _r and "•" not in _r)
    check_true(f"telegram {_k} leads with a bold headline sentence", _r.startswith("<b>"))
_auth_tg = notify.build_alert({**_ALERT_BASE, "kind": "auth_error", "event_detail": "401"})["telegram"]
check_true("telegram auth_error carries the re-auth command, no desktop",
           "deploy/gcp/rh-reauth.sh" in _auth_tg and "remotedesktop" not in _auth_tg)
_err_tg = notify.build_alert({**_ALERT_BASE, "kind": "error", "severity": "critical", "stage": "execute",
                              "event_detail": "the check blew up"})["telegram"]
check_true("telegram error names the stage and shows the detail",
           "execute" in _err_tg and "the check blew up" in _err_tg)
# §14.9: the halt blocks taps for the day and lifts with a resume_trading proposal; no `rm KILL`.
_halt_model = {**_ALERT_BASE, "kind": "halt", "event_detail": "equity fell 21%"}
_halt_tg = notify.build_alert(_halt_model)["telegram"]
_halt_steps = " ".join(notify.action_steps(_halt_model))
check_true("halt steps: the halt lifts with a resume_trading proposal", "resume_trading" in _halt_steps)
check_true("halt steps: no `rm KILL` recovery any more",
           not _re_alert.search(r"\brm\b", _halt_steps) and "KILL" not in _halt_steps)
check_true("halt telegram: no `rm KILL`, names resume_trading",
           "rm /opt/quiver/KILL" not in _halt_tg and "rm KILL" not in _halt_tg and "resume_trading" in _halt_tg)
check_true("halt steps: the halt blocks the taps for today", "today" in _halt_steps.lower())

# --- canonical stages and severity (§14.9) ---
check("stage: auth_error derives broker_auth", notify.stage_of({"kind": "auth_error"}), "broker_auth")
check("stage: halt derives daily_loss_halt", notify.stage_of({"kind": "halt"}), "daily_loss_halt")
check("stage: an explicit stage wins", notify.stage_of({"kind": "error", "stage": "snapshot"}), "snapshot")
check("stage: AUTH_STAGE / HALT_STAGE constants", (notify.AUTH_STAGE, notify.HALT_STAGE),
      ("broker_auth", "daily_loss_halt"))
check("stage: _CRITICAL_STAGES is the §14.9 set", set(notify._CRITICAL_STAGES),
      {"broker_auth", "daily_loss_halt", "monitor", "worker", "broker", "desk", "edgar", "snapshot",
       "execute", "path_unit"})
check("stage: _WARNING_STAGES is the §14.9 set", set(notify._WARNING_STAGES),
      {"levels", "drift", "expire", "queue", "reflect", "benchmark", "goal", "prune", "news",
       "transcripts", "brief"})
check("stage: STAGES is the union", set(notify.STAGES), set(notify._CRITICAL_STAGES) | set(notify._WARNING_STAGES))
check("severity: halt is critical", notify.severity_of({"kind": "halt"}), "critical")
check("severity: auth_error is critical", notify.severity_of({"kind": "auth_error"}), "critical")
check("severity: a warning stage with no severity is a warning",
      notify.severity_of({"kind": "error", "stage": "prune"}), "warning")
check("severity: a critical stage with no severity is critical",
      notify.severity_of({"kind": "error", "stage": "execute"}), "critical")
check("severity: an explicit warning wins", notify.severity_of({"kind": "error", "stage": "execute",
                                                                  "severity": "warning"}), "warning")
_m_warn = {**_ALERT_BASE, "kind": "error", "severity": "warning", "stage": "prune", "event_detail": "blip"}
check("warning -> no action steps", notify.action_steps(_m_warn), [])
check_true("warning telegram has no recovery paragraph", "What to do" not in notify.build_alert(_m_warn)["telegram"])
# non-finite equity/pct must fall back to an em-dash (a corrupt broker read must not print +nan%)
check("nan money -> em-dash", notify._fmt_money(float("nan")), "—")
check("inf pct -> em-dash", notify._fmt_pct(float("inf")), "—")

# --- content_hash: keyed on (kind, stage, severity); dry_run is gone (§14.9) ---
_m_err = {**_ALERT_BASE, "kind": "error", "severity": "critical", "stage": "execute",
          "event_detail": "the check blew up"}
_he = notify.content_hash(_m_err)
check_true("content_hash is 16 hex", len(_he) == 16 and all(c in "0123456789abcdef" for c in _he))
check("content_hash deterministic", notify.content_hash(copy.deepcopy(_m_err)), _he)
check("content_hash ignores event_detail, equity and time (cross-sender stable)",
      notify.content_hash({**_m_err, "event_detail": "other text", "equity": 9, "now_iso": "t2"}), _he)
check_true("content_hash changes with stage", notify.content_hash({**_m_err, "stage": "snapshot"}) != _he)
check_true("content_hash changes with kind", notify.content_hash({**_m_err, "kind": "auth_error"}) != _he)
check_true("content_hash changes with severity", notify.content_hash({**_m_err, "severity": "warning"}) != _he)
check("content_hash ignores a dry_run field (§14.9: dry_run is gone)",
      notify.content_hash({**_m_err, "dry_run": True}), notify.content_hash({**_m_err, "dry_run": False}))
check_true("halt hashes distinctly from an error", notify.content_hash({"kind": "halt"}) != _he)

from lib import telegram as _tgm  # noqa: E402
check("telegram _recipients: CSV/space split", _tgm._recipients("1, 2 3"), ["1", "2", "3"])
check("telegram _recipients: list drops junk", _tgm._recipients([1, None, " 2 "]), ["1", "2"])
check("telegram _recipients: scalar int", _tgm._recipients(9), ["9"])
check("telegram _recipients: None -> []", _tgm._recipients(None), [])
check("send_message no token -> not ok (never raises)",
      _tgm.send_message(token="", chat_ids="1", text="x")["ok"], False)
check("send_message no chats -> not ok",
      _tgm.send_message(token="t", chat_ids="", text="x")["ok"], False)
check_true("send_message unconfigured errors are labelled",
           "unconfigured" in _tgm.send_message(token="", chat_ids="1", text="x")["error"])
# offline disable: ok True (dedup row still writes), no network
_saved_disable = os.environ.get("QUIVER_TELEGRAM_DISABLE")
os.environ["QUIVER_TELEGRAM_DISABLE"] = "1"
_off = _tgm.send_message(token="t", chat_ids="42, 43", text="a\nb")
check("disable -> ok True (row still writes)", _off["ok"], True)
check_true("disable -> no send + chunk count", bool(_off.get("disabled")) and _off["chunks"] == 1)
os.environ["QUIVER_TELEGRAM_DISABLE"] = "0"
check("explicit-falsy disable is ignored", _tgm._disabled(), False)
if _saved_disable is None:
    del os.environ["QUIVER_TELEGRAM_DISABLE"]
else:
    os.environ["QUIVER_TELEGRAM_DISABLE"] = _saved_disable
# chunking: over-long single line hard-splits under the limit; line-join round-trips
_cks = _tgm._chunks("Z" * 8000, 3500)
check_true("chunks never exceed the limit",
           all(len(c) <= 3500 for c in _cks) and "".join(_cks) == "Z" * 8000)
_multi = "\n".join("row%d" % i for i in range(1500))
check("chunks split on line boundaries (round-trip)", "\n".join(_tgm._chunks(_multi, 3500)), _multi)
check("telegram _strip_html unescapes + drops tags", _tgm._strip_html("<b>A &amp; B</b>"), "A & B")
# resolve_env: alert override wins, else allowlist fallback, else empty
check("resolve_env: token + alert override",
      _tgm.resolve_env({"TELEGRAM_BOT_TOKEN": "tk", "TELEGRAM_ALERT_CHAT_IDS": "7,8",
                        "TELEGRAM_ALLOWED_CHAT_IDS": "9"}), ("tk", ["7", "8"]))
check("resolve_env: falls back to allowlist",
      _tgm.resolve_env({"TELEGRAM_BOT_TOKEN": "tk", "TELEGRAM_ALLOWED_CHAT_IDS": "9"}), ("tk", ["9"]))
check("resolve_env: empty env -> no creds", _tgm.resolve_env({}), ("", []))
# partial-delivery dedup semantics: ok tracks ONLY the PRIMARY chat. A dead SECONDARY sets
# `partial` but must NOT flip `ok` (else it re-spams the primary every run).
_orig_post = _tgm.post
try:
    _dead_chats = {"43"}
    _tgm.post = (lambda **k: ({"ok": False, "error": "http 403: blocked"}
                              if str(k.get("chat_id")) in _dead_chats else {"ok": True, "id": "1"}))
    _pr = _tgm.send_message(token="t", chat_ids="42, 43", text="hello")
    check("partial: ok tracks the healthy PRIMARY chat", _pr["ok"], True)
    check("partial: a failed secondary is flagged", _pr["partial"], True)
    _dead_chats = {"42"}  # primary dead -> ok False (re-send next run; at-least-once)
    check("partial: dead primary -> ok False",
          _tgm.send_message(token="t", chat_ids="42, 43", text="hi")["ok"], False)
finally:
    _tgm.post = _orig_post

# --- config: notify block fail-safe + validation (§14.8) ---
check("notify off when absent", make_config().notify.enabled, False)
check("notify on only when enabled is exactly True", make_config({"enabled": True}).notify.enabled, True)
check("enabled must be exactly True (string -> off)", make_config({"enabled": "yes"}).notify.enabled, False)
_cfg_def = make_config({"enabled": True})
check("on_error defaults ON", _cfg_def.notify.on_error, True)
check("on_warning defaults OFF", _cfg_def.notify.on_warning, False)
check("on_warning explicit true parsed", make_config({"enabled": True, "on_warning": True}).notify.on_warning, True)
check("on_error fail-safe (only explicit false disables)",
      make_config({"enabled": True, "on_error": "nope"}).notify.on_error, True)
check("on_error explicit false disables", make_config({"enabled": True, "on_error": False}).notify.on_error, False)
check("NotifyConfig keeps only enabled/on_error/on_warning (§14.8)",
      {f.name for f in _dcs.fields(_cfg_def.notify)}, {"enabled", "on_error", "on_warning"})
check("old email keys in notify are ignored, not loaded",
      make_config({"enabled": True, "to": ["a@b.com"], "alerts_to": ["p@b.com"], "on_complete": True,
                   "from": "x@y.com", "subject_prefix": "[Q]"}).notify.enabled, True)
# §14.8: no dry-run stage. The tap is the gate.
check_true("Config has no dry_run field (§14.8)", "dry_run" not in {f.name for f in _dcs.fields(_cfg_def)})
check_true("a legacy dry_run key does not create the field",
           not hasattr(load_config(_write_config({**_base_config(), "dry_run": True})), "dry_run"))
_kill_rel = load_config(_write_config({**_base_config(), "kill_switch_file": "KILL"}))
check_true("kill_switch_file: a relative path resolves against the config dir, not the cwd",
           os.path.isabs(_kill_rel.kill_switch_file) and _kill_rel.kill_switch_file.endswith("/KILL"))

# --- ledger: read-only rollups + notification dedup ---
_led = _tmp_ledger()
DAY = "2026-05-30"
check("get_baseline None before open", _led.get_baseline(DAY), None)
_led.get_or_create_baseline(DAY, 100.0, "2026-05-30T09:35:00-04:00")
_b = _led.get_baseline(DAY)
check_true("get_baseline reads back", _b is not None and _b.baseline_equity == 100.0 and _b.halted is False)
_rid = _led.new_ref_id()
_led.reserve_order(_rid, DAY, "AAPL", side="buy", type="market", dollar_amount=25.0, quantity=None, now_iso="t")
_ord = _led.get_order(_rid)
check("reserve_order: the leg reads back by ref_id", _ord["dollar_amount"], 25.0)
check("reserve_order: a new leg defaults to state 'proposed' (§14.7)", _ord["state"], "proposed")
check("reserve_order: a proposed leg has no submitted_at (§14.7)", _ord["submitted_at"], None)
check("no notify hash initially", _led.last_notified_hash(DAY, "error"), None)
_led.mark_notified(DAY, "error", "abc123", "42", "t")
check("notify hash recorded", _led.last_notified_hash(DAY, "error"), "abc123")
check("notify hash isolated by kind", _led.last_notified_hash(DAY, "halt"), None)
_led.mark_notified(DAY, "error", "def456", "42", "t2")
check("notify hash replaced when content changes", _led.last_notified_hash(DAY, "error"), "def456")
# stage-keyed dedup: same (date, kind) different stage -> independent rows (no clobber)
_led.mark_notified(DAY, "error", "errexec", "42", "t", stage="execute")
_led.mark_notified(DAY, "error", "errprune", "42", "t", stage="prune")
check("notify rows isolated by stage (execute)", _led.last_notified_hash(DAY, "error", "execute"), "errexec")
check("notify rows isolated by stage (prune)", _led.last_notified_hash(DAY, "error", "prune"), "errprune")
check("notify stage default '' isolated", _led.last_notified_hash(DAY, "error"), "def456")
for _gone in ("record_action", "day_actions", "day_orders", "clear_day"):
    check_true(f"ledger: {_gone} is deleted (§14.7)", not hasattr(Ledger, _gone))
# ============================ STORAGE RETENTION =============================
import os  # noqa: E402

from lib import storage  # noqa: E402
from lib.config import StorageConfig  # noqa: E402

_NOW_TS = 1_000_000.0
_DAY_S = 86400.0

# --- select_for_prune: boundary is deterministic (only strictly-older drops) ---
_entries = [
    ("keep_new", _NOW_TS - 1 * _DAY_S),
    ("at_cutoff", _NOW_TS - 7 * _DAY_S),   # exactly at the cutoff -> KEPT
    ("drop_old", _NOW_TS - 8 * _DAY_S),
]
check("prune selects only strictly-older", storage.select_for_prune(_entries, 7, _NOW_TS), ["drop_old"])
check("prune keep_days=0 disables", storage.select_for_prune(_entries, 0, _NOW_TS), [])
check("prune keep_days<0 disables", storage.select_for_prune(_entries, -5, _NOW_TS), [])
check("prune empty entries", storage.select_for_prune([], 7, _NOW_TS), [])

# --- prune_dir: real filesystem, deterministic clock, recursive ---
_pdir = Path(tempfile.mkdtemp())
(_pdir / "old.log").write_text("x")
(_pdir / "new.log").write_text("y")
(_pdir / "sub").mkdir()
(_pdir / "sub" / "old2.log").write_text("z")
os.utime(_pdir / "old.log", (_NOW_TS - 10 * _DAY_S, _NOW_TS - 10 * _DAY_S))
os.utime(_pdir / "new.log", (_NOW_TS - 1 * _DAY_S, _NOW_TS - 1 * _DAY_S))
os.utime(_pdir / "sub" / "old2.log", (_NOW_TS - 10 * _DAY_S, _NOW_TS - 10 * _DAY_S))
_psum = storage.prune_dir(_pdir, 7, now_ts=_NOW_TS)
check("prune_dir scanned all (recursive)", _psum["scanned"], 3)
check("prune_dir pruned the two old", _psum["pruned"], 2)
check("prune_dir no archival with local", _psum["archived"], 0)
check_true("prune_dir kept the new file", (_pdir / "new.log").exists())
check_true("prune_dir deleted old file", not (_pdir / "old.log").exists())
check_true("prune_dir deleted nested old file", not (_pdir / "sub" / "old2.log").exists())
_pmiss = storage.prune_dir(_pdir / "nope", 7, now_ts=_NOW_TS)
check("prune_dir missing dir is no-op", (_pmiss["scanned"], _pmiss["pruned"]), (0, 0))
_pdisabled = storage.prune_dir(_pdir, 0, now_ts=_NOW_TS)
check("prune_dir keep_days=0 prunes nothing", _pdisabled["pruned"], 0)


def _storage_cfg(**kw):
    base = {"archive_enabled": False, "archive_backend": "s3",
            "archive_bucket": "", "archive_prefix": ""}
    base.update(kw)
    return StorageConfig(retention_days=30, **base)


# --- get_archiver: default local; enabling the deferred S3 backend degrades to local ---
check("default archiver is local",
      type(storage.get_archiver(_storage_cfg())).__name__, "LocalArchiver")
check("enabled S3 (deferred) degrades to local, never raises",
      type(storage.get_archiver(_storage_cfg(archive_enabled=True, archive_bucket="b"))).__name__,
      "LocalArchiver")



# --- StorageConfig parsing + validation (fails safe) ---
def make_storage_config(storage_block):
    d = _base_config()
    if storage_block is not None:
        d["storage"] = storage_block
    return load_config(_write_config(d))

_sc_absent = make_storage_config(None).storage
check("storage defaults when absent", (_sc_absent.retention_days, _sc_absent.archive_enabled), (30, False))
check("storage retention parsed", make_storage_config({"retention_days": 7}).storage.retention_days, 7)
check("storage retention 0 allowed (disables)", make_storage_config({"retention_days": 0}).storage.retention_days, 0)
check_raises("negative retention raises", lambda: make_storage_config({"retention_days": -1}), ConfigError)
check_raises("non-int retention raises", lambda: make_storage_config({"retention_days": "lots"}), ConfigError)
check_raises("archive enabled without bucket raises",
             lambda: make_storage_config({"archive": {"enabled": True, "backend": "s3", "bucket": ""}}), ConfigError)
check("archive enabled with bucket ok",
      make_storage_config({"archive": {"enabled": True, "backend": "s3", "bucket": "bk"}}).storage.archive_bucket, "bk")

# ============================ DECISION MEMORY ===============================
from lib import memory  # noqa: E402

# --- directional_return (pure) ---
check("dir return up", memory.directional_return(100.0, 110.0), 0.1)
check("dir return down", round(memory.directional_return(100.0, 90.0), 4), -0.1)
check("dir return None price_now -> None", memory.directional_return(100.0, None), None)
check("dir return zero decision price -> None", memory.directional_return(0.0, 100.0), None)
check("dir return None decision price -> None", memory.directional_return(None, 100.0), None)

# --- is_hit (directional correctness; Hold ungradeable) ---
check("buy up = hit", memory.is_hit("Buy", 0.02), True)
check("buy down = miss", memory.is_hit("Buy", -0.02), False)
check("overweight up = hit", memory.is_hit("Overweight", 0.01), True)
check("sell down = hit", memory.is_hit("Sell", -0.03), True)
check("underweight up = miss", memory.is_hit("Underweight", 0.03), False)
check("hold is ungradeable", memory.is_hit("Hold", 0.05), None)
check("none-return ungradeable", memory.is_hit("Buy", None), None)

# --- build_scorecard (pure) ---
check("empty scorecard -> ''", memory.build_scorecard("AAPL", []), "")
_rows = [
    {"trade_date": "2026-05-28", "signal": "Buy", "directional_return": 0.012, "holding_days": 5,
     "realized_pnl": None, "rationale": "Services growth strong."},
    {"trade_date": "2026-05-21", "signal": "Sell", "directional_return": -0.03, "holding_days": 5,
     "realized_pnl": 1.5, "rationale": "Overbought."},
    {"trade_date": "2026-05-14", "signal": "Buy", "directional_return": None, "holding_days": None,
     "realized_pnl": None, "rationale": "Pending one."},
]
_sc = memory.build_scorecard("AAPL", _rows)
check_true("scorecard names ticker + counts", "AAPL" in _sc and "3 decision(s), 2 resolved" in _sc)
check_true("scorecard hit-rate 2/2 (buy-up + sell-down)", "2/2" in _sc)
check_true("scorecard avg move shown", "avg move" in _sc)
check_true("scorecard realized P&L line", "Realized P&L" in _sc and "$+1.50" in _sc)
check_true("scorecard shows pending row", "pending" in _sc)

# realized P&L is a NAME-level figure that `reflect` copies onto EVERY pending decision
# for a ticker when it closes; with multi-tranche buys that lands the IDENTICAL value on
# several rows. The scorecard must dedup the close (sum of DISTINCT values), not triple it.
_dup_rows = [
    {"trade_date": "2026-05-28", "signal": "Buy", "directional_return": 0.01, "holding_days": 3,
     "realized_pnl": 50.0, "rationale": "tranche 3"},
    {"trade_date": "2026-05-27", "signal": "Buy", "directional_return": 0.01, "holding_days": 4,
     "realized_pnl": 50.0, "rationale": "tranche 2"},
    {"trade_date": "2026-05-26", "signal": "Buy", "directional_return": 0.01, "holding_days": 5,
     "realized_pnl": 50.0, "rationale": "tranche 1"},
]
_sc_dup = memory.build_scorecard("MSFT", _dup_rows)
check_true("scorecard: one close across 3 tranches is NOT triple-counted ($50, not $150)",
           "$+50.00" in _sc_dup and "$+150.00" not in _sc_dup)
# genuinely-distinct closes still sum (two different realized values -> $80).
_distinct_rows = [
    {"trade_date": "2026-05-28", "signal": "Buy", "directional_return": 0.01, "holding_days": 3,
     "realized_pnl": 50.0, "rationale": "close A"},
    {"trade_date": "2026-05-20", "signal": "Buy", "directional_return": 0.01, "holding_days": 4,
     "realized_pnl": 30.0, "rationale": "close B"},
]
check_true("scorecard: distinct closes still sum ($50 + $30 = $80)",
           "$+80.00" in memory.build_scorecard("MSFT", _distinct_rows))

# --- ledger decisions/outcomes round-trip + scorecard integration ---
_mled = _tmp_ledger()
_id1 = _mled.record_decision(trade_date="2026-05-20", ticker="AAPL",
                             decided_at="2026-05-20T10:00:00-04:00", signal="Buy", intent="buy",
                             entry_price=190.0, decision_price=190.0, rationale="Phased entry.")
_id2 = _mled.record_decision(trade_date="2026-05-21", ticker="AAPL",
                             decided_at="2026-05-21T10:00:00-04:00", signal="Hold", intent="hold",
                             decision_price=192.0, rationale="Wait.")
check_true("record_decision returns sequential ids", isinstance(_id1, int) and _id2 == _id1 + 1)
check("get_decision reads back signal", _mled.get_decision(_id1)["signal"], "Buy")
check("two pending at/before cutoff date", len(_mled.pending_outcome_decisions("2026-05-25")), 2)
check("none pending before earliest trade_date", len(_mled.pending_outcome_decisions("2026-05-19")), 0)
_mled.record_outcome(_id1, resolved_at="2026-05-27T10:00:00-04:00", holding_days=5,
                     directional_return=0.02, scored_against="directional")
check("one pending after resolving id1", len(_mled.pending_outcome_decisions("2026-05-25")), 1)
_dw = _mled.decisions_with_outcomes("AAPL")
check("decisions_with_outcomes newest first", [r["trade_date"] for r in _dw], ["2026-05-21", "2026-05-20"])
check("resolved row carries directional", _dw[1]["directional_return"], 0.02)
check("pending row directional is None", _dw[0]["directional_return"], None)
check_true("integrated scorecard reflects 1 resolved",
           "2 decision(s), 1 resolved" in memory.build_scorecard("AAPL", _dw))

# --- T1 REGRESSION: un-poison the scorecard --------------------------------
# A SKIP decision puts no capital at risk; grading it directionally was the live
# poison (CBRS Underweight/skip into a +9% move scored as a MISS -> fictitious low
# hit-rate -> the loop steers AWAY from working names). It must be excluded.
_poison_rows = [
    {"trade_date": "2026-06-02", "signal": "Buy", "intent": "buy",
     "directional_return": 0.02, "holding_days": 5, "rationale": "real long"},
    {"trade_date": "2026-06-01", "signal": "Underweight", "intent": "skip",
     "directional_return": 0.09, "holding_days": 5, "rationale": "no position"},
]
_psc = memory.build_scorecard("CBRS", _poison_rows)
check_true("REGRESSION: skip excluded from hit-rate (1/1 not 1/2)", "1/1" in _psc and "1/2" not in _psc)
check_true("REGRESSION: skip-into-rising-price is not a miss",
           "(100%)" in _psc)
check("is_hit still pure (skip filtering is scorecard-level, not is_hit)",
      memory.is_hit("Underweight", 0.09), False)

# --- T1: benchmark-relative (alpha) grading, not absolute beta --------------
# A long that rose +10% absolute but UNDERPERFORMED the benchmark (alpha -2%) is a
# SKILL miss; absolute grading would call it a hit (pure market beta).
_alpha_rows = [
    {"trade_date": "2026-06-03", "signal": "Buy", "intent": "buy",
     "directional_return": 0.10, "alpha": -0.02, "holding_days": 5, "rationale": "lagged QQQ"},
]
_asc = memory.build_scorecard("SMH", _alpha_rows)
check("score_return prefers alpha when present", memory.score_return(_alpha_rows[0]), -0.02)
check("score_return falls back to directional", memory.score_return({"directional_return": 0.05}), 0.05)
check_true("alpha-graded Buy that lagged benchmark is a MISS (0/1)", "0/1" in _asc)
check_true("scorecard labels metric basis excess-vs-benchmark", "excess-vs-benchmark" in _asc)

# --- T1: basis surfaced per recent decision (cross-day learning) ------------
_basis_rows = [
    {"trade_date": "2026-06-04", "signal": "Buy", "intent": "buy", "basis": "power_bottleneck",
     "directional_return": 0.03, "holding_days": 5, "rationale": "CEG PPA"},
]
check_true("scorecard surfaces the strategy basis tag", "[power_bottleneck]" in memory.build_scorecard("CEG", _basis_rows))



# ============================ RISK CONFIG ======================================
def make_loop_config(risk_extra=None):
    """A config with extra ``risk:`` keys (the risk-config helper; the loop block is gone)."""
    d = _base_config()
    if risk_extra:
        d["risk"].update(risk_extra)
    return load_config(_write_config(d))


# --- config: the min_trade_notional knob parses + validates (default when absent; reject negative) ---
check("config: min_trade_notional defaults 5 when absent", make_loop_config().risk.min_trade_notional, 5.0)
check("config: min_trade_notional parses explicit value",
      make_loop_config(risk_extra={"min_trade_notional": 10}).risk.min_trade_notional, 10.0)
check("config: min_trade_notional 0 disables (the $1 broker floor still applies at the call sites)",
      make_loop_config(risk_extra={"min_trade_notional": 0}).risk.min_trade_notional, 0.0)
check_raises("config: negative min_trade_notional raises",
             lambda: make_loop_config(risk_extra={"min_trade_notional": -1}), ConfigError)
check_raises("config: non-number min_trade_notional raises",
             lambda: make_loop_config(risk_extra={"min_trade_notional": "lots"}), ConfigError)
check_raises("config: negative rebalance_drift_band_pct raises",
             lambda: make_loop_config(risk_extra={"rebalance_drift_band_pct": -1}), ConfigError)
check_raises("config: daily_loss_halt_pct <= 0 raises",
             lambda: make_loop_config(risk_extra={"daily_loss_halt_pct": 0}), ConfigError)
_cfg_yaml = yaml.safe_load((Path(__file__).resolve().parent.parent / "config.yaml").read_text(encoding="utf-8"))
check("config: the 20% daily-loss halt in config.yaml is kept", float(_cfg_yaml["risk"]["daily_loss_halt_pct"]), 20.0)
check_true("config: config.yaml has no dry_run key (§14.8)", "dry_run" not in _cfg_yaml)
# §14.8: the deleted RiskConfig fields are gone; a legacy key in the risk block is ignored.
_rc_fields = {f.name for f in _dcs.fields(make_loop_config().risk)}
for _gone in ("max_actions_per_ticker_per_day", "max_analyses_per_ticker_per_day", "rebalance_enabled",
              "reconcile_unmanaged", "consistency_enabled", "max_discretionary_reversals",
              "consistency_flip_window", "loss_catalyst_pct", "conviction_rebalance_min_delta_pct"):
    check_true(f"config: RiskConfig.{_gone} is deleted (§14.8)", _gone not in _rc_fields)
check("config: a legacy risk key is ignored, not loaded",
      make_loop_config(risk_extra={"conviction_rebalance_min_delta_pct": 3.5}).risk.min_trade_notional, 5.0)
# §14.8: the desk: block start values, each checked against its §10.3 range.
_dd = make_loop_config().desk
check("config: desk defaults (§14.8)",
      (_dd.requote_tolerance_pct, _dd.margin_of_safety_pct, _dd.valuation_discount_rate,
       _dd.valuation_terminal_growth, _dd.valuation_years), (1.5, 15.0, 0.09, 0.03, 10))
_cfg_desk_bad = {**_base_config(), "desk": {"requote_tolerance_pct": 99}}
check_raises("config: a desk value outside its range raises",
             lambda: load_config(_write_config(_cfg_desk_bad)), ConfigError)


# ============================ THE DECISION WALL (plan §14.10) ===================
# THE INVARIANT: every module under lib/ sits on the ANALYSIS side of the wall. It must
# not IMPORT a sizing, limit or approval module (Mechanism A: AST over the real import
# nodes, because modules describe the wall in their docstrings and a text scan would
# false-positive on "never imports lib.sizing"). It must not NAME a broker tool, a trading
# limit or the approval entry points (Mechanism B: a word-boundary source scan, so the
# ledger's active_target_portfolio is not read as get_portfolio).
#
# The roster is ENUMERATED FROM THE FILESYSTEM. A NEW module is INSIDE the wall by
# default and must be exempted ON PURPOSE below. A grant is ITEMIZED: it names the exact
# wall items the module may use, so the module stays walled against every other item.
# Meta-checks keep the roster and the grants honest: the roster has a floor (a bad cwd
# would make every check pass with no module), a grant must name a module that exists,
# and a grant must be load-bearing (the dead-grant check).
import ast as _ast  # noqa: E402
import re as _re  # noqa: E402  (used by the source scans below and later blocks)
import subprocess as _wsp  # noqa: E402

_REPO = Path(__file__).resolve().parent.parent

_WALL_IMPORTS = {"lib.sizing", "lib.config", "lib.desk.approvals", "lib.desk.execution"}
_BROKER_TOOLS = ("get_portfolio", "get_equity_positions", "get_equity_quotes", "get_equity_orders",
                 "review_equity_order", "place_equity_order", "cancel_equity_order",
                 "get_trade_approval_setting", "get_trade_approvals", "get_accounts")
_WALL_SYMBOLS = ("buying_power", "remaining_daily_cap", "RiskConfig", "handle_tap", "apply_change") + _BROKER_TOOLS

# The ONLY opt-outs. Value = the exact wall items that module may reach for; every other
# item still applies to it. The plan's §14.10 list is the start; the dead-grant check and
# the ungranted-item check below set each grant to what the code really does. The
# changes from the plan list are in the comments (and in the run's plan_deviations).
_WALL_GRANTS = {
    "lib/config.py":         {"RiskConfig"},
    # broker_snapshots has a buying_power column.
    "lib/desk/store.py":     {"buying_power"},
    "lib/desk/proposals.py": {"lib.sizing", "lib.config", "buying_power", "apply_change"},
    # execution.py parses the raw broker tool results keyed by tool name (§11.2). The plan
    # list also names review_equity_order and get_trade_approvals; execution.py names
    # neither (broker_guard records the review), so the dead-grant check drops them.
    "lib/desk/execution.py": {"lib.sizing", "lib.config", "buying_power", "get_portfolio",
                              "get_equity_positions", "get_equity_quotes", "get_equity_orders",
                              "place_equity_order", "get_trade_approval_setting"},
    "lib/desk/approvals.py": {"lib.config", "handle_tap", "apply_change"},
    "lib/desk/settings.py":  {"lib.config"},
    # The snapshot field (the desk tool is portfolio_view). tools.py also loads the config
    # (load_config) for the runtime; the plan list lacks lib.config.
    "lib/desk/tools.py":     {"lib.config", "buying_power"},
    # Not in the plan list: agent.py loads the config (load_config) for the runtime.
    "lib/desk/agent.py":     {"lib.config"},
    # Not in the plan list: the attribution report (moved out of tick.py so the desk tools
    # never import tick) reads config.yaml for the strategy book fallback.
    "lib/factor_report.py":  {"lib.config"},
    # The plan list grants lib/sizing.py {"buying_power"}; sizing.py no longer names it
    # (the caller passes `available`, §14.6), so the dead-grant check drops that grant.
}
_RESEARCH = ["lib/desk/%s.py" % m for m in ("edgar", "sections", "xbrl", "valuation", "transcripts",
             "news", "etf", "documents", "findings", "events", "levels")]
# W2: a research module also imports none of these.
_RESEARCH_FORBIDDEN = _WALL_IMPORTS | {"lib.desk.proposals", "lib.desk.tools", "lib.desk.agent",
                                       "lib.desk.outbox", "lib.telegram", "subprocess"}


def _package_of(relpath):
    """The dotted package of a repo-relative .py path (lib/desk/x.py -> lib.desk)."""
    parts = list(Path(relpath).with_suffix("").parts)
    return parts[:-1]


def _imported_modules(src: str, relpath=None, siblings=False) -> set:
    """Every module a source imports: absolute, relative (resolved when relpath is given),
    `from X import name` as X.name, and the string argument of importlib.import_module /
    __import__. With ``siblings``, a bare `import name` also counts as <package>.name (a
    runner script imports its sibling files that way; §14.12)."""
    mods = set()
    pkg = _package_of(relpath) if relpath else None
    for _node in _ast.walk(_ast.parse(src)):
        if isinstance(_node, _ast.Import):
            for a in _node.names:
                mods.add(a.name)
                if siblings and pkg and "." not in a.name:
                    mods.add(".".join(pkg + [a.name]))
        elif isinstance(_node, _ast.ImportFrom):
            if _node.level:
                if pkg is None:
                    base = "." * _node.level + (_node.module or "")
                    mods.add(base)
                    continue
                keep = len(pkg) - (_node.level - 1)
                if keep < 0:
                    continue
                base = ".".join(pkg[:keep] + ([_node.module] if _node.module else []))
            else:
                base = _node.module
            if base:
                mods.add(base)
            # `from lib import sizing` binds lib.sizing, but the node module is only "lib".
            # Record module.name too, or that whole import form crosses the wall unseen.
            mods.update(f"{base}.{a.name}" if base else a.name for a in _node.names)
        elif isinstance(_node, _ast.Call):
            _f = _node.func
            _fname = _f.attr if isinstance(_f, _ast.Attribute) else (_f.id if isinstance(_f, _ast.Name) else "")
            if (_fname in ("import_module", "__import__") and _node.args
                    and isinstance(_node.args[0], _ast.Constant) and isinstance(_node.args[0].value, str)):
                mods.add(_node.args[0].value)
    return mods


def _hits(mods, targets) -> set:
    """The targets that `mods` imports: the module itself or a name inside it."""
    return {t for t in targets if any(m == t or m.startswith(t + ".") for m in mods)}


def _wall_violations(src: str, relpath=None) -> set:
    """Every wall item a module actually reaches for (imports + limit/broker symbols)."""
    return (_hits(_imported_modules(src, relpath), _WALL_IMPORTS)
            | {s for s in _WALL_SYMBOLS if _re.search(rf"\b{_re.escape(s)}\b", src)})


# --- the scanner binds: each import form and symbol form below is caught ---------------
# Without these, a scanner that saw nothing would make every wall check pass.
_scan = (lambda src, rel="lib/desk/news.py", **kw: _imported_modules(src, rel, **kw))
check("wall scanner: `from lib.desk import approvals`", _hits(_scan("from lib.desk import approvals"),
                                                              _WALL_IMPORTS), {"lib.desk.approvals"})
check("wall scanner: `import lib.sizing as s`", _hits(_scan("import lib.sizing as s"), _WALL_IMPORTS), {"lib.sizing"})
check("wall scanner: `from lib.config import Config`", _hits(_scan("from lib.config import Config"),
                                                             _WALL_IMPORTS), {"lib.config"})
check("wall scanner: a relative `from . import tools` in lib/desk", _hits(_scan("from . import tools"),
                                                                          _RESEARCH_FORBIDDEN), {"lib.desk.tools"})
check("wall scanner: a relative `from .execution import check`",
      _hits(_scan("from .execution import check"), _WALL_IMPORTS), {"lib.desk.execution"})
check("wall scanner: a lazy import inside a function",
      _hits(_scan("def f():\n    from lib import telegram\n"), _RESEARCH_FORBIDDEN), {"lib.telegram"})
check("wall scanner: importlib.import_module('lib.sizing')",
      _hits(_scan("import importlib\nimportlib.import_module('lib.sizing')"), _WALL_IMPORTS), {"lib.sizing"})
check("wall scanner: a sibling `import chat_history` in deploy/runner (siblings on)",
      _hits(_scan("import chat_history", "deploy/runner/chat_bridge.py", siblings=True),
            {"deploy.runner.chat_history"}), {"deploy.runner.chat_history"})
check("wall scanner: a symbol is matched on a word boundary",
      _wall_violations("x = snap['buying_power']\ny = led.active_target_portfolio(1)\n"), {"buying_power"})
check("wall scanner: `subprocess` is caught for a research module",
      _hits(_scan("import subprocess"), _RESEARCH_FORBIDDEN), {"subprocess"})
check("wall scanner: `lib.desk.store` is not a forbidden module",
      _hits(_scan("from lib.desk import store"), _RESEARCH_FORBIDDEN), set())

# --- the old 1336 check, renamed (§14.10): lib/sizing.py imports nothing from lib ---
_sizsrc = (_REPO / "lib" / "sizing.py").read_text(encoding="utf-8")
_siz_lib = {m for m in _imported_modules(_sizsrc, "lib/sizing.py") if m == "lib" or m.startswith("lib.")}
check("lib/sizing.py imports nothing from lib", sorted(_siz_lib), [])
check_true("lib/signals.py is gone (renamed to lib/sizing.py, §14.6)", not (_REPO / "lib" / "signals.py").exists())

# --- W1: the roster, the stale-grant and the dead-grant checks -----------------------
_WALL_ROSTER = sorted(p.relative_to(_REPO).as_posix() for p in _REPO.glob("lib/**/*.py")
                      if "__pycache__" not in p.parts)
# Meta-guard: an empty/short roster would make every wall check below pass VACUOUSLY.
check_true(f"wall W1: roster enumerates lib/ ({len(_WALL_ROSTER)} found, want >= 45)", len(_WALL_ROSTER) >= 45)
# Meta-guard: a grant naming a module that no longer exists is a stale exemption.
for _g in sorted(_WALL_GRANTS):
    check_true(f"wall W1: grant target {_g} still exists (no stale exemption)", _g in _WALL_ROSTER)
# The deleted modules' grants are gone (§14.10 W1).
for _g in ("lib/risk.py", "lib/wall_replay.py", "lib/learn.py", "lib/reflect_memory.py", "lib/ledger.py",
           "lib/signals.py"):
    check_true(f"wall W1: no grant for {_g}", _g not in _WALL_GRANTS)
for _mod in _WALL_ROSTER:
    _src = (_REPO / _mod).read_text(encoding="utf-8")
    _granted = _WALL_GRANTS.get(_mod, set())
    _reached = _wall_violations(_src, _mod)
    _bad = _reached - _granted
    check_true(f"{_mod}: reaches no ungranted limit/broker item (decision wall)"
               + (f" -> {sorted(_bad)}; it is analysis-side, or add an itemized grant" if _bad else ""),
               not _bad)
    # Meta-guard: a grant the module no longer needs must be REMOVED, not left to
    # quietly widen the opt-out surface.
    _dead = _granted - _reached
    check_true(f"{_mod}: every wall grant is load-bearing"
               + (f" -> drop {sorted(_dead)} from _WALL_GRANTS" if _dead else ""), not _dead)

# --- W2: the research modules get no grant and stay on the analysis side --------------
for _mod in _RESEARCH:
    _p = _REPO / _mod
    check_true(f"wall W2: research module {_mod} exists", _p.is_file())
    if not _p.is_file():
        continue
    _src = _p.read_text(encoding="utf-8")
    check_true(f"wall W2: {_mod} has no grant", _mod not in _WALL_GRANTS)
    _bad_imp = _hits(_imported_modules(_src, _mod), _RESEARCH_FORBIDDEN)
    check_true(f"wall W2: {_mod} imports none of the forbidden modules"
               + (f" -> {sorted(_bad_imp)}" if _bad_imp else ""), not _bad_imp)
    _bad_sym = [s for s in _BROKER_TOOLS if _re.search(rf"\b{_re.escape(s)}\b", _src)]
    check_true(f"wall W2: {_mod} names no broker tool" + (f" -> {_bad_sym}" if _bad_sym else ""), not _bad_sym)

# --- W3: the desk tools (§5.4) can read, queue a snapshot, and nothing else ------------
_TOOLS_PATH = _REPO / "lib" / "desk" / "tools.py"
_tools_src = _TOOLS_PATH.read_text(encoding="utf-8")
_tools_tree = _ast.parse(_tools_src)
_W3_NAMES = {"approvals", "execution", "handle_tap", "apply_change", "subprocess", "Popen", "shutil",
             "write_text", "write_bytes",
             # A dynamic import can load lib.desk.execution from a built string.
             "importlib", "import_module", "__import__"}
def _w3_identifiers(src, rel):
    """The W3 names that a source imports or uses as an identifier."""
    bad = set()
    for m in _imported_modules(src, rel):
        bad |= set(m.split(".")) & _W3_NAMES
        if m in ("os.system", "os.popen"):
            bad.add(m)
    for node in _ast.walk(_ast.parse(src)):
        if isinstance(node, _ast.Name) and node.id in _W3_NAMES:
            bad.add(node.id)
        elif isinstance(node, _ast.Attribute):
            if node.attr in _W3_NAMES:
                bad.add(node.attr)
            if isinstance(node.value, _ast.Name) and node.value.id == "os" and node.attr in ("system", "popen"):
                bad.add(f"os.{node.attr}")
        elif isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)) and node.name in _W3_NAMES:
            bad.add(node.name)
    return bad


def _open_mode(call):
    """The mode of an open() call: a str, None when the call is not an open(), or False
    when the mode is not a constant (then the check cannot prove it reads only)."""
    _f = call.func
    if isinstance(_f, _ast.Name) and _f.id == "open":
        pos = 1
    elif isinstance(_f, _ast.Attribute) and _f.attr == "open":
        pos = 1 if isinstance(_f.value, _ast.Name) and _f.value.id in ("io", "builtins", "os", "codecs") else 0
    else:
        return None
    node = call.args[pos] if len(call.args) > pos else next(
        (k.value for k in call.keywords if k.arg in ("mode", "flags")), None)
    if node is None:
        return "r"
    if isinstance(node, _ast.Constant) and isinstance(node.value, str):
        return node.value
    return False


def _w3_write_opens(src):
    """Each open() call whose mode has w, a, x or + (or is not a constant)."""
    out = []
    for node in _ast.walk(_ast.parse(src)):
        if isinstance(node, _ast.Call):
            mode = _open_mode(node)
            if mode is not None and (mode is False or any(c in mode for c in "wax+")):
                out.append(f"line {node.lineno}: mode {mode!r}")
    return out


check("wall W3: tools.py names no approval, execution, process or write identifier",
      sorted(_w3_identifiers(_tools_src, "lib/desk/tools.py")), [])
check("wall W3: tools.py opens no file for write (mode with w, a, x or +)", _w3_write_opens(_tools_src), [])
# The W3 scanner binds on each form it must refuse.
for _w3_src, _w3_want in (("from lib.desk import approvals", {"approvals"}),
                          ("from lib.desk.execution import check", {"execution"}),
                          ("import subprocess", {"subprocess"}), ("import shutil", {"shutil"}),
                          ("from subprocess import Popen", {"subprocess", "Popen"}),
                          ("import os\nos.system('x')", {"os.system"}), ("import os\nos.popen('x')", {"os.popen"}),
                          ("def f(p):\n    p.write_text('x')", {"write_text"}),
                          ("def f(p):\n    p.write_bytes(b'x')", {"write_bytes"}),
                          ("def f(c):\n    return c.handle_tap(1)", {"handle_tap"}),
                          ("def f(c):\n    apply_change(c)", {"apply_change"}),
                          ("import importlib", {"importlib"}),
                          ("from importlib import import_module", {"importlib", "import_module"}),
                          ("def f():\n    return importlib.import_module('lib.desk.' + 'exec' + 'ution')",
                           {"importlib", "import_module"}),
                          ("def f():\n    return __import__('lib.desk.' + 'exec' + 'ution')", {"__import__"})):
    check(f"wall W3 scanner: {_w3_src.splitlines()[-1].strip()!r} is caught",
          _w3_identifiers(_w3_src, "lib/desk/tools.py") >= _w3_want, True)
for _w3_src in ("open('f', 'w')", "open('f', mode='a')", "open('f', 'r+')", "open('f', 'xb')",
                "from pathlib import Path\nPath('f').open('w')", "open('f', m)"):
    check_true(f"wall W3 scanner: {_w3_src.splitlines()[-1]!r} is a write open", _w3_write_opens(_w3_src))
for _w3_src in ("open('f')", "open('f', 'r')", "open('f', 'rb')", "open('f', encoding='utf-8')"):
    check(f"wall W3 scanner: {_w3_src!r} is a read open", _w3_write_opens(_w3_src), [])

# broker_jobs: tools.py may queue a 'snapshot' job and no other kind. Check the SQL text in
# tools.py, and every store function it calls that inserts into broker_jobs.
_bj_sql = [n.value for n in _ast.walk(_tools_tree) if isinstance(n, _ast.Constant) and isinstance(n.value, str)
           and "broker_jobs" in n.value and "insert" in n.value.lower()]
check("wall W3: tools.py SQL inserts into broker_jobs only with kind 'snapshot'",
      [s for s in _bj_sql if "'snapshot'" not in s], [])
_store_tree = _ast.parse((_REPO / "lib" / "desk" / "store.py").read_text(encoding="utf-8"))
_store_bj = {}
for _fn in _store_tree.body:
    if isinstance(_fn, _ast.FunctionDef):
        _sql = [n.value for n in _ast.walk(_fn) if isinstance(n, _ast.Constant) and isinstance(n.value, str)
                and "broker_jobs" in n.value and "insert" in n.value.lower()]
        if _sql:
            # snapshot-only: every INSERT names the 'snapshot' kind as a literal.
            _store_bj[_fn.name] = all("'snapshot'" in s for s in _sql)
check_true("wall W3 (sanity): store.enqueue_snapshot_job inserts a 'snapshot' broker job",
           _store_bj.get("enqueue_snapshot_job") is True)
_tools_calls = {n.func.attr for n in _ast.walk(_tools_tree)
                if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute)}
check("wall W3: tools.py calls no store function that inserts a non-snapshot broker job",
      sorted(f for f in _tools_calls if f in _store_bj and not _store_bj[f]), [])

# --- W4: one approver, one executor -------------------------------------------------
# "Tracked" = the files git tracks plus the new files it does not ignore (the build adds
# them before the commit).
_TRACKED_PY = sorted(set(_wsp.run(["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "*.py"],
                                  cwd=_REPO, capture_output=True, text=True, check=True).stdout.split()))
_TRACKED_PY = [p for p in _TRACKED_PY if (_REPO / p).is_file()]
check_true(f"wall W4 (sanity): tracked .py files found ({len(_TRACKED_PY)})", len(_TRACKED_PY) >= 60)
_IMPORTS_OF = {}
for _p in _TRACKED_PY:
    try:
        _IMPORTS_OF[_p] = _imported_modules((_REPO / _p).read_text(encoding="utf-8"), _p, siblings=True)
    except SyntaxError as _e:
        _IMPORTS_OF[_p] = set()
        check_true(f"wall W4: {_p} parses ({_e})", False)
_is_test = (lambda p: p.startswith("tests/"))
_appr_importers = sorted(p for p, ms in _IMPORTS_OF.items() if _hits(ms, {"lib.desk.approvals"})
                         and p != "lib/desk/approvals.py")
check("wall W4: only deploy/runner/chat_bridge.py and tests import lib.desk.approvals",
      [p for p in _appr_importers if p != "deploy/runner/chat_bridge.py" and not _is_test(p)], [])
check_true("wall W4 (sanity): chat_bridge.py imports lib.desk.approvals",
           "deploy/runner/chat_bridge.py" in _appr_importers)
_exec_importers = sorted(p for p, ms in _IMPORTS_OF.items() if _hits(ms, {"lib.desk.execution"})
                         and p != "lib/desk/execution.py")
check("wall W4: only tick.py, run_broker.py and tests import lib.desk.execution",
      [p for p in _exec_importers if p not in ("tick.py", "deploy/runner/run_broker.py") and not _is_test(p)], [])
check_true("wall W4 (sanity): tick.py imports lib.desk.execution", "tick.py" in _exec_importers)
# The needle is split so this file does not hold the SQL text itself. It finds
# state='approved' at any place in the SET list of an UPDATE (the text up to its WHERE), so
# "SET decided_by='desk', state='approved'" is caught too. A WHERE ... state='approved'
# condition is not an approval and does not match.
_APPROVE_SQL = _re.compile(r"\bUPDATE\s+\w+\s+SET\b(?:(?!\bWHERE\b)[^;])*?\bstate\s*=\s*['\"]" + "approved" + r"['\"]",
                           _re.IGNORECASE)
# The needle binds on each form it must refuse, and passes a WHERE condition.
for _w4_src, _w4_hit in (("UPDATE proposals SET state='" + "approved' WHERE id=1", True),
                         ("UPDATE proposals SET decided_by='desk', state='" + "approved' WHERE id=1", True),
                         ('"UPDATE proposals SET decided_at=?, " "state = \'' + 'approved\' WHERE id=?"', True),
                         ("UPDATE orders SET state='skipped' WHERE ref_id=? AND state='" + "approved'", False),
                         ("SELECT * FROM orders WHERE state='" + "approved'", False)):
    check(f"wall W4 needle: {_w4_src[:60]!r} -> {_w4_hit}", bool(_APPROVE_SQL.search(_w4_src)), _w4_hit)
_appr_sql = sorted(p for p in _TRACKED_PY if _APPROVE_SQL.search((_REPO / p).read_text(encoding="utf-8")))
# The rule covers the code. A test may hold the text as a search needle (as this file and
# tests/test_desk_approvals.py do), so tests/ is listed apart, not exempted in silence.
check("wall W4: the approve SQL text occurs only in lib/desk/approvals.py (code files)",
      [p for p in _appr_sql if not _is_test(p)], ["lib/desk/approvals.py"])
_appr_sql_tests = [p for p in _appr_sql if _is_test(p)]
if _appr_sql_tests:
    print(f"  NOTE wall W4: test files that hold the approve SQL text: {_appr_sql_tests}")

# --- W5: no tracked .py file imports a module of the §14 delete set ------------------
# §14.1-§14.4 DELETE rows, plus the RENAME rows whose old module name is now gone.
_DELETED_MODULES = (
    "analyze",
    "lib.allocate", "lib.calibrate", "lib.levers", "lib.learn", "lib.reflect_memory", "lib.risk",
    "lib.strategy_context", "lib.strategy_doc", "lib.rating", "lib.data_sentinels", "lib.ds_config",
    "lib.event_risk", "lib.screener", "lib.wall_replay", "lib.mailer", "lib.legislative",
    "lib.legislative_llm", "lib.legislative_backtest", "lib.legislative_judge_eval", "lib.intel",
    "lib.dataflows.y_finance", "lib.dataflows.congress", "lib.dataflows.fedreg", "lib.dataflows.stocktwits",
    "deploy.runner.run_tick", "deploy.runner.run_intel", "deploy.runner.chat_history",
    "scripts", "bench", "quiver_eve", "quill_data",
    # RENAME: the old names no longer exist.
    "lib.signals", "deploy.runner.order_guard", "deploy.runner.chat_guard",
)
for _dm in _DELETED_MODULES:
    _dp = _REPO / Path(*_dm.split("."))
    check_true(f"wall W5 (sanity): {_dm} is gone from disk",
               not _dp.with_suffix(".py").exists() and not (_dp / "__init__.py").exists())
check("wall W5 (sanity): no tracked .py file is left under scripts/, bench/ or quiver_eve/",
      [p for p in _TRACKED_PY if p.startswith(("scripts/", "bench/", "quiver_eve/"))
       or Path(p).name == "quill_data.py"], [])
for _p in _TRACKED_PY:
    _dead_imp = _hits(_IMPORTS_OF[_p], _DELETED_MODULES)
    check_true(f"wall W5: {_p} imports no deleted module" + (f" -> {sorted(_dead_imp)}" if _dead_imp else ""),
               not _dead_imp)

# --- reverse wall (§18.5 step 4): the approver and the executor never import the agent side ---
_AGENT_SIDE = {"lib.desk.tools", "lib.desk.agent"}
for _mod in ("lib/desk/approvals.py", "lib/desk/execution.py"):
    _rev = _hits(_imported_modules((_REPO / _mod).read_text(encoding="utf-8"), _mod), _AGENT_SIDE)
    check_true(f"reverse wall: {_mod} imports neither lib.desk.tools nor lib.desk.agent"
               + (f" -> {sorted(_rev)}" if _rev else ""), not _rev)


def _lib_closure(start):
    """Every lib module that `start` reaches through imports (direct and indirect)."""
    seen, todo = set(), [start]
    while todo:
        rel = todo.pop()
        if rel in seen or not (_REPO / rel).is_file():
            continue
        seen.add(rel)
        for m in _imported_modules((_REPO / rel).read_text(encoding="utf-8"), rel):
            if not m.startswith("lib."):
                continue
            for cand in (m.replace(".", "/") + ".py", m.replace(".", "/") + "/__init__.py"):
                if (_REPO / cand).is_file():
                    todo.append(cand)
    return seen


for _mod in ("lib/desk/approvals.py", "lib/desk/execution.py"):
    _reach = _lib_closure(_mod) & {"lib/desk/tools.py", "lib/desk/agent.py"}
    check_true(f"reverse wall: {_mod} does not reach tools.py or agent.py indirectly"
               + (f" -> {sorted(_reach)}" if _reach else ""), not _reach)

# --- the injectable clock seam of lib.market (moved from test_e2e_wall_replay.py, §18.5) ---
from datetime import datetime as _dtm  # noqa: E402
from zoneinfo import ZoneInfo as _ZI  # noqa: E402
import lib.market as _market  # noqa: E402

_ET = _ZI("America/New_York")
_ca, _cb = _market.now_et(), _dtm.now(_ET)
check_true("clock: unset now_et() is the real ET clock",
           _ca.tzinfo == _ET and abs((_cb - _ca).total_seconds()) < 1.0)
check("clock: unset override is None", _market._now_override, None)
check("clock: unset trading_day_et matches the wall clock", _market.trading_day_et(),
      _dtm.now(_ET).strftime("%Y-%m-%d"))
_pin = _dtm(2024, 3, 14, 10, 0, tzinfo=_ET)
with _market.frozen_clock(_pin):
    check("clock SET: now_et() == pin", _market.now_et(), _pin)
    check("clock SET: trading_day_et() == the pinned day", _market.trading_day_et(), "2024-03-14")
    check("clock SET: the session check uses the pinned day", _market.is_regular_session_open(), True)
check("clock: override None after the block", _market._now_override, None)
with _market.frozen_clock(_dtm(2024, 7, 4, 12, 0, tzinfo=_ET)):
    check("clock SET holiday: XNYS closed under the pin", _market.is_regular_session_open(), False)
try:
    with _market.frozen_clock(_dtm(2024, 3, 14, 10, 0)):   # naive -> ET
        check("clock: a naive pin is normalized to ET", _market.now_et().tzinfo, _ET)
        raise RuntimeError("boom")
except RuntimeError:
    pass
check("clock: restore on exception (override None)", _market._now_override, None)
_market.set_clock(_dtm(2024, 3, 14, 15, 0, tzinfo=_ZI("UTC")))
check("clock: set_clock converts an aware pin to ET", _market.now_et(), _dtm(2024, 3, 14, 11, 0, tzinfo=_ET))
_market.set_clock(None)
check("clock: set_clock(None) restores the real clock", _market._now_override, None)
with _market.frozen_clock(_pin):
    with _market.frozen_clock(_dtm(2024, 3, 15, 10, 0, tzinfo=_ET)):
        check("clock: a nested pin wins inside", _market.trading_day_et(), "2024-03-15")
    check("clock: the outer pin returns after the nested block", _market.now_et(), _pin)
check("clock: override None after the nested blocks", _market._now_override, None)
# ============================================================================
# Stage 0 — strategy layer (strategy.yaml + lib/strategy.py) + ledger tables.
# Pure + hermetic: the committed strategy.yaml, temp files, and a temp db only.
# ============================================================================
import dataclasses as _dc  # noqa: E402
import lib.strategy as _strat  # noqa: E402
from lib.config import RiskConfig as _RiskConfig  # noqa: E402

_REPO = Path(__file__).resolve().parent.parent
_sc = _strat.load_strategy(_REPO / "tests" / "fixtures" / "strategy_fixture.yaml")
_base_yaml = (_REPO / "tests" / "fixtures" / "strategy_fixture.yaml").read_text(encoding="utf-8")


def _write_tmp(text):
    p = Path(tempfile.mktemp(suffix=".yaml"))
    p.write_text(text, encoding="utf-8")
    return str(p)


# --- structure ---
check("strategy: default book", _sc.default_book, "core_55_45")
check("strategy: two books", sorted(_sc.books), ["core_55_45", "dial_up_63_37"])
for _bn, _b in _sc.books.items():
    check_true(f"strategy: {_bn} weights sum ~100",
               abs(sum(h.weight for h in _b.holdings) - 100.0) <= 0.5)
check_true("strategy: SOL non-quotable",
           any(h.ticker == "SOL" and not h.quotable for h in _sc.books["core_55_45"].holdings))
check_true("strategy: dial-up OFF by default", _sc.books["dial_up_63_37"].enabled is False)
check_true("strategy: cash sleeve carries no band",
           all(h.band == 0.0 for h in _sc.books["core_55_45"].holdings if h.is_cash))

# --- validate-or-raise (strict; the config wrapper turns these into None) ---
check_raises("strategy: band>=weight raises",
             lambda: _strat.load_strategy(_write_tmp(
                 _base_yaml.replace("weight: 9, band: 4", "weight: 9, band: 9"))),
             _strat.StrategyError)
check_raises("strategy: weights!=100 raises",
             lambda: _strat.load_strategy(_write_tmp(
                 _base_yaml.replace("ticker: SGOV, weight: 45", "ticker: SGOV, weight: 60", 1))),
             _strat.StrategyError)
check_raises("strategy: no default book raises",
             lambda: _strat.load_strategy(_write_tmp(
                 _base_yaml.replace("default: true", "default: false"))),
             _strat.StrategyError)
check_raises("strategy: deploy>=standdown raises",
             lambda: _strat.load_strategy(_write_tmp(
                 _base_yaml.replace("deploy_trigger_pce_pct: 2.5", "deploy_trigger_pce_pct: 4.0"))),
             _strat.StrategyError)
check_raises("strategy: ticker off allow-list raises",
             lambda: _strat.load_strategy(_write_tmp(
                 _base_yaml.replace("[SMH, SOXX,", "[SOXX,"))),
             _strat.StrategyError)
check_raises("strategy: absent file raises",
             lambda: _strat.load_strategy("/no/such/strategy.yaml"), _strat.StrategyError)



# --- config fail-safe + rebalance-knob defaults ---
def _safe_strategy(path):  # mirrors the load_config wrapper: garbled -> None
    try:
        return _strat.load_strategy(path)
    except Exception:
        return None


check("config: garbled strategy.yaml -> None (fail-safe contract)",
      _safe_strategy(_write_tmp("schema: 2\nnonsense: [")), None)
_rc = _RiskConfig(daily_loss_halt_pct=20, min_buying_power_buffer=5)
check("config: RiskConfig defaults (cash sleeve SGOV, drift band 5, min trade $5)",
      (_rc.cash_sleeve_ticker, _rc.rebalance_drift_band_pct, _rc.min_trade_notional), ("SGOV", 5.0, 5.0))
# §14.2: strategy.py keeps the seed parser; the regime and sleeve helpers are deleted.
for _gone in ("LearningConfig", "ConsistencyConfig", "regime_label", "book_for_regime", "select_active_book",
              "regime_label_banded", "normalize_regime", "regime_scalar", "regime_with_confirmation",
              "sleeve_thesis"):
    check_true(f"strategy: {_gone} is deleted (§14.2)", not hasattr(_strat, _gone))
check_true("strategy: the fixture's learning/consistency/sleeves keys are ignored, not loaded",
           not hasattr(_sc, "learning") and not hasattr(_sc, "consistency") and not hasattr(_sc, "sleeves"))


def _set_strategy_goal(led, **goal):
    """Write one active goal with no holdings through the kept atomic writer (the
    piecewise set_strategy_goal is deleted, §14.7)."""
    return led.set_strategy_goal_with_holdings(goal=goal, holdings=[])


# --- ledger: the strategy tables round-trip (temp db; fresh + existing) ---
_db = tempfile.mktemp(suffix=".db")
Ledger(_db)
_led = Ledger(_db)  # re-open: idempotent auto-create on a pre-existing db
_gid = _set_strategy_goal(_led, created_at="t", target_return_pct=15, horizon_months=12,
    benchmark="SGOV", benchmark_annual_pct=3.6, constraint_note="", macro_thesis_version="v1",
    macro_thesis_json="{}", active_book="core_55_45", as_of="d", start_date="d", start_equity=100.0)
_gid2 = _set_strategy_goal(_led, created_at="t2", target_return_pct=15, horizon_months=12,
    benchmark="SGOV", benchmark_annual_pct=3.6, constraint_note="", macro_thesis_version="v2",
    macro_thesis_json="{}", active_book="core_55_45", as_of="d", start_date="d", start_equity=100.0)
check("ledger: exactly one active goal (newest wins)", _led.get_active_goal()["id"], _gid2)
check_true("ledger: prior goal id differs", _gid != _gid2)
_led.upsert_target_holding(goal_id=_gid2, sleeve="s", ticker="SMH", target_weight=9, band=4,
    status="active", book="core_55_45", quotable=True, proxy_ticker=None, updated_at="t")
_led.upsert_target_holding(goal_id=_gid2, sleeve="s", ticker="SMH", target_weight=10, band=4,
    status="active", book="core_55_45", quotable=True, proxy_ticker=None, updated_at="t2")
_led.upsert_target_holding(goal_id=_gid2, sleeve="s", ticker="SOL", target_weight=3, band=2,
    status="active", book="core_55_45", quotable=False, proxy_ticker=None, updated_at="t")
check("ledger: upsert idempotent (no dup row)", len(_led.active_target_portfolio(_gid2)), 2)
check("ledger: upsert updates weight in place",
      next(r for r in _led.active_target_portfolio(_gid2) if r["ticker"] == "SMH")["target_weight"], 10.0)
check("ledger: non-quotable persisted (0)",
      next(r for r in _led.active_target_portfolio(_gid2) if r["ticker"] == "SOL")["quotable"], 0)
_led.set_holding_status(_gid2, "SMH", "exiting", "t3")
check("ledger: exiting drops from active", len(_led.active_target_portfolio(_gid2)), 1)
check("ledger: status filter includes exiting",
      len(_led.active_target_portfolio(_gid2, statuses=("active", "exiting"))), 2)
_led.record_goal_snapshot(goal_id=_gid2, trade_date="2026-06-13", captured_at="t", portfolio_value=100,
    glidepath_target_value=100, cumulative_return_pct=0.0, ahead_behind_pct=0.0,
    alpha_vs_benchmark_pct=0.0, active_book="core_55_45", regime="HOLD")
with _led.connect() as _gc:
    check("ledger: goal_tracking row appended",
          _gc.execute("SELECT COUNT(*) FROM goal_tracking WHERE goal_id=?", (_gid2,)).fetchone()[0], 1)
for _gone in ("set_strategy_goal", "goal_tracking_series", "upsert_thesis_state", "get_thesis_state"):
    check_true(f"ledger: {_gone} is deleted (§14.7)", not hasattr(Ledger, _gone))
Path(_db).unlink(missing_ok=True)

# ============================================================================
# Stage 1 — pure portfolio/goal math + the sizing target-aware clamp helpers.
# ============================================================================
import lib.portfolio as _pf  # noqa: E402
import lib.goal as _goal  # noqa: E402

# --- portfolio: target$/drift/intent ---
check("pf: target$ for weight", _pf.target_dollars_for_weight(9, 100.0), 9.0)
check("pf: target$ equity<=0 -> 0", _pf.target_dollars_for_weight(9, 0), 0.0)
check_true("pf: drift underweight negative", _pf.weight_drift(5.0, 100.0, 9.0) < 0)
check_true("pf: drift overweight positive", _pf.weight_drift(15.0, 100.0, 9.0) > 0)
check("pf: needs_rebalance inside band -> False", _pf.needs_rebalance(3.0, 4.0), False)
check("pf: needs_rebalance outside band -> True", _pf.needs_rebalance(5.0, 4.0), True)
check("pf: needs_rebalance at boundary -> False", _pf.needs_rebalance(4.0, 4.0), False)
check("pf: intent underweight -> buy", _pf.rebalance_intent(2.0, 100.0, 9.0, 4.0, "active")[0], "buy")
check("pf: intent overweight -> trim", _pf.rebalance_intent(20.0, 100.0, 9.0, 4.0, "active")[0], "trim")
check("pf: intent within band -> hold", _pf.rebalance_intent(10.0, 100.0, 9.0, 4.0, "active")[0], "hold")
check("pf: intent exiting -> exit", _pf.rebalance_intent(10.0, 100.0, 9.0, 4.0, "exiting")[0], "exit")
check("pf: intent unheld underweight -> buy", _pf.rebalance_intent(0.0, 100.0, 9.0, 4.0, "active")[0], "buy")
_targets = [
    {"ticker": "SMH", "sleeve": "Semis", "target_weight": 9, "band": 4, "status": "active", "quotable": True},
    {"ticker": "SOL", "sleeve": "Crypto", "target_weight": 3, "band": 2, "status": "active", "quotable": False},
    {"ticker": "SGOV", "sleeve": "Cash", "target_weight": 45, "band": 0, "status": "active", "quotable": True},
]
_book = {r["ticker"]: r for r in _pf.construct_target_book(_targets, {"SMH": 2.0}, 100.0, cash_sleeve_ticker="SGOV")}
check("pf: SGOV held as cash residual", _book["SGOV"]["intent"], "cash_residual")
check("pf: SOL skipped (unquotable)", _book["SOL"]["intent"], "skip_unquotable")
check("pf: SMH underweight -> buy", _book["SMH"]["intent"], "buy")
check_true("pf: SMH buy delta positive (toward target)", _book["SMH"]["delta_dollars"] > 0)

# --- item 1: band = a real no-trade dead-band with a config fallback + edge dollars ---
check("pf: resolve_band per-name binds", _pf.resolve_band_pct(3.0, 7.0, 5.0), 3.0)
check("pf: resolve_band fallback to config when 0", _pf.resolve_band_pct(0.0, 20.0, 5.0), 5.0)
check("pf: resolve_band capped at weight*0.5", _pf.resolve_band_pct(8.0, 6.0, 5.0), 3.0)
check("pf: resolve_band config fallback also capped", _pf.resolve_band_pct(0.0, 4.0, 5.0), 2.0)
check("pf: resolve_band weight 0 -> band 0 (no strand; full unwind to target 0)",
      _pf.resolve_band_pct(3.0, 0.0, 5.0), 0.0)
# regression (review): a target-0 but still-ACTIVE held name must not keep a band-sized
# residual. band resolves to 0 at target 0, so the trim winds it fully to 0 (not stranded at 5% of eq).
_bz = [{"ticker": "ZZZ", "sleeve": "s", "target_weight": 0, "band": 0, "status": "active", "quotable": True}]
_bkz = {r["ticker"]: r for r in _pf.construct_target_book(_bz, {"ZZZ": 16.0}, 200.0, default_band_pct=5.0)}
check("pf: target-0 active held name has band_dollars 0 (no strand)", _bkz["ZZZ"]["band_dollars"], 0.0)
check("pf: target-0 active held name -> trim toward exact 0", _bkz["ZZZ"]["intent"], "trim")
# and the sell sizing winds it fully out (excess over 0+0 = full held mv)
check("sig: target-0 trim (band 0) sells the whole overshoot to 0",
      sizing.resolve_target_sell_quantity(1.6, 10.0, 16.0, 0.0, upper_band_dollars=0.0), 1.6)
# A bandless (band=0) name must pick up the config fallback and emit band_dollars, so it
# no longer churns on any drift and the trim pass can size to the edge.
_bt = [{"ticker": "AAA", "sleeve": "S", "target_weight": 10, "band": 0, "status": "active", "quotable": True}]
_bk = {r["ticker"]: r for r in _pf.construct_target_book(_bt, {"AAA": 50.0}, 100.0, default_band_pct=5.0)}
check("pf: bandless name gets config fallback band (10w,band0,cfg5 -> 5)", _bk["AAA"]["band"], 5.0)
check("pf: band_dollars = band% * equity", _bk["AAA"]["band_dollars"], 5.0)
check("pf: SMH band_dollars present (band4 * 100)", _book["SMH"]["band_dollars"], 4.0)

# --- goal: glidepath + progress + coarse regime ---
check("goal: glidepath elapsed0 == start", _goal.glidepath_target_value(100.0, 15, 12, 0.0), 100.0)
check("goal: glidepath horizon == start*1.15", round(_goal.glidepath_target_value(100.0, 15, 12, 365.25), 2), 115.0)
check("goal: glidepath mid == start*1.075", round(_goal.glidepath_target_value(100.0, 15, 12, 182.625), 4), 107.5)
check("goal: glidepath start<=0 -> None", _goal.glidepath_target_value(0.0, 15, 12, 10.0), None)
check("goal: glidepath bad elapsed -> None", _goal.glidepath_target_value(100.0, 15, 12, None), None)
_gp = _goal.goal_progress(100.0, 110.0, 15, 12, "2026-01-01", "2026-07-02", benchmark_annual_pct=3.6)
check("goal: ahead of glidepath -> AHEAD", _gp["regime"], "AHEAD")
check_true("goal: cumulative ~ +10%", abs(_gp["cumulative_return_pct"] - 10.0) < 0.001)
check_true("goal: alpha positive vs ~3.6% cash", _gp["alpha_vs_benchmark_pct"] > 0)
check("goal: below glidepath -> BEHIND",
      _goal.goal_progress(100.0, 95.0, 15, 12, "2026-01-01", "2026-12-15")["regime"], "BEHIND")
check("goal: start<=0 -> None", _goal.goal_progress(0.0, 95.0, 15, 12, "2026-01-01", "2026-07-02"), None)
check("goal: coarse_regime dead-band -> ON-TRACK", _goal.coarse_regime(0.5), "ON-TRACK")
check("goal: coarse_regime None -> ON-TRACK", _goal.coarse_regime(None), "ON-TRACK")

# --- goal: deposit-adjusted (time-weighted) return — external flows are NOT gains ---
# No flows -> byte-identical to the legacy simple return (short-circuit, no float drift).
check_true("goal: TWR no-flows == legacy simple (+30%)",
           abs(_goal.time_weighted_return_pct(100.0, "2026-01-01",
               [("2026-02-01", 110.0), ("2026-03-01", 130.0)], []) - 30.0) < 1e-9)
# A pure deposit ($900 into a $100 acct, no market move) is 0% return, NOT +900%.
check_true("goal: TWR strips a pure deposit (0%, not 900%)",
           abs(_goal.time_weighted_return_pct(100.0, "2026-01-01",
               [("2026-01-02", 1000.0)], [("2026-01-02", 900.0)])) < 1e-9)
# Chained real gains around an early deposit: 100->150(+50%), +850 dep->1000, ->1100(+10%) = 65%
# (the buggy simple return would read +1000%).
check_true("goal: TWR chains real gains around a deposit (65%, not 1000%)",
           abs(_goal.time_weighted_return_pct(100.0, "2026-01-01",
               [("2026-01-02", 150.0), ("2026-01-03", 1000.0), ("2026-01-04", 1100.0)],
               [("2026-01-03", 850.0)]) - 65.0) < 1e-9)
# Withdrawal (negative flow): 100->200 (+100%) then withdraw 50 -> 150 end = still +100%.
check_true("goal: TWR strips a withdrawal (still +100%)",
           abs(_goal.time_weighted_return_pct(100.0, "2026-01-01",
               [("2026-01-02", 200.0), ("2026-01-03", 150.0)], [("2026-01-03", -50.0)]) - 100.0) < 1e-9)
# Guards: start<=0 / non-positive interval-start equity / empty points -> None.
check("goal: TWR start<=0 -> None",
      _goal.time_weighted_return_pct(0.0, "2026-01-01", [("2026-02-01", 110.0)], [("2026-01-15", 5.0)]), None)
check("goal: TWR prev_eq<=0 -> None",
      _goal.time_weighted_return_pct(100.0, "2026-01-01",
          [("2026-02-01", 0.0), ("2026-03-01", 50.0)], [("2026-02-15", 10.0)]), None)
check("goal: TWR empty points -> None",
      _goal.time_weighted_return_pct(100.0, "2026-01-01", [], [("2026-02-01", 5.0)]), None)

# goal_progress: an explicit deposit-adjusted return de-inflates ahead_behind + alpha; with no
# override it stays byte-identical to the legacy simple return.
_gp_adj = _goal.goal_progress(100.0, 1000.0, 15, 12, "2026-01-01", "2026-07-02",
                              benchmark_annual_pct=3.6, cumulative_return_pct=0.0)
check("goal: override cumulative used verbatim", _gp_adj["cumulative_return_pct"], 0.0)
check_true("goal: override de-inflates ahead_behind (<0 vs glidepath)", _gp_adj["ahead_behind_pct"] < 0)
check_true("goal: override de-inflates alpha (<0 vs +cash bench)", _gp_adj["alpha_vs_benchmark_pct"] < 0)
check("goal: override -> BEHIND regime", _gp_adj["regime"], "BEHIND")
check("goal: no override == legacy simple (+900%)",
      _goal.goal_progress(100.0, 1000.0, 15, 12, "2026-01-01", "2026-07-02",
                          benchmark_annual_pct=3.6)["cumulative_return_pct"], 900.0)

# ledger round-trip: record_cash_flow + cash_flows (date-ASC, signed).
_fdb = tempfile.mktemp(suffix=".db")
_fled = Ledger(_fdb)
_fled.record_cash_flow(trade_date="2026-01-03", amount=900.0, note="seed deposit", created_at="t")
_fled.record_cash_flow(trade_date="2026-02-01", amount=-50.0, note=None, created_at="t2")
check("ledger: cash_flows round-trip (date-ASC, signed)",
      [(f["trade_date"], f["amount"]) for f in _fled.cash_flows()],
      [("2026-01-03", 900.0), ("2026-02-01", -50.0)])
Path(_fdb).unlink(missing_ok=True)

# compute_from_ledger integration (the RCA bug): a $900 deposit that grows a $100 seed to ~$1000
# must NOT read as ~+900% return. With no flow recorded -> legacy inflated value; once the deposit
# is recorded -> de-inflated to ~0%. This is the red/green anchor (old math fails the second check).
_cdb = tempfile.mktemp(suffix=".db")
_cled = Ledger(_cdb)
_set_strategy_goal(_cled, created_at="t", target_return_pct=15, horizon_months=12,
    benchmark="SGOV", benchmark_annual_pct=3.6, constraint_note="", macro_thesis_version="v",
    macro_thesis_json="{}", active_book="core_55_45", as_of="d",
    start_date="2026-01-01", start_equity=100.0)
_cled.get_or_create_baseline("2026-01-01", 100.0, "2026-01-01T00:00:00Z")
_cled.get_or_create_baseline("2026-01-03", 1000.0, "2026-01-03T00:00:00Z")  # jumped via a deposit
_crow = _cled.get_active_goal()
check("goal: compute_from_ledger inflated w/o flow (legacy, RCA bug)",
      _goal.compute_from_ledger(_cled, _crow)["cumulative_return_pct"], 900.0)
_cled.record_cash_flow(trade_date="2026-01-03", amount=900.0, note="deposit", created_at="t")
check_true("goal: compute_from_ledger deposit-adjusted ~0% (the fix)",
           abs(_goal.compute_from_ledger(_cled, _crow)["cumulative_return_pct"]) < 1e-9)
Path(_cdb).unlink(missing_ok=True)

# --- deposit auto-capture: infer suspected external flows from baseline jumps ---
# (The Robinhood MCP exposes NO cumulative net-deposit figure — verified get_portfolio only
#  returns a transient pending_deposits — so flows are inferred from the equity curve, then
#  surfaced/opt-in-written. All pure/offline; nothing here reads limits/broker/halt.)
# _goal_window (Gate-B F4): points >= start_date INCLUDE the anchor baseline; flows > start_date EXCLUDE a start-date flow.
_gw_pts, _gw_fl = _goal._goal_window(
    [{"trade_date": "2026-01-01", "baseline_equity": 100.0},
     {"trade_date": "2026-01-03", "baseline_equity": 1000.0}],
    [{"trade_date": "2026-01-01", "amount": 5.0}, {"trade_date": "2026-01-03", "amount": 900.0}],
    "2026-01-01")
check("flow: _goal_window points include the start-date baseline (>=)",
      [d for d, _e in _gw_pts], ["2026-01-01", "2026-01-03"])
check("flow: _goal_window flows exclude a start-date flow (>)",
      [d for d, _a in _gw_fl], ["2026-01-03"])
# detect_external_flows: the real RCA jump (98->246 in a day) is flagged as a deposit.
_det = _goal.detect_external_flows([("2026-06-02", 98.03), ("2026-06-03", 245.98)], [], min_pct=25, min_abs=30)
check("flow: deposit jump flagged (count/amount)", (len(_det), _det[0]["amount"]), (1, 147.95))
check("flow: deposit direction+date", (_det[0]["direction"], _det[0]["trade_date"]), ("deposit", "2026-06-03"))
check("flow: within-band (+10%) ignored",
      _goal.detect_external_flows([("d0", 100.0), ("d1", 110.0)], [], min_pct=25, min_abs=30), [])
check("flow: min_pct leg (+1% on $4k) ignored",  # both AND legs must be pinned
      _goal.detect_external_flows([("d0", 4000.0), ("d1", 4040.0)], [], min_pct=25, min_abs=30), [])
check("flow: min_abs leg (+100% but $1) ignored",
      _goal.detect_external_flows([("d0", 1.0), ("d1", 2.0)], [], min_pct=25, min_abs=30), [])
check("flow: a recorded flow in the interval suppresses its candidate",
      _goal.detect_external_flows([("2026-06-02", 98.03), ("2026-06-03", 245.98)],
                                  [("2026-06-03", 147.95)], min_pct=25, min_abs=30), [])
_wd = _goal.detect_external_flows([("d0", 200.0), ("d1", 100.0)], [], min_pct=25, min_abs=30)
check("flow: withdrawal flagged (sign+dir)", (_wd[0]["amount"] < 0, _wd[0]["direction"]), (True, "withdrawal"))
check("flow: prev_eq<=0 skipped (no raise)",
      _goal.detect_external_flows([("d0", 0.0), ("d1", 100.0)], [], min_pct=25, min_abs=30), [])
check("flow: None equity skipped (no raise)",
      _goal.detect_external_flows([("d0", None), ("d1", 100.0)], [], min_pct=25, min_abs=30), [])
check("flow: single point -> []", _goal.detect_external_flows([("d0", 100.0)], [], min_pct=25, min_abs=30), [])
check("flow: multi-jump date-ASC order",
      [c["trade_date"] for c in _goal.detect_external_flows(
          [("d0", 100.0), ("d1", 1000.0), ("d2", 100.0)], [], min_pct=25, min_abs=30)], ["d1", "d2"])
# flow_adjusted_returns: the deposit-adjusted daily return series. A deposit is not a gain.
# The points are the production curve around the 2026-07-09 deposit ($800, checked against the
# broker's cost basis + realized P&L and against hourly bars across the jump).
_fa_pts = [("2026-07-07", 235.73), ("2026-07-08", 238.84), ("2026-07-09", 1045.09), ("2026-07-10", 1046.36)]
_fa_r, _fa_skip = _goal.flow_adjusted_returns(_fa_pts, [("2026-07-09", 800.0)], min_pct=25, min_abs=30)
check("flow-adj: a recorded deposit leaves the market return of its day",
      [round(r, 6) for r in _fa_r],
      [round(238.84 / 235.73 - 1, 6), round((1045.09 - 800.0) / 238.84 - 1, 6), round(1046.36 / 1045.09 - 1, 6)])
check("flow-adj: nothing is skipped when the flow is recorded", _fa_skip, [])
_fa_r0, _fa_skip0 = _goal.flow_adjusted_returns(_fa_pts, [], min_pct=25, min_abs=30)
check("flow-adj: an UNRECORDED jump is not a return (2 returns, not 3)", len(_fa_r0), 2)
check_true("flow-adj: no +337% day reaches the return series", max(_fa_r0) < 0.02)
check("flow-adj: the unrecorded jump is reported with its date",
      [j["trade_date"] for j in _fa_skip0], ["2026-07-09"])
_fa_wr, _fa_ws = _goal.flow_adjusted_returns(
    [("d0", 1000.0), ("d1", 700.0)], [("d1", -300.0)], min_pct=25, min_abs=30)
check("flow-adj: a recorded withdrawal is not a loss", ([round(r, 9) for r in _fa_wr], _fa_ws), ([0.0], []))
check("flow-adj: a non-positive / missing prior equity is dropped, never divided",
      _goal.flow_adjusted_returns([("d0", 0.0), ("d1", 100.0), ("d2", None), ("d3", 101.0)], [],
                                  min_pct=25, min_abs=30), ([], []))
check("flow-adj: fewer than two points -> no returns",
      _goal.flow_adjusted_returns([("d0", 100.0)], [], min_pct=25, min_abs=30), ([], []))

# compute_from_ledger with NO start_equity on the goal row (a `strategy-set` with no equity
# input). Production had this from 2026-08-07: goal-track returned "insufficient equity
# history" and wrote no row for two months. The anchor is the first baseline of the window.
_ndb = tempfile.mktemp(suffix=".db")
_nled = Ledger(_ndb)
_set_strategy_goal(_nled, created_at="t", target_return_pct=15, horizon_months=12,
    benchmark="SGOV", benchmark_annual_pct=3.6, constraint_note="", macro_thesis_version="v",
    macro_thesis_json="{}", active_book="core_55_45", as_of="d",
    start_date="2026-08-07", start_equity=None)
_nled.get_or_create_baseline("2026-08-06", 2014.57, "t")     # before the window: not the anchor
check("goal: no start_equity + no baseline in the window -> None (nothing to report yet)",
      _goal.compute_from_ledger(_nled, _nled.get_active_goal()), None)
_nled.get_or_create_baseline("2026-08-07", 2033.22, "t")
_nled.get_or_create_baseline("2026-10-13", 2070.89, "t")
_nprog = _goal.compute_from_ledger(_nled, _nled.get_active_goal())
check_true("goal: no start_equity -> anchored at the first baseline of the window",
           _nprog is not None and abs(_nprog["cumulative_return_pct"] - (2070.89 / 2033.22 - 1) * 100) < 1e-3)
Path(_ndb).unlink(missing_ok=True)

# confirmable_flows (Gate-B F-WALL-1): only SETTLED + PERSISTED jumps are auto-writable.
_cand = _goal.detect_external_flows([("d0", 100.0), ("d1", 1000.0)], [], min_pct=25, min_abs=30)
check("flow: unsettled jump (no later baselines) NOT confirmable",
      _goal.confirmable_flows([("d0", 100.0), ("d1", 1000.0)], _cand, confirm_days=2), [])
check("flow: settled + level persisted -> confirmable",
      len(_goal.confirmable_flows([("d0", 100.0), ("d1", 1000.0), ("d2", 1010.0), ("d3", 990.0)],
                                  _cand, confirm_days=2)), 1)
check("flow: settled but REVERTED (transient spike) NOT confirmable",
      _goal.confirmable_flows([("d0", 100.0), ("d1", 1000.0), ("d2", 105.0), ("d3", 102.0)],
                              _cand, confirm_days=2), [])
_cand_wd = _goal.detect_external_flows([("d0", 1000.0), ("d1", 100.0)], [], min_pct=25, min_abs=30)
check("flow: withdrawal persisted (max stays low) -> confirmable",
      len(_goal.confirmable_flows([("d0", 1000.0), ("d1", 100.0), ("d2", 110.0), ("d3", 90.0)],
                                  _cand_wd, confirm_days=2)), 1)
# suggest_flows_from_ledger (real Ledger) + red/green idempotency + Gate-B F4 anchor boundary.
_sdb = tempfile.mktemp(suffix=".db"); _sled = Ledger(_sdb)
_set_strategy_goal(_sled, created_at="t", target_return_pct=15, horizon_months=12,
    benchmark="SGOV", benchmark_annual_pct=3.6, constraint_note="", macro_thesis_version="v",
    macro_thesis_json="{}", active_book="core_55_45", as_of="d", start_date="2026-01-01", start_equity=100.0)
_sled.get_or_create_baseline("2026-01-01", 100.0, "2026-01-01T00:00:00Z")   # first baseline == goal start_date
_sled.get_or_create_baseline("2026-01-03", 1000.0, "2026-01-03T00:00:00Z")  # the deposit jump
_sgoal = _sled.get_active_goal()
_sug = _goal.suggest_flows_from_ledger(_sled, _sgoal, min_pct=25, min_abs=30)
check("flow: suggest_from_ledger finds the jump anchored at start_date (>= boundary)",
      (len(_sug), _sug[0]["trade_date"], _sug[0]["amount"]), (1, "2026-01-03", 900.0))
_sled.record_cash_flow(trade_date="2026-01-03", amount=900.0, note="deposit", created_at="t")
check("flow: suggest suppressed once the flow is recorded (idempotent)",
      _goal.suggest_flows_from_ledger(_sled, _sgoal, min_pct=25, min_abs=30), [])
check("flow: suggest no goal -> []", _goal.suggest_flows_from_ledger(_sled, None, min_pct=25, min_abs=30), [])
Path(_sdb).unlink(missing_ok=True)
# record_cash_flow_if_absent (Gate-B F-IDEMPOTENCY): atomic guard, at most one auto row per date.
_adb = tempfile.mktemp(suffix=".db"); _aled = Ledger(_adb)
check_true("flow: if_absent first insert wins",
           _aled.record_cash_flow_if_absent(trade_date="2026-02-01", amount=100.0, note="auto:x", created_at="t"))
check("flow: if_absent second same-date insert is a no-op",
      _aled.record_cash_flow_if_absent(trade_date="2026-02-01", amount=100.0, note="auto:y", created_at="t2"), False)
check("flow: if_absent kept exactly one auto row",
      len([f for f in _aled.cash_flows() if str(f.get("note") or "").startswith("auto:")]), 1)
_aled.record_cash_flow(trade_date="2026-03-01", amount=50.0, note="manual", created_at="t")
check("flow: manual record unaffected by the auto guard", len(_aled.cash_flows()), 2)
Path(_adb).unlink(missing_ok=True)

# --- sizing: target-aware helpers (room under the target, trim to the band edge) ---
check("sig: room_under_target", sizing.room_under_target(9.0, 2.0), 7.0)
check("sig: room_under_target at/over -> 0", sizing.room_under_target(9.0, 12.0), 0.0)
check("sig: target sell trim (excess/quote)", sizing.resolve_target_sell_quantity(10.0, 5.0, 60.0, 45.0), 3.0)
check("sig: target sell full_exit -> all held", sizing.resolve_target_sell_quantity(10.0, 5.0, 60.0, 45.0, full_exit=True), 10.0)
check("sig: target sell at/under target -> 0", sizing.resolve_target_sell_quantity(10.0, 5.0, 40.0, 45.0), 0.0)
check("sig: target sell never oversells", sizing.resolve_target_sell_quantity(10.0, 5.0, 1000.0, 0.0), 10.0)
check("sig: target sell no quote -> 0", sizing.resolve_target_sell_quantity(10.0, 0.0, 60.0, 45.0), 0.0)
# item 1 (trim->edge): with a band, trim only the excess over target+band (60-45-5=10 -> 2sh),
# NOT the full excess over target (which would be 3sh). A name settles at the upper edge.
check("sig: trim to band EDGE not exact target",
      sizing.resolve_target_sell_quantity(10.0, 5.0, 60.0, 45.0, upper_band_dollars=5.0), 2.0)
check("sig: within band (target+band) -> no trim",
      sizing.resolve_target_sell_quantity(10.0, 5.0, 48.0, 45.0, upper_band_dollars=5.0), 0.0)
check("sig: full_exit ignores band (all held)",
      sizing.resolve_target_sell_quantity(10.0, 5.0, 60.0, 45.0, full_exit=True, upper_band_dollars=5.0), 10.0)
check("sig: band=0 == classic trim-to-target (byte-identical)",
      sizing.resolve_target_sell_quantity(10.0, 5.0, 60.0, 45.0, upper_band_dollars=0.0), 3.0)
# buy->target is unchanged: room_under_target with no band fills to the EXACT target
check("sig: buy fills to exact target (buy->target, no band)", sizing.room_under_target(45.0, 30.0), 15.0)

# downside_deviation (lib/trend): the downside volatility of a return series
import lib.trend as _trend  # noqa: E402
check("ptj: downside_deviation None on no downside", _trend.downside_deviation([0.01, 0.02, 0.03]), None)
check("ptj: downside_deviation None on empty", _trend.downside_deviation([]), None)
check_true("ptj: downside_deviation positive when downside present",
           (_trend.downside_deviation([-0.02, 0.01, -0.03]) or 0) > 0)

# F7 plan_adherence (pure) + ledger adherence round-trip
from lib import memory as _mem  # noqa: E402
check("ptj: adherence target_hit", _mem.plan_adherence({"stop_loss": 90, "target_price": 130}, 135), "target_hit")
check("ptj: adherence stop_violated", _mem.plan_adherence({"stop_loss": 90, "target_price": 130}, 88), "stop_violated")
check("ptj: adherence within", _mem.plan_adherence({"stop_loss": 90, "target_price": 130}, 110), "within")
check("ptj: adherence na (no plan)", _mem.plan_adherence({}, 110), "na")
check("ptj: adherence na (no price)", _mem.plan_adherence({"stop_loss": 90}, None), "na")
_adb = tempfile.mktemp(suffix=".db")
_adl = Ledger(_adb)
_adl.ensure_schema()
_adid = _adl.record_decision(trade_date="2026-06-20", ticker="ZZZ", signal="Buy", intent="buy",
                             decision_price=100.0, run_id="r", decided_at="t")
_adl.record_outcome(_adid, resolved_at="t2", directional_return=0.05, adherence="target_hit")
_adrow = [d for d in _adl.decisions_with_outcomes("ZZZ") if d["id"] == _adid][0]
check("ptj: adherence persists + reads back via decisions_with_outcomes", _adrow.get("adherence"), "target_hit")


# ============================================================================
# strategy-set + goal-track + flow-suggest (tick.py glue; observability only).
# ============================================================================
import tick as _tick  # noqa: E402

# Atomic strategy-set: goal + ALL holdings land in one transaction (the piecewise
# path could leave a partial book mid-crash -> the mass-liquidation above).
_led_atom = _tmp_ledger()
_gid_atom = _led_atom.set_strategy_goal_with_holdings(
    goal={"created_at": "t", "target_return_pct": 15, "horizon_months": 12, "benchmark": "SGOV",
          "benchmark_annual_pct": 3.6, "constraint_note": "", "macro_thesis_version": "v",
          "macro_thesis_json": "{}", "active_book": "core_ai", "as_of": "d", "start_date": "d",
          "start_equity": 100.0},
    holdings=[{"sleeve": "Compute", "ticker": "SMH", "target_weight": 60, "band": 5,
               "status": "active", "book": "core_ai", "quotable": True, "proxy_ticker": None,
               "updated_at": "t"},
              {"sleeve": "Cash", "ticker": "SGOV", "target_weight": 40, "band": 0,
               "status": "active", "book": "core_ai", "quotable": True, "proxy_ticker": None,
               "updated_at": "t"}])
check("ledger: atomic strategy-set writes the goal", _led_atom.get_active_goal()["id"], _gid_atom)
check("ledger: atomic strategy-set writes ALL holdings in one txn",
      len(_led_atom.active_target_portfolio(_gid_atom)), 2)

# ---- deposit auto-capture wired through _run_goal_track + config + flow-suggest CLI ----
def _cfg_goal_dict(goal_block=None):
    d = _base_config()
    if goal_block is not None:
        d["goal"] = goal_block
    return d


def _cfg_goal(auto, **over):
    return load_config(_write_config(_cfg_goal_dict({"auto_capture_flows": auto, **over})))


def _seed_jump_ledger(start_equity, jump_to, *later):
    """A ledger: active goal (start @ 2026-01-01) + a start->jump_to jump on 2026-01-03
    + optional later baselines (to settle/persist or revert the jump)."""
    led = _tmp_ledger()
    _set_strategy_goal(led, created_at="t", target_return_pct=15, horizon_months=12,
        benchmark="SGOV", benchmark_annual_pct=3.6, constraint_note="", macro_thesis_version="v",
        macro_thesis_json="{}", active_book="core_55_45", as_of="d",
        start_date="2026-01-01", start_equity=start_equity)
    led.get_or_create_baseline("2026-01-01", start_equity, "2026-01-01T00:00:00Z")
    led.get_or_create_baseline("2026-01-03", jump_to, "2026-01-03T00:00:00Z")
    for dte, eq in later:
        led.get_or_create_baseline(dte, eq, dte + "T00:00:00Z")
    return led


# goal-track records one goal_tracking snapshot from the equity curve + the active goal.
_led_gt = _seed_jump_ledger(100.0, 110.0)
_gt = _tick._run_goal_track(_cfg_goal(False), _led_gt)
check("goal-track: records a snapshot", _gt["recorded"], True)
check_true("goal-track: regime present", _gt.get("regime") in ("AHEAD", "ON-TRACK", "BEHIND"))
check("goal-track: no active goal -> nothing recorded",
      _tick._run_goal_track(_cfg_goal(False), _tmp_ledger())["recorded"], False)

# Gate-B F1: the OFF test seeds the IDENTICAL settled jump the ON test uses, so an empty
# cash_flows is attributable to the flag being OFF — not to an absent/undetectable jump.
_led_off = _seed_jump_ledger(100.0, 1000.0, ("2026-01-05", 1010.0), ("2026-01-07", 990.0))
_gt_off = _tick._run_goal_track(_cfg_goal(False), _led_off)
check("flow: goal-track surfaces suspected flows even when auto OFF", _gt_off["n_suspected_flows"], 1)
check("flow: auto-capture OFF writes nothing (same detectable jump)", _led_off.cash_flows(), [])
# flag ON on the SAME settled jump -> exactly one row; a SECOND run adds nothing (idempotent).
_led_on = _seed_jump_ledger(100.0, 1000.0, ("2026-01-05", 1010.0), ("2026-01-07", 990.0))
_gt_on = _tick._run_goal_track(_cfg_goal(True), _led_on)
check("flow: auto-capture ON writes the settled deposit", len(_led_on.cash_flows()), 1)
check("flow: auto_captured reported in goal-track output", len(_gt_on.get("auto_captured", [])), 1)
_tick._run_goal_track(_cfg_goal(True), _led_on)
check("flow: auto-capture idempotent across runs", len(_led_on.cash_flows()), 1)
# Gate-B F-WALL-1: an UNSETTLED jump (no later baselines) is NOT auto-written even with the flag ON.
_led_uns = _seed_jump_ledger(100.0, 1000.0)
_tick._run_goal_track(_cfg_goal(True), _led_uns)
check("flow: unsettled jump not auto-written (confirmation gate)", _led_uns.cash_flows(), [])
# Gate-B F5: prove cfg.goal_flow_min_pct is actually threaded into the detector on the WRITE
# path — DIFFERENTIAL: a +26% ($260) settled jump writes under min_pct=25 but not min_pct=50.
_led_lo = _seed_jump_ledger(1000.0, 1260.0, ("2026-01-05", 1270.0), ("2026-01-07", 1255.0))
_tick._run_goal_track(_cfg_goal(True), _led_lo)
check("flow: +26% jump auto-written at default min_pct=25", len(_led_lo.cash_flows()), 1)
_led_hi = _seed_jump_ledger(1000.0, 1260.0, ("2026-01-05", 1270.0), ("2026-01-07", 1255.0))
_tick._run_goal_track(_cfg_goal(True, flow_min_pct=50), _led_hi)
check("flow: +26% jump NOT written at min_pct=50 (cfg threaded, not a literal)", _led_hi.cash_flows(), [])

# flow-suggest CLI is READ-ONLY (records nothing) and emits ready-to-run flow-record commands.
_led_cli = _seed_jump_ledger(100.0, 1000.0, ("2026-01-05", 1010.0))
_orig_cfl = _tick._cfg_and_ledger
_tick._cfg_and_ledger = lambda: (_cfg_goal(False), _led_cli)
try:
    _sug_out = _tick.cmd_flow_suggest(None)  # cmd_flow_suggest ignores args (uses _cfg_and_ledger)
finally:
    _tick._cfg_and_ledger = _orig_cfl
check("flow-suggest: count matches suspected", _sug_out["count"], 1)
check_true("flow-suggest: emits a ready flow-record command",
           _sug_out["commands"][0].startswith("tick.py flow-record --input"))
check("flow-suggest: records NOTHING (read-only)", _led_cli.cash_flows(), [])

# config: goal block parse — defaults (Gate-A) + F1 fail-safe + F2 strict gate.
_c_absent = load_config(_write_config(_cfg_goal_dict(None)))
check("config: goal block absent -> auto OFF + defaults (25/30/2)",
      (_c_absent.goal_auto_capture_flows, _c_absent.goal_flow_min_pct,
       _c_absent.goal_flow_min_abs, _c_absent.goal_flow_confirm_days), (False, 25.0, 30.0, 2))
_c_expl = _cfg_goal(True, flow_min_pct=20)
check("config: explicit goal values parsed",
      (_c_expl.goal_auto_capture_flows, _c_expl.goal_flow_min_pct), (True, 20.0))
check("config: garbled goal threshold does NOT brick load (Gate-A F1 fail-safe -> default)",
      _cfg_goal(True, flow_min_pct="aggressive").goal_flow_min_pct, 25.0)
check("config: quoted 'false' does NOT enable the write path (Gate-A F2 strict is-True)",
      load_config(_write_config(_cfg_goal_dict({"auto_capture_flows": "false"}))).goal_auto_capture_flows, False)


# ============================================================================
# tradable_universe, named run locks, the offline healthcheck, the prompt loader.
# ============================================================================
import lib.universe as _uni  # noqa: E402

# --- tradable_universe: the analysis universe a book implies (pure) ---
_tu_rows = [
    {"ticker": "smh", "sleeve": "Semis", "quotable": True, "status": "active"},
    {"ticker": "SOL", "sleeve": "Crypto", "quotable": False, "status": "active"},
    {"ticker": "SGOV", "sleeve": "Cash", "quotable": True, "status": "active"},
    {"ticker": "XLV", "sleeve": "Value", "quotable": True, "status": "exiting"},
    {"ticker": "SMH", "sleeve": "Semis", "quotable": True, "status": "active"},  # dup
]
check("uni: tradable_universe filters cash/unquotable/non-active/dups + uppercases, keeps order",
      _uni.tradable_universe(_tu_rows, "SGOV"), ["SMH"])
check("uni: tradable_universe drops cash by ticker even if sleeve differs",
      _uni.tradable_universe([{"ticker": "SGOV", "sleeve": "Misc", "quotable": True}], "SGOV"), [])
check("uni: tradable_universe quotable + status default permissive",
      _uni.tradable_universe([{"ticker": "URA", "sleeve": "Uranium"}], "SGOV"), ["URA"])
check("uni: tradable_universe empty rows -> []", _uni.tradable_universe([], "SGOV"), [])
check("uni: tradable_universe skips blank tickers", _uni.tradable_universe([{"ticker": ""}], "SGOV"), [])
# Order is load-bearing on the yaml-fallback path (declaration order -> analysis order);
# multiple survivors in non-alphabetical input proves it is NOT sorted/reversed.
check("uni: tradable_universe preserves declaration order (not sorted)",
      _uni.tradable_universe([{"ticker": "URA"}, {"ticker": "SMH"}, {"ticker": "ETHA"}], "SGOV"),
      ["URA", "SMH", "ETHA"])

# --- named run locks (§14.2 runlock.py): one row per runner name ---
import lib.runlock as _runlock  # noqa: E402

_ledL = _tmp_ledger()
check("ledger: named lock acquires when free",
      _ledL.try_acquire_named_lock("monitor", "b:1", "2026-06-13T10:00:00Z", ttl_seconds=3600), True)
check("ledger: named lock blocks a second holder while held",
      _ledL.try_acquire_named_lock("monitor", "b:2", "2026-06-13T10:01:00Z", ttl_seconds=3600), False)
check("ledger: another name is independent (the monitor never blocks the broker)",
      _ledL.try_acquire_named_lock("broker", "b:2", "2026-06-13T10:01:00Z", ttl_seconds=3600), True)
check("ledger: named lock steals a STALE lock (past the TTL)",
      _ledL.try_acquire_named_lock("monitor", "b:3", "2026-06-13T12:00:00Z", ttl_seconds=3600), True)
check("ledger: the stolen lock names the new holder", _ledL.named_lock("monitor")["holder"], "b:3")
check("ledger: a live holder within the TTL keeps the lock",
      _ledL.try_acquire_named_lock("monitor", "b:4", "2026-06-13T12:01:00Z", ttl_seconds=3600,
                                   holder_dead=lambda h: False), False)
check("ledger: a dead holder loses the lock at once (SIGKILL skips finally)",
      _ledL.try_acquire_named_lock("monitor", "b:4", "2026-06-13T12:01:00Z", ttl_seconds=3600,
                                   holder_dead=lambda h: True), True)
_ledL.release_named_lock("monitor", "b:other")
check("ledger: release by a non-holder leaves the lock", _ledL.named_lock("monitor")["holder"], "b:4")
_ledL.release_named_lock("monitor", "b:4")
check("ledger: release by the holder frees the lock", _ledL.named_lock("monitor"), None)
for _gone in ("try_acquire_run_lock", "release_run_lock", "run_lock_state"):
    check_true(f"ledger: the single-row {_gone} is removed", not hasattr(Ledger, _gone))

# run_lock context manager
_ledRL = _tmp_ledger()
_raised = False
# Live holders of this boot (this process and its parent), so holder_dead says "alive".
_h_self, _h_parent = _runlock.holder_id(), _runlock.holder_id(os.getppid())
with _runlock.run_lock(_ledRL, "worker", _h_self, "2026-06-13T10:00:00Z", ttl_seconds=3600):
    try:
        with _runlock.run_lock(_ledRL, "worker", _h_parent, "2026-06-13T10:00:30Z", ttl_seconds=3600):
            pass
    except _runlock.RunLockError:
        _raised = True
    with _runlock.run_lock(_ledRL, "broker", _h_parent, "2026-06-13T10:00:30Z", ttl_seconds=3600):
        check_true("runlock: another name takes its own lock inside", _ledRL.named_lock("broker") is not None)
check("runlock: a second acquire of the same name while held raises", _raised, True)
check("runlock: released after exit (lock free)", _ledRL.named_lock("worker"), None)
check("runlock: the inner lock released too", _ledRL.named_lock("broker"), None)
# A holder from an earlier boot cannot hold the lock: run_lock takes it at once.
_ledRL.try_acquire_named_lock("worker", "oldboot:1", "2026-06-13T10:00:00Z", ttl_seconds=3600)
with _runlock.run_lock(_ledRL, "worker", _h_self, "2026-06-13T10:00:10Z", ttl_seconds=3600):
    check("runlock: a holder of an earlier boot loses the lock at once",
          _ledRL.named_lock("worker")["holder"], _h_self)
check("runlock: holder_dead for another boot", _runlock.holder_dead("oldboot:1", this_boot="thisboot"), True)
check("runlock: holder_dead for a live pid of this boot",
      _runlock.holder_dead(f"thisboot:{os.getpid()}", this_boot="thisboot"), False)
check("runlock: holder_dead for a gone pid of this boot",
      _runlock.holder_dead("thisboot:123", this_boot="thisboot", alive=lambda p: False), True)
check("runlock: an unknown holder format is never dead (the TTL decides)",
      _runlock.holder_dead("garbage", this_boot="thisboot"), False)
check_true("runlock: holder_id is <boot_id>:<pid>", _runlock.holder_id(42).endswith(":42"))

# --- the offline healthcheck (§15.5): the new check list ---
import deploy.runner.healthcheck as _hc  # noqa: E402

check("healthcheck: the offline check list (§15.5)", [n for n, _f in _hc.OFFLINE],
      ["config", "market", "ledger", "fts5", "imports", "sdk", "claude_bin", "guard_selftest",
       "fts_integrity", "env", "dirs", "runbooks"])
check("healthcheck: the core import list (§15.5 step 4)", list(_hc.CORE_MODULES),
      ["lib.sizing", "lib.portfolio", "lib.goal", "lib.universe", "lib.runlock", "lib.strategy"])
check("healthcheck: the third-party import list", list(_hc.THIRD_PARTY), ["claude_agent_sdk", "bs4", "lxml"])
check("healthcheck: the runner import list", list(_hc.RUNNERS),
      ["run_monitor", "run_worker", "run_broker", "broker_guard", "chat_bridge"])
check("healthcheck: the runbook list (§15.5 step 8)", list(_hc.RUNBOOKS),
      ["prompts/desk.md", "prompts/analyst.md", "prompts/news_classifier.md", "prompts/broker.md",
       "EXECUTE.md", "SNAPSHOT.md"])
check_true("healthcheck: the eve_install check is deleted", not hasattr(_hc, "check_eve_install")
           and not hasattr(_hc, "_eve_install"))
check_true("healthcheck: every lib/desk module is in the import list",
           {"lib.desk." + p.stem for p in (_REPO / "lib" / "desk").glob("*.py") if p.stem != "__init__"}
           <= set(_hc._desk_modules()))
_hc_out = _hc.run_healthcheck()
check("healthcheck: offline self-test green", _hc_out["ok"], True)
if not _hc_out["ok"]:
    print("    healthcheck:", {k: v for k, v in _hc_out["checks"].items() if v.startswith("FAIL")})


# ============================================================================
# Prompts-as-markdown loader.
# ============================================================================
import lib.prompts as _prompts  # noqa: E402

for _pn in ("desk", "analyst", "news_classifier", "broker"):
    check_true(f"prompts: {_pn} prompt loads from md", len(_prompts.load_prompt(_pn).strip()) > 100)
check_raises("prompts: missing prompt raises", lambda: _prompts.load_prompt("nonexistent_xyz"), FileNotFoundError)
for _gone in ("orchestrator", "chat", "agents/market_analyst", "agents/news_analyst"):
    check_true(f"prompts: {_gone}.md is deleted (§14.4)", not (_REPO / "prompts" / f"{_gone}.md").exists())
# kwargs fill a .format template; no kwargs returns the raw text (braces untouched).
_pdir_saved = _prompts.PROMPTS_DIR
_ptmp = Path(tempfile.mkdtemp())
(_ptmp / "t_fmt.md").write_text("hello {asset_label}", encoding="utf-8")
_prompts.PROMPTS_DIR = _ptmp
_prompts._read.cache_clear()
try:
    check("prompts: raw load preserves a placeholder", _prompts.load_prompt("t_fmt"), "hello {asset_label}")
    check("prompts: kwargs fill the placeholder", _prompts.load_prompt("t_fmt", asset_label="ZZZ"), "hello ZZZ")
finally:
    _prompts.PROMPTS_DIR = _pdir_saved
    _prompts._read.cache_clear()

# --- validate_book guard (§14.2 universe.py keeps validate_book + tradable_universe) ---
_negbook = [{"ticker": "AAA", "target_weight": 60, "band": 5, "status": "active", "sleeve": "Tech"},
            {"ticker": "BBB", "target_weight": 45, "band": 5, "status": "active", "sleeve": "Tech"},
            {"ticker": "SGOV", "target_weight": -5, "band": 0, "status": "active", "sleeve": "Cash"}]
check_true("validate_book: rejects a negative weight (sum ~100)", _uni.validate_book(_negbook)[0] is False)
_okbook = [{"ticker": "AAA", "target_weight": 40, "band": 5, "status": "active", "sleeve": "Tech"},
           {"ticker": "SGOV", "target_weight": 60, "band": 0, "status": "active", "sleeve": "Cash"}]
check("validate_book: a 100% book with bands inside the weights is valid", _uni.validate_book(_okbook)[0], True)
_offbook = [{"ticker": "AAA", "target_weight": 40.3, "band": 5, "status": "active", "sleeve": "Tech"},
            {"ticker": "SGOV", "target_weight": 60, "band": 0, "status": "active", "sleeve": "Cash"}]
check("validate_book: default tolerance 0.5 accepts a 100.3 book", _uni.validate_book(_offbook)[0], True)
check("validate_book: tolerance 0.01 (apply_change) rejects a 100.3 book",
      _uni.validate_book(_offbook, tolerance=0.01)[0], False)
check("validate_book: a band >= its weight is rejected",
      _uni.validate_book([{"ticker": "X", "sleeve": "S", "target_weight": 50, "band": 50, "status": "active"},
                          {"ticker": "SGOV", "sleeve": "Cash", "target_weight": 50, "band": 0,
                           "status": "active"}])[0], False)
for _gone in ("validate_add", "apply_remove", "conserve_to_cash", "redistribute_to_cash", "apply_add"):
    check_true(f"universe: {_gone} is deleted (§14.2)", not hasattr(_uni, _gone))

# === F1 lib.trend long-horizon guideposts (pure math) =======================
import numpy as _np

def _line(n, slope=0.0, noise=0.0, base=100.0, seed=1):
    return [float(base + i * slope + (_np.random.RandomState(seed).normal(0, noise) if noise else 0.0))
            for i in range(n)]

from lib import trend as _tr

_up = _tr.trend_metrics(_line(300, slope=0.10, base=100.0), high=_line(300, 0.10, base=101.0), low=_line(300, 0.10, base=99.0))
check_true("trend: uptrend +1y", _up.ret_1y is not None and _up.ret_1y > 0)
check("trend: uptrend regime", _up.trend_regime, "UPTREND")
check_true("trend: uptrend +Sharpe", _up.sharpe is not None and _up.sharpe > 0)
check_true("trend: ADX computable on 300 rows", _up.adx is not None)

_down = _tr.trend_metrics(_line(300, slope=-0.10, base=100.0), high=_line(300, -0.10, base=101.0), low=_line(300, -0.10, base=99.0))
check_true("trend: downtrend -1y", _down.ret_1y is not None and _down.ret_1y < 0)
check("trend: downtrend regime", _down.trend_regime, "DOWNTREND")

_short = _tr.trend_metrics([100.0, 101.0])
check("trend: short series ADX None", _short.adx, None)
check("trend: short series regime RANGE", _short.trend_regime, "RANGE")
check("trend: short series Sharpe None", _short.sharpe, None)

_px = [100.0] + [100 + i for i in range(1, 21)] + [120 - i * 1.5 for i in range(1, 21)] + [90 + i for i in range(1, 40)]
_dd = _tr.drawdown_stats(_px)
check_true("trend: max DD ~25%", _dd.max_depth is not None and 0.24 < _dd.max_depth < 0.26)
check_true("trend: DD duration >=20 days", _dd.max_duration_days is not None and _dd.max_duration_days >= 20)
check("trend: ends at new high (current DD 0)", _dd.current_depth, 0.0)

check("trend: calmar None on no-DD", _tr.calmar(_line(300, slope=0.05, base=100.0)), None)
check("trend: 12-1 None on short", _tr.momentum_12_1(_line(100, slope=0.1)), None)
check_true("trend: 12-1 computable on 280", _tr.momentum_12_1(_line(280, slope=0.05)) is not None)

_md_tr = _tr.render_trend_report(_up)
check_true("trend: render has no ## subheadings", not any(l.startswith("## ") for l in _md_tr.splitlines()))
check("trend: None ADX -> RANGE", _tr.trend_regime(None, _tr.MAStats(None, None, None, None, None, None, None)), "RANGE")


# === F1: structure-first trend_regime (the RANGE->UPTREND fix + H4/H5 guardrails) =====
# The bug: a smooth multi-quarter uptrend in a normal pullback runs a MODERATE ADX (15-25)
# and/or a temporarily-negative 50d slope, so the OLD ADX-first classifier stamped it RANGE
# — and the HORIZON prompt made RANGE ineligible for a bullish rating. struct_up now reads
# long-horizon STRUCTURE first (above a rising 200d + golden cross + positive 12-1), ADX second.
_rg = _tr.trend_regime
def _ma_fix(above, gc, s50, s200, off):
    return _tr.MAStats(100.0, 90.0, s50, s200, above, gc, off)

# BINDING PROOF: adx=19 (< REGIME_RANGE_ADX=20) — under the OLD `adx<20 -> RANGE` gate this was
# forced RANGE; the ONLY path to UPTREND now is the new structure-first branch running FIRST.
check("trend F1: SMH pullback adx19 -> UPTREND (old gate forced RANGE)",
      _rg(19.0, _ma_fix(0.05, True, -0.01, 0.10, 0.12), mom_12_1=0.58, plus_di=39, minus_di=39), "UPTREND")
# 50d-slope veto case: adx 22 (>=20) but negative 50d slope -> OLD `up` failed -> RANGE; now UPTREND.
check("trend F1: pullback 50d-veto adx22 -> UPTREND (was RANGE)",
      _rg(22.0, _ma_fix(0.03, True, -0.02, 0.11, 0.13), mom_12_1=0.60, plus_di=40, minus_di=41), "UPTREND")
# H4 GUARDRAIL: a distributing top (DI- dominant AND >15% off the 52w high) is NOT a clean uptrend.
check("trend F1/H4: distributing top (DI- dom, deep) -> RANGE (not UPTREND)",
      _rg(23.0, _ma_fix(0.03, True, -0.02, 0.10, 0.22), mom_12_1=0.80, plus_di=21, minus_di=47), "RANGE")
# GUARDRAIL: a rolled-over name (death cross, neg 12-1, falling+below 200d) stays DOWNTREND (L3: above<0).
check("trend F1: CEG death-cross -> DOWNTREND (guardrail; above_200d<0)",
      _rg(30.0, _ma_fix(-0.20, False, -0.05, -0.03, 0.30), mom_12_1=-0.16, plus_di=15, minus_di=40), "DOWNTREND")
# young name (valid 200d but <253 rows so mom None) qualifies on structure alone (mom tolerant-None).
check("trend F1: young mom=None on structure -> UPTREND",
      _rg(15.0, _ma_fix(0.08, True, 0.01, 0.05, 0.05), mom_12_1=None, plus_di=25, minus_di=20), "UPTREND")
# NO false positive: negative 12-1 despite up-structure blocks struct_up.
check("trend F1: neg 12-1 blocks struct_up -> RANGE",
      _rg(18.0, _ma_fix(0.05, True, -0.01, 0.10, 0.10), mom_12_1=-0.05, plus_di=30, minus_di=28), "RANGE")
# NO false positive: falling 200d despite price above blocks struct_up.
check("trend F1: falling 200d blocks struct_up -> RANGE",
      _rg(18.0, _ma_fix(0.05, True, -0.01, -0.02, 0.10), mom_12_1=0.50, plus_di=30, minus_di=28), "RANGE")
# genuine low-ADX chop (mixed 200d slope) stays RANGE.
check("trend F1: low-adx chop -> RANGE",
      _rg(12.0, _ma_fix(0.01, True, 0.0, -0.001, 0.10), mom_12_1=0.02, plus_di=22, minus_di=21), "RANGE")
# existing steep fixtures unchanged (regression).
check("trend F1: steep uptrend still UPTREND", _up.trend_regime, "UPTREND")
check("trend F1: steep downtrend still DOWNTREND", _down.trend_regime, "DOWNTREND")

# === Data-health audit fixes (2026-07-13): pin the fetcher-channel repairs ========
# BUG 5 — total_return is None when the series is too short for the horizon (the old
# min(lookback+1,2) guard clamped to since-inception and mislabeled it as e.g. "3y").
check("dh: total_return None below horizon", _tr.total_return([100.0 + i for i in range(50)], 252), None)
check_true("dh: total_return computes when covered", _tr.total_return([100.0 + i for i in range(300)], 252) is not None)
# BUGs 5+7 — a thin/young name: long horizons + annualized ratios render None (n/a),
# NOT fabricated identical 6m=1y=2y=3y values / noise ratios on a tiny sample.
_thin_b = _tr.trend_metrics(_line(42, slope=0.2, base=100.0))
check("dh: thin ret_3y None", _thin_b.ret_3y, None)
check("dh: thin ret_1y None", _thin_b.ret_1y, None)
check("dh: thin Sharpe None (min-N gate)", _thin_b.sharpe, None)
check("dh: thin Calmar None (min-N gate)", _thin_b.calmar, None)
check_true("dh: mature Sharpe present (300 rows)", _up.sharpe is not None)
check_true("dh: mature ret_1y present (300 rows)", _up.ret_1y is not None)
# BUG 6 — coverage label derived from N, never a hard-coded ~3y on short history.
_thin_hdr = _tr.render_trend_report(_thin_b).splitlines()[0]
check_true("dh: thin coverage not ~3y", "~3y" not in _thin_hdr)
check_true("dh: thin coverage shows mo/rows", ("mo)" in _thin_hdr) or ("rows)" in _thin_hdr))
check_true("dh: mature coverage shows ~Ny", "y)" in _tr.render_trend_report(_up).splitlines()[0])


# === Fetcher data-integrity fixes (2026-08-01), the kept part ========================
# F1: load_ohlcv once anchored its OHLCV window to TODAY instead of curr_date, so a dated
# run got a SHORT frame. stockstats rolls with min_periods=1, so that short mean came back
# labelled `close_200_sma`. The cache key omitted curr_date, so the answer also depended on
# the day you asked. get_technicals (§14.2) reads load_ohlcv; these cases pin the fix.
import shutil as _fsh  # noqa: E402
import datetime as _fdt  # noqa: E402
import numpy as _fnp  # noqa: E402
import pandas as _fpd  # noqa: E402
import lib.dataflows.stockstats_utils as _ssu  # noqa: E402
import lib.dataflows.config as _dfcfg  # noqa: E402
from stockstats import wrap as _fwrap  # noqa: E402
from lib.dataflows.errors import DataUnavailableError as _fDUE  # noqa: E402

# --- F1c: min_periods_for — a literal table over the CLOSED live indicator set.
# (Measured: a 5-bar frame returns a confident number for ALL 11, so a name-parsing
# heuristic would have masked only 3.)
for _fname, _fwant in [("close_200_sma", 200), ("close_50_sma", 50), ("close_10_ema", 10),
                       ("rsi", 14), ("atr", 14), ("boll", 20), ("boll_ub", 20),
                       ("boll_lb", 20), ("macd", 26), ("macds", 34), ("macdh", 34)]:
    check(f"fetch: min_periods_for({_fname})", _ssu.min_periods_for(_fname), _fwant)
# numeric-suffix fallback for names outside the table
check("fetch: min_periods_for(rsi_14) fallback", _ssu.min_periods_for("rsi_14"), 14)
check("fetch: min_periods_for(close_20_sma) fallback", _ssu.min_periods_for("close_20_sma"), 20)
# genuinely unknown, no window in the name -> None (never mask on a guess)
for _fname in ("vwap", "close", "supertrend"):
    check(f"fetch: min_periods_for({_fname}) is None", _ssu.min_periods_for(_fname), None)


def _fsynth(n, start_price=100.0, drift=0.5):
    """n business-day bars on a steady uptrend (deterministic, no network)."""
    _d = _fpd.bdate_range("2024-01-02", periods=n)
    _c = start_price + drift * _fnp.arange(n)
    return _fpd.DataFrame({"Date": _d, "Open": _c, "High": _c * 1.01, "Low": _c * 0.99,
                           "Close": _c, "Volume": 1_000_000})


# --- F1c: a frame SHORTER than the indicator's window yields NaN, not a short mean.
# RED BEFORE FIX: stockstats returned 139.0 — the 157-bar mean — as the "200-day SMA".
_fshort = _fwrap(_fsynth(157).copy())
check_true("fetch: close_200_sma NaN on a 157-bar frame",
           _fpd.isna(_ssu.indicator_at(_fshort, "close_200_sma")))
check_true("fetch: close_50_sma still real on 157 bars (157 >= 50)",
           not _fpd.isna(_ssu.indicator_at(_fshort, "close_50_sma")))
# Pin the VALUE on a long frame, so masking can't be faked by returning NaN unconditionally.
_flong = _fwrap(_fsynth(400).copy())
check("fetch: close_200_sma value on 400 bars",
      round(float(_ssu.indicator_at(_flong, "close_200_sma")), 4),
      round(float(_fsynth(400)["Close"].tail(200).mean()), 4))

# --- F1a/F1b: the download window + cache key anchor to curr_date, not to today.
# The temp-dir swap is MANDATORY: data_cache_dir is the relative "state/cache" and
# run_e2e.sh cds to the repo root, so an unisolated call writes into the real repo.
_fcache = tempfile.mkdtemp(prefix="quiver-test-cache-")
_forig_cache = _dfcfg.get_config()["data_cache_dir"]
_forig_dl = _ssu.yf.download
_fdl = []


def _ffake_download(symbol, start=None, end=None, **kw):
    _fdl.append({"symbol": symbol, "start": start, "end": end})
    _f = _fsynth(1400)
    _f["Date"] = _fpd.bdate_range(end=_fpd.to_datetime(end) - _fpd.Timedelta(days=1), periods=1400)
    return _f.set_index("Date")


def _flast_dl():
    """Last recorded download, or a blank record. A missing call is itself a FAILURE mode
    (a stale cache key serving the wrong window), so it must fail a check — never abort the
    file with an IndexError and take every later check down with it."""
    return _fdl[-1] if _fdl else {"symbol": None, "start": None, "end": None}


_dfcfg.set_config({"data_cache_dir": _fcache})
_ssu.yf.download = _ffake_download
try:
    _ftoday = _fpd.Timestamp.today().normalize()
    _ftoday_s = _ftoday.strftime("%Y-%m-%d")
    # (a) LIVE INVARIANCE (a PIN, green either way by construction): when
    # to_datetime(curr_date).normalize() == Timestamp.today().normalize(), the requested
    # window must be byte-identical to the old today-anchored one.
    _fdl.clear()
    _ssu.load_ohlcv("TESTX", _ftoday_s)
    check("fetch: live download called once", len(_fdl), 1)
    check("fetch: live start == old today-anchored start", _flast_dl()["start"],
          (_fpd.Timestamp.today() - _fpd.DateOffset(years=5)).strftime("%Y-%m-%d"))
    check("fetch: live end == old today-anchored end", _flast_dl()["end"], _ftoday_s)

    # (b) RED-FIRST: a HISTORICAL curr_date anchors the window to curr_date.
    _fdl.clear()
    _fhist = _ssu.load_ohlcv("TESTX", "2022-03-01")
    check("fetch: historical download called once", len(_fdl), 1)
    check("fetch: historical start is curr_date - 5y", _flast_dl()["start"], "2017-03-01")
    check("fetch: historical end includes curr_date (+1d; yfinance end is exclusive)",
          _flast_dl()["end"], "2022-03-02")
    check("fetch: historical frame ends at curr_date",
          str(_fhist["Date"].max().date()), "2022-03-01")
    # The headline defect: today this frame is 147 bars, far short of the 200 the SMA claims.
    check_true("fetch: historical frame has a FULL window (>=1000 bars, was 147)",
               len(_fhist) >= 1000)

    # (c) RED-FIRST: the cache key differs per curr_date, so the historical run cannot be
    # served the live run's file (that is the month-to-month reproducibility defect).
    check("fetch: two curr_dates -> two distinct cache files", len(os.listdir(_fcache)), 2)

    # (d) curr_date one day back: the start shifts by a day (asserted, not discovered).
    # The live caller passes an ET date while Timestamp.today() is the process-local clock,
    # so after 20:00 ET they disagree by one day — this pins that consequence.
    # NOTE the distinct symbol. On a LEAP DAY, pd.DateOffset(years=5) clamps both 02-29 and
    # 02-28 to the same 02-28, and `end` clamps to today for both — so under one symbol this
    # probe's cache filename would equal (a)'s, hit the cache, never call the stub, and the
    # suite would go red every Feb 29 for a reason unrelated to that day's change.
    _fdl.clear()
    _fyest = (_ftoday - _fpd.Timedelta(days=1))
    _ssu.load_ohlcv("TESTY", _fyest.strftime("%Y-%m-%d"))
    check("fetch: yesterday start is yesterday - 5y", _flast_dl()["start"],
          (_fyest - _fpd.DateOffset(years=5)).strftime("%Y-%m-%d"))
    check("fetch: yesterday end clamps to today (never the future)", _flast_dl()["end"], _ftoday_s)

    # (e) an empty curr_date must RAISE, not rot into a NaT strftime accident.
    check_raises("fetch: load_ohlcv(None) raises", lambda: _ssu.load_ohlcv("TESTX", None),
                 ValueError)

    # (f) AN EMPTY DOWNLOAD MUST NEVER BE CACHED.
    # yfinance swallows ANY per-ticker network error into an empty frame (multi.py: the
    # except branch stores utils.empty_df()), so a blip returns 0 rows rather than raising,
    # and yf_retry only covers YFRateLimitError. Persisting that empty frame used to be
    # survivable because the cache key rolled with the wall clock and self-healed the next
    # day — but a curr_date-anchored key for a PAST date never rolls, so a single blip would
    # poison that (symbol, curr_date) permanently. Validate before publishing.
    _fdl.clear()
    _fempty_calls = []

    def _fblip(symbol, start=None, end=None, **kw):
        _fempty_calls.append(symbol)
        return _fpd.DataFrame(columns=["Adj Close", "Close", "High", "Low", "Open", "Volume"])

    def _fblip_dated(symbol, start=None, end=None, **kw):
        # The SILENT variant: an empty frame that still carries a Date column, so cleaning
        # does NOT raise — it just yields 0 bars. Caught by the `.empty` half of the guard.
        _fempty_calls.append(symbol)
        return _fpd.DataFrame(columns=["Date", "Open", "High", "Low", "Close", "Volume"])

    for _flabel, _fstub in (("no Date col", _fblip), ("with Date col (silent)", _fblip_dated)):
        _ssu.yf.download = _fstub
        try:
            check_raises(f"fetch: empty download RAISES [{_flabel}]",
                         lambda: _ssu.load_ohlcv("BLIPX", "2022-03-01"), _fDUE)
            check(f"fetch: empty download writes NO cache file [{_flabel}]",
                  [f for f in os.listdir(_fcache) if f.startswith("BLIPX")], [])
        finally:
            _ssu.yf.download = _ffake_download
    # ...and the very next healthy call must succeed, i.e. the blip left nothing behind.
    _fheal = _ssu.load_ohlcv("BLIPX", "2022-03-01")
    check_true("fetch: a healthy retry after a blip self-heals", len(_fheal) >= 1000)
finally:
    _ssu.yf.download = _forig_dl
    _dfcfg.set_config({"data_cache_dir": _forig_cache})
    _fsh.rmtree(_fcache, ignore_errors=True)
check("fetch: cache dir restored", _dfcfg.get_config()["data_cache_dir"], _forig_cache)


# --- F2: the flat (yf.Search) shape carries providerPublishTime, a Unix epoch int.
import lib.dataflows.yfinance_news as _ynm  # noqa: E402

_fepoch = 1785418357
check("fetch: flat pub_date parsed from epoch",
      _ynm._extract_article_data({"title": "T", "providerPublishTime": _fepoch})["pub_date"],
      _fdt.datetime.fromtimestamp(_fepoch, _fdt.timezone.utc).replace(tzinfo=None))
for _fbad in ({}, {"providerPublishTime": None}, {"providerPublishTime": 0},
              {"providerPublishTime": "nope"}, {"providerPublishTime": float("nan")}):
    check(f"fetch: flat pub_date None on {_fbad}",
          _ynm._extract_article_data(_fbad)["pub_date"], None)
# The NESTED shape is what yf.Ticker.get_news() returns — F2 leaves it as it was.
check("fetch: nested pub_date still parsed from pubDate ISO",
      _ynm._extract_article_data({"content": {"title": "N", "pubDate": "2026-07-10T12:00:00Z"}})["pub_date"],
      _fdt.datetime(2026, 7, 10, 12, 0, tzinfo=_fdt.timezone.utc))


def _fts(y, m, d, h=12):
    return int(_fdt.datetime(y, m, d, h, tzinfo=_fdt.timezone.utc).timestamp())


def _fsearch(articles):
    """A search seam for fetch_news_items: (ticker, count) -> the given flat articles."""
    return lambda ticker, count: [dict(a) for a in articles]


def _fno_ticker_news(ticker, count):
    return []


# §14.2: fetch_news_items replaces get_news_yfinance and get_global_news_yfinance. It has no
# curr_date and no look-ahead guard: lib/desk/news.py applies its own 7-day window.
for _gone in ("get_news_yfinance", "get_global_news_yfinance"):
    check_true(f"fetch: {_gone} is deleted (§14.2)", not hasattr(_ynm, _gone))

# F2 on the row path: each flat epoch becomes an AWARE UTC pub_date on its row.
_fitems = _ynm.fetch_news_items("TESTX", 10, search_fn=_fsearch([
    {"title": "PAST HEADLINE", "publisher": "AP", "link": "l1", "providerPublishTime": _fts(2026, 7, 10)},
    {"title": "EVENING HEADLINE", "publisher": "AP", "link": "l2", "providerPublishTime": _fts(2026, 7, 14, 1)},
]), ticker_news_fn=_fno_ticker_news)
check("fetch: fetch_news_items keeps both rows in order", [r["title"] for r in _fitems],
      ["PAST HEADLINE", "EVENING HEADLINE"])
check("fetch: the row pub_date is the epoch as aware UTC", _fitems[0]["pub_date"],
      _fdt.datetime(2026, 7, 10, 12, tzinfo=_fdt.timezone.utc))
check("fetch: a 21:00 ET headline keeps its exact UTC instant (01:00Z next day)", _fitems[1]["pub_date"],
      _fdt.datetime(2026, 7, 14, 1, tzinfo=_fdt.timezone.utc))
check("fetch: each row has the URL the desk needs", [r["link"] for r in _fitems], ["l1", "l2"])
check("fetch: the row keys", set(_fitems[0]), {"title", "publisher", "link", "pub_date", "related_tickers"})
# The boundary hours of the old guard: fetch_news_items returns each instant unchanged.
for _flabel, _fargs in [("curr 12:00 ET", (2026, 7, 13, 16)), ("curr+1 09:30 ET", (2026, 7, 14, 13)),
                        ("curr+1 16:00 ET", (2026, 7, 14, 20)), ("curr+2 09:30 ET", (2026, 7, 15, 13))]:
    _fb = _ynm.fetch_news_items("TESTX", 10, search_fn=_fsearch([
        {"title": "BOUNDARY", "publisher": "AP", "link": "l", "providerPublishTime": _fts(*_fargs)}]),
        ticker_news_fn=_fno_ticker_news)
    check(f"fetch: the pub_date of [{_flabel}] is exact", _fb[0]["pub_date"] if _fb else None,
          _fdt.datetime(*_fargs, tzinfo=_fdt.timezone.utc))
# A title-less FLAT article stays SKIPPED; so does one with no link (the desk needs the URL).
check("fetch: a title-less flat article is skipped",
      _ynm.fetch_news_items("TESTX", 10, search_fn=_fsearch([
          {"publisher": "AP", "link": "l", "providerPublishTime": _fts(2026, 7, 10)}]),
          ticker_news_fn=_fno_ticker_news), [])
check("fetch: a link-less flat article is skipped",
      _ynm.fetch_news_items("TESTX", 10, search_fn=_fsearch([
          {"title": "NO LINK", "publisher": "AP", "providerPublishTime": _fts(2026, 7, 10)}]),
          ticker_news_fn=_fno_ticker_news), [])
check("fetch: a repeated link is kept once",
      len(_ynm.fetch_news_items("TESTX", 10, search_fn=_fsearch([
          {"title": "A", "link": "same", "providerPublishTime": _fts(2026, 7, 10)},
          {"title": "B", "link": "same", "providerPublishTime": _fts(2026, 7, 11)}]),
          ticker_news_fn=_fno_ticker_news)), 1)
# The per-ticker empty path: "no news" is an empty list, not an error string.
check("fetch: empty sources -> [] (no news, not an error)",
      _ynm.fetch_news_items("TESTX", 10, search_fn=lambda t, c: [], ticker_news_fn=_fno_ticker_news), [])


def _fboom(ticker, count):
    raise RuntimeError("yfinance down")


check_raises("fetch: every source fails -> the yfinance error is raised (never hidden in a string)",
             lambda: _ynm.fetch_news_items("TESTX", 10, search_fn=_fboom, ticker_news_fn=_fboom), RuntimeError)
check("fetch: the search fails, get_news answers -> its rows",
      [r["title"] for r in _ynm.fetch_news_items("TESTX", 10, search_fn=_fboom, ticker_news_fn=lambda t, c: [
          {"content": {"title": "NESTED", "pubDate": "2026-07-10T12:00:00Z",
                       "canonicalUrl": {"url": "https://x/n"}, "provider": {"displayName": "AP"}}}])], ["NESTED"])


# --- FACTOR ATTRIBUTION (lib/attribution.py) --------------------------------
# A DIAGNOSTIC, never a gate. These cases pin the three properties that make the
# difference between a usable estimator and a confident wrong number:
#   (1) the HAC convention (no small-sample correction) -- against GOLDEN
#       constants, so the check can never skip on a box without statsmodels;
#   (2) refusal on degenerate input -- pinv silently returns a plausible answer
#       on a rank-deficient design, so the rank check is load-bearing;
#   (3) the residual-df floor -- below it a point estimate is still reported but
#       every inference field is withheld.
import json as _json  # noqa: E402

from lib import attribution as _attr  # noqa: E402
from lib import goal as _goal_attr  # noqa: E402

_GOLD = _json.loads(
    (Path(__file__).resolve().parent / "fixtures" / "hac_golden.json").read_text()
)

for _case in _GOLD["cases"]:
    _f = _attr.ols_hac(_case["y"], _case["X"], lag=_case["lag"])
    _tag = f"n={_case['n']},k={_case['k']},lag={_case['lag']}"
    check_true(f"attribution: golden {_tag} returns a Fit", _f is not None)
    if _f is None:
        continue
    check_true(f"attribution: golden {_tag} estimates match statsmodels",
               max(abs(a - b) for a, b in zip(_f.estimates, _case["params"])) < 1e-8)
    check_true(f"attribution: golden {_tag} HAC stderrs match (no small-sample correction)",
               _f.stderr is not None
               and max(abs(a - b) for a, b in zip(_f.stderr, _case["bse"])) < 1e-8)
    check_true(f"attribution: golden {_tag} t-stats match",
               _f.t is not None
               and max(abs(a - b) for a, b in zip(_f.t, _case["tvalues"])) < 1e-8)

# Opportunistic cross-check: pins the convention in BOTH directions against the
# live library. Skips VISIBLY (statsmodels is in neither the lock nor pyproject).
try:
    import statsmodels.api as _sm  # noqa: E402
except Exception:  # noqa: BLE001
    skip("attribution: statsmodels cross-check",
         "statsmodels not installed (not in requirements.lock.txt) - golden fixture still ran")
else:
    _c = _GOLD["cases"][0]
    _yc, _Xc, _lc = _c["y"], _c["X"], _c["lag"]
    _res = _sm.OLS(_yc, _Xc).fit(cov_type="HAC",
                                 cov_kwds={"maxlags": _lc, "use_correction": False})
    _mine = _attr.ols_hac(_yc, _Xc, lag=_lc)
    check_true("attribution: matches statsmodels use_correction=False to 1e-8",
               _mine is not None and _mine.stderr is not None
               and max(abs(a - b) for a, b in zip(_mine.stderr, _res.bse)) < 1e-8)
    _res_c = _sm.OLS(_yc, _Xc).fit(cov_type="HAC",
                                   cov_kwds={"maxlags": _lc, "use_correction": True})
    _n_c, _k_c = _c["n"], _c["k"]
    _ratio = [b / a for a, b in zip(_res.bse, _res_c.bse)]
    check_true("attribution: use_correction=True is exactly sqrt(n/(n-k)) larger (convention pinned both ways)",
               max(abs(r - (_n_c / (_n_c - _k_c)) ** 0.5) for r in _ratio) < 1e-9)

# Recovery of a known beta at a real sample size.
_rng_a = __import__("numpy").random.default_rng(7)
_np_a = __import__("numpy")
_mkt = _rng_a.normal(scale=0.01, size=400)
_port = 0.85 * _mkt + _rng_a.normal(scale=0.002, size=400)
_fit_known = _attr.ols_hac(_port, _attr.with_intercept([[m] for m in _mkt]),
                           names=["intercept", "beta"])
check_true("attribution: recovers a known beta=0.85 at n=400",
           _fit_known is not None and abs(_fit_known.estimates[1] - 0.85) < 0.02)
check_true("attribution: reports inference when df floor is met",
           _fit_known is not None and _fit_known.stderr is not None
           and _fit_known.inference_withheld_reason == "")

# Degenerate designs must REFUSE, not answer.
check("attribution: singular design -> None",
      _attr.ols_hac([1.0, 2.0, 3.0, 4.0] * 5, [[1.0, 1.0]] * 20), None)
check("attribution: NaN in y -> None",
      _attr.ols_hac([1.0, float("nan"), 3.0] * 7, _attr.with_intercept([[float(i)] for i in range(21)])), None)
check("attribution: n <= k -> None",
      _attr.ols_hac([1.0, 2.0], [[1.0, 0.0], [1.0, 1.0]]), None)
check("attribution: empty input -> None", _attr.ols_hac([], []), None)

# THE DF FLOOR. Estimates survive; every inference field is withheld.
_small = _attr.ols_hac([0.01, -0.02, 0.005, -0.001],
                       _attr.with_intercept([[0.002], [-0.003], [0.001], [0.0005]]),
                       names=["intercept", "beta"])
check_true("attribution: n=4,k=2 still returns point estimates", _small is not None and len(_small.estimates) == 2)
check("attribution: n=4,k=2 withholds stderr", _small.stderr if _small else "x", None)
check("attribution: n=4,k=2 withholds t", _small.t if _small else "x", None)
check("attribution: n=4,k=2 withholds r2", _small.r2 if _small else "x", None)
check_true("attribution: n=4,k=2 states why inference was withheld",
           bool(_small and _small.inference_withheld_reason))
check_true("attribution: as_dict() carries no verdict/threshold/p-value key",
           _small is not None
           and not ({"verdict", "significant", "p", "p_value", "t_crit", "threshold", "pass"}
                    & set(_small.as_dict())))

# n=8,k=7 on PURE NOISE is the case that would print seven |t| of 4-12.
_rng_n = __import__("numpy").random.default_rng(11)
_noise_y = _rng_n.normal(size=8)
_noise_X = _attr.with_intercept(_rng_n.normal(size=(8, 6)).tolist())
_nf = _attr.ols_hac(_noise_y, _noise_X)
check_true("attribution: n=8,k=7 pure noise emits NO t-statistic", _nf is not None and _nf.t is None)

# A 300-draw sweep below the floor must NEVER emit a numeric t.
_rng_s = __import__("numpy").random.default_rng(3)
_leaked = 0
for _i in range(300):
    _nn = 4 + (_i % 8)
    _fs = _attr.ols_hac(_rng_s.normal(size=_nn),
                        _attr.with_intercept(_rng_s.normal(size=(_nn, 1)).tolist()))
    if _fs is not None and _fs.t is not None and _fs.n - _fs.k < _attr.RESIDUAL_DF_MIN:
        _leaked += 1
check("attribution: 300-draw sweep below the df floor leaks zero t-stats", _leaked, 0)

# Lag rule + clamp.
check("attribution: newey_west_lag(100) == 4", _attr.newey_west_lag(100), 4)
check("attribution: newey_west_lag(252) == 4", _attr.newey_west_lag(252), 4)
check("attribution: newey_west_lag clamps to n-k-1 at tiny n", _attr.newey_west_lag(5, 2), 2)
check("attribution: newey_west_lag(3,2) clamps to 0", _attr.newey_west_lag(3, 2), 0)

# --- equity-series hygiene (the filters that were provably inert before) -----
_REAL_PTS = [("2026-05-30", 100.0), ("2026-06-01", 100.0), ("2026-06-02", 98.02884646),
             ("2026-06-03", 245.980825135), ("2026-06-04", 245.37762008),
             ("2026-06-05", 244.5615508), ("2026-06-08", 241.90108934),
             ("2026-06-09", 51.398566535), ("2026-06-12", 242.9310406105),
             ("2026-07-06", 219.94)]

_kept, _drop = _attr.clean_equity_points(_REAL_PTS)
check_true("attribution: the real 241.90/51.40/242.93 corrupt reading is dropped",
           "2026-06-09" not in [d for d, _ in _kept])
check("attribution: the drop is REPORTED with a date and a reason",
      [(r["date"], r["reason"]) for r in _drop], [("2026-06-09", "spike_revert")])
check("attribution: no other real point is dropped", len(_kept), 9)

# A REAL 50% drawdown that does NOT retrace must be PRESERVED -- deleting it
# would erase the single most important point in an account's history.
_crash = [("d1", 200.0), ("d2", 100.0), ("d3", 98.0), ("d4", 101.0)]
check("attribution: a real non-retracing drawdown is NOT dropped",
      _attr.find_spike_reverts(_crash), [])

# Capture-window filter: a Saturday stamp and a 21:20 stamp are real rows today.
_win = {d: d not in ("2026-05-30", "2026-07-06") for d, _ in _REAL_PTS}
_kept_w, _drop_w = _attr.clean_equity_points(_REAL_PTS, in_window=_win)
check_true("attribution: out-of-window captures (Saturday, 21:20) are dropped",
           {"2026-05-30", "2026-07-06"} <= {r["date"] for r in _drop_w})
check_true("attribution: out-of-window drops carry the reason",
           all(r["reason"] == "capture_outside_window"
               for r in _drop_w if r["date"] in ("2026-05-30", "2026-07-06")))

# interval_returns: gap boundary is exact, flows and unexplained jumps refuse.
_span = {("a", "b"): 1, ("b", "c"): 2}
_r_ok, _d_ok = _attr.interval_returns([("a", 100.0), ("b", 101.0), ("c", 102.0)],
                                      trading_days=_span)
check("attribution: interval of exactly MAX_INTERVAL_TRADING_DAYS is KEPT", sorted(_r_ok), ["b"])
check("attribution: interval of MAX+1 is EXCLUDED",
      [(r["date"], r["reason"]) for r in _d_ok], [("c", "interval_gap")])

_r_f, _d_f = _attr.interval_returns([("a", 100.0), ("b", 101.0)], flows={"b": 50.0})
check("attribution: an interval containing a recorded cash flow is dropped, not adjusted",
      [(r["date"], r["reason"]) for r in _d_f], [("b", "recorded_cash_flow")])

# The real unrecorded ~$148 deposit (98.03 -> 245.98, +150.9%) is a LEVEL SHIFT a
# spike filter cannot catch, and at small n it single-handedly sets the slope.
_r_j, _d_j = _attr.interval_returns([("2026-06-02", 98.02884646), ("2026-06-03", 245.980825135)])
check("attribution: the unrecorded deposit-sized jump is REFUSED",
      [(r["date"], r["reason"]) for r in _d_j], [("2026-06-03", "suspected_unrecorded_flow")])
check("attribution: refusing it leaves no return for that interval", _r_j, {})

# ...and there is NO order-activity escape hatch. An equity order converts cash
# into stock at equal value, so it cannot move TOTAL equity; whitelisting on order
# days let this very deposit through the guard built to catch it.
check_true("attribution: interval_returns takes no order-activity whitelist",
           "explained" not in __import__("inspect").signature(_attr.interval_returns).parameters)
_r_o, _d_o = _attr.interval_returns(
    [("2026-06-02", 98.02884646), ("2026-06-03", 245.980825135)])
check("attribution: a deposit-sized jump is refused even on a heavy order day",
      [(r["date"], r["reason"]) for r in _d_o], [("2026-06-03", "suspected_unrecorded_flow")])

# CONVENTION CROSS-CHECK: compounding the per-interval returns must reproduce the
# cumulative time-weighted return exactly. Pins the convention by arithmetic
# rather than by prose, so the two can never drift apart.
_clean_pts = [("2026-06-03", 245.980825135), ("2026-06-04", 245.37762008),
              ("2026-06-05", 244.5615508), ("2026-06-08", 241.90108934)]
_ri, _ = _attr.interval_returns(_clean_pts)
_compounded = 1.0
for _d in sorted(_ri):
    _compounded *= (1.0 + _ri[_d])
_twr = _goal_attr.time_weighted_return_pct(
    _clean_pts[0][1], _clean_pts[0][0], _clean_pts[1:], [])
check_true("attribution: prod(1+r_i)-1 equals goal.time_weighted_return_pct (convention pinned)",
           _twr is not None and abs((_compounded - 1.0) * 100.0 - _twr) < 1e-12)

# --- book_beta: the PRIMARY (bottom-up) exposure estimate -------------------
_rng_b = __import__("numpy").random.default_rng(42)
_mkt_b = _rng_b.normal(scale=0.01, size=500)
_rets_b = {"AAA": 1.2 * _mkt_b + _rng_b.normal(scale=0.001, size=500),
           "BBB": 0.4 * _mkt_b + _rng_b.normal(scale=0.001, size=500)}
_bb = _attr.book_beta({"AAA": 60.0, "BBB": 40.0}, _rets_b, _mkt_b)
_expect_b = (60.0 * _bb["per_name"]["AAA"]["beta"]
             + 40.0 * _bb["per_name"]["BBB"]["beta"]) / _bb["weight_scale"]
check_true("attribution: beta_book is the exact weighted sum of per-name betas",
           abs(_bb["beta_book"] - _expect_b) < 1e-9)
check_true("attribution: per-name betas recover 1.2 / 0.4",
           abs(_bb["per_name"]["AAA"]["beta"] - 1.2) < 0.05
           and abs(_bb["per_name"]["BBB"]["beta"] - 0.4) < 0.05)
check_true("attribution: n>=500 per name means inference is NOT withheld",
           _bb["per_name"]["AAA"]["stderr"] is not None)

# An unpriced name must SURFACE, never be renormalized away.
_bb2 = _attr.book_beta({"AAA": 60.0, "SPCX": 10.0}, {"AAA": _rets_b["AAA"]}, _mkt_b)
check("attribution: an unpriced holding is reported as unpriced weight",
      _bb2["unpriced_weight_pct"], 10.0)
check_true("attribution: the unpriced name is named", "SPCX" in _bb2["unpriced"])
check_true("attribution: unpriced weight is NOT renormalized away",
           abs(_bb2["beta_book"]
               - (60.0 / _bb2["weight_scale"]) * _bb2["per_name"]["AAA"]["beta"]) < 1e-9)
check("attribution: total weight still reflects the real book", _bb2["total_weight_pct"], 70.0)

# WEIGHT SCALE. A book quoted in percent (sums to 100) and one quoted in
# fractions (sums to 1) must give the SAME beta. Getting this wrong multiplies the
# answer by 100 -- a wrong number that still looks like a number. Found live: the
# real book first reported beta_book = 149.76 instead of 1.4976.
_w_pct = {"AAA": 60.0, "BBB": 40.0}
_w_frac = {"AAA": 0.6, "BBB": 0.4}
check("attribution: percent-quoted book detected as scale 100",
      _attr.detect_weight_scale(_w_pct), 100.0)
check("attribution: fraction-quoted book detected as scale 1",
      _attr.detect_weight_scale(_w_frac), 1.0)
_bb_pct = _attr.book_beta(_w_pct, _rets_b, _mkt_b)
_bb_frac = _attr.book_beta(_w_frac, _rets_b, _mkt_b)
check_true("attribution: percent and fraction books give the SAME beta_book",
           abs(_bb_pct["beta_book"] - _bb_frac["beta_book"]) < 1e-9)
check_true("attribution: a 60/40 book of beta-1.2 and beta-0.4 names lands near 0.88",
           0.83 < _bb_pct["beta_book"] < 0.93)
check("attribution: the applied weight scale is REPORTED, never silent",
      _bb_pct["weight_scale"], 100.0)
check_true("attribution: an explicit weight_scale overrides detection",
           abs(_attr.book_beta(_w_pct, _rets_b, _mkt_b, weight_scale=1.0)["beta_book"]
               - 100.0 * _bb_pct["beta_book"]) < 1e-6)

# DATE ALIGNMENT. Pairing a holding's returns to the market BY POSITION silently
# regresses a late-listing or gappy name against the wrong days. Found live: a
# synthetic name with a TRUE beta of 2.0 trading only the first 30 of 100 market
# days reported 0.028. book_beta must REFUSE a length mismatch, and the caller
# must pass a date-aligned per-ticker market series.
_rng_al = __import__("numpy").random.default_rng(1)
_mkt_al = list(_rng_al.normal(scale=0.01, size=100))
_early = [2.0 * _mkt_al[i] for i in range(30)]   # exact beta 2.0, first 30 days only
_bb_bad = _attr.book_beta({"T": 100.0}, {"T": _early}, _mkt_al)
check_true("attribution: a length-mismatched series is REFUSED, not tail-sliced",
           "T" in _bb_bad["unpriced"] and _bb_bad["beta_book"] is None)
check_true("attribution: the refusal states the mismatch",
           "length mismatch" in _bb_bad["unpriced_reasons"].get("T", ""))
_bb_good = _attr.book_beta({"T": 100.0}, {"T": _early}, _mkt_al,
                           market_by_ticker={"T": _mkt_al[:30]})
check_true("attribution: a DATE-aligned market series recovers the true beta 2.0",
           abs(_bb_good["per_name"]["T"]["beta"] - 2.0) < 1e-6)
check_true("attribution: every unpriced name carries a stated reason",
           all(t in _bb_bad["unpriced_reasons"] for t in _bb_bad["unpriced"]))


# --- ISOLATION: attribution must not be reachable from the sizing path (§14.10) ------
check_true("attribution: lib/attribution.py is inside the wall roster (zero grants)",
           "lib/attribution.py" in _WALL_ROSTER and "lib/attribution.py" not in _WALL_GRANTS)
# §14.10 says 8 grants. The code needs 9: lib/desk/agent.py and lib/universe.py add one
# each and lib/sizing.py drops its dead grant (see the _WALL_GRANTS comments).
check("attribution: _WALL_GRANTS has exactly 9 entries", len(_WALL_GRANTS), 9)
check_true("attribution: reaches zero ungranted wall items",
           not _wall_violations((_REPO / "lib" / "attribution.py").read_text(encoding="utf-8"),
                                "lib/attribution.py"))

for _consumer in ("lib/sizing.py", "lib/memory.py", "lib/desk/proposals.py", "lib/desk/execution.py"):
    _csrc = (_REPO / _consumer).read_text(encoding="utf-8")
    check_true(f"attribution: {_consumer} does not import lib.attribution (sizing isolation)",
               not _hits(_imported_modules(_csrc, _consumer), {"lib.attribution"})
               and "attribution" not in _csrc)

# --- benchmark measurement: the calendar anchor rule (lib/benchmark) ----------
# The defect being pinned: outcomes.benchmark_return was priced off a SPY series
# that stopped at 2026-07-02 while the position leg used 2026-07-06, understating
# the market leg 2.2x (0.007521 vs the true 0.016314573). Every case below uses the
# REAL dates and the REAL closes from that incident.
from lib import benchmark as _bm  # noqa: E402

_BM_CLOSES = {"2026-05-29": 754.5, "2026-06-08": 739.219971,
              "2026-07-02": 744.780029, "2026-07-06": 751.280029, "2026-07-09": 751.710022}
# XNYS sessions across the window. 2026-05-30 is a Saturday and 2026-07-03 is the
# observed July 4th holiday — both absent, which is exactly why a lag heuristic
# cannot separate a legitimate gap from a truncated series.
_BM_SESSIONS = ["2026-05-29", "2026-06-08", "2026-07-02", "2026-07-06", "2026-07-07",
                "2026-07-08", "2026-07-09"]

_bm_v, _bm_why = _bm.window_return(_BM_CLOSES, "2026-06-08", "2026-07-06", _BM_SESSIONS)
check_true("benchmark: T1 true window 06-08->07-06 == 0.016314573",
           _bm_v is not None and abs(_bm_v - 0.016314572756584766) < 1e-9)

# T2 — THE BUG. Series truncated before the end session: refuse, never substitute
# the last close it happens to have (which would reproduce 0.007521 exactly).
_bm_trunc = {k: v for k, v in _BM_CLOSES.items() if k <= "2026-07-02"}
_bm_v, _bm_why = _bm.window_return(_bm_trunc, "2026-06-08", "2026-07-06", _BM_SESSIONS)
check("benchmark: T2 truncated series refuses rather than using a stale close", _bm_v, None)
check_true("benchmark: T2 refusal names the missing session", "2026-07-06" in _bm_why)

# T2b — INTERIOR gap. A series that resumes AFTER the target still refuses; the
# straddle/lag formulations both pass this one, which is why the rule is calendar-exact.
_bm_gap = {"2026-06-08": 739.219971, "2026-07-02": 744.780029, "2026-07-09": 751.710022}
_bm_v, _ = _bm.window_return(_bm_gap, "2026-06-08", "2026-07-06", _BM_SESSIONS)
check("benchmark: T2b interior gap {07-02,07-09} refuses for 07-06", _bm_v, None)

# T2c — alignment + degenerate inputs.
_bm_v, _ = _bm.window_return(_BM_CLOSES, "2026-05-30", "2026-06-08", _BM_SESSIONS)
check_true("benchmark: T2c Saturday trade_date 05-30 anchors back to Fri 05-29",
           _bm_v is not None and abs(_bm_v - (739.219971 - 754.5) / 754.5) < 1e-12)
check(("benchmark: T2c ISO timestamp end date is accepted"),
      _bm.window_return(_BM_CLOSES, "2026-06-08", "2026-07-06T21:25:17-04:00", _BM_SESSIONS)[0],
      _bm.window_return(_BM_CLOSES, "2026-06-08", "2026-07-06", _BM_SESSIONS)[0])
check("benchmark: T2c same session == 0.0",
      _bm.window_return(_BM_CLOSES, "2026-06-08", "2026-06-08", _BM_SESSIONS)[0], 0.0)
check("benchmark: T2c end before start refuses",
      _bm.window_return(_BM_CLOSES, "2026-07-06", "2026-06-08", _BM_SESSIONS)[0], None)
check("benchmark: T2c empty series refuses",
      _bm.window_return({}, "2026-06-08", "2026-07-06", _BM_SESSIONS)[0], None)
check("benchmark: T2c no session on or before the target refuses",
      _bm.window_return(_BM_CLOSES, "2020-01-01", "2026-07-06", _BM_SESSIONS)[0], None)
# T2d — COVERAGE. A calendar that stops before the target must refuse, not fall back to
# its own last session. Without this the anchor collapses onto the series' last close and
# the tail guard dies — which is how the shipped producer (which bounded the calendar by
# max(series)) silently reproduced the 0.007521 incident value end-to-end.
_bm_short = [s for s in _BM_SESSIONS if s <= "2026-07-02"]
_bm_v, _bm_why = _bm.window_return(_BM_CLOSES, "2026-06-08", "2026-07-06", _bm_short)
check("benchmark: T2d a calendar ending before the target refuses on coverage", _bm_v, None)
check_true("benchmark: T2d the coverage refusal names the calendar end",
           "calendar ends" in _bm_why and "2026-07-02" in _bm_why)
check("benchmark: T2d no session calendar at all refuses",
      _bm.window_return(_BM_CLOSES, "2026-06-08", "2026-07-06", [])[0], None)
check("benchmark: T2c non-positive close refuses",
      _bm.window_return({**_BM_CLOSES, "2026-06-08": 0.0}, "2026-06-08", "2026-07-06",
                        _BM_SESSIONS)[0], None)
check("benchmark: T2c NaN close refuses",
      _bm.window_return({**_BM_CLOSES, "2026-07-06": float("nan")}, "2026-06-08", "2026-07-06",
                        _BM_SESSIONS)[0], None)

# T3 — the narrow writer must NOT behave like record_outcome (INSERT OR REPLACE over
# ten columns). Sentinels on the columns a naive backfill would silently blank.
_bml = _tmp_ledger()
_bmd = _bml.record_decision(trade_date="2026-06-08", ticker="SPYX", run_id="r", signal="Buy",
                            intent="buy", decided_at="2026-06-08T10:00:00-04:00",
                            decision_price=100.0)
_bml.record_outcome(_bmd, resolved_at="2026-07-06T21:25:17-04:00", holding_days=28,
                    directional_return=0.10, benchmark_return=0.007521, alpha=0.092479,
                    realized_pnl=12.5, unrealized_pnl=3.5, scored_against="both",
                    adherence="stop_violated")
check("benchmark: T3 narrow update reports one row", _bml.update_outcome_benchmark(_bmd, 0.5, -0.4), 1)
_bmr = [r for r in _bml.outcomes_for_backfill() if r["decision_id"] == _bmd][0]
check("benchmark: T3 benchmark_return updated", _bmr["benchmark_return"], 0.5)
check("benchmark: T3 alpha updated", _bmr["alpha"], -0.4)
_bmraw = dict(_bml.decisions_with_outcomes("SPYX")[0])
check("benchmark: T3 holding_days SURVIVES the narrow update", _bmraw["holding_days"], 28)
check("benchmark: T3 scored_against SURVIVES", _bmraw["scored_against"], "both")
check("benchmark: T3 adherence SURVIVES", _bmraw["adherence"], "stop_violated")
check("benchmark: T3 realized_pnl SURVIVES", _bmraw["realized_pnl"], 12.5)
check("benchmark: T3 directional_return SURVIVES", _bmraw["directional_return"], 0.10)
check("benchmark: T3 resolved_at is byte-identical (proves UPDATE, not INSERT OR REPLACE)",
      _bmr["resolved_at"], "2026-07-06T21:25:17-04:00")
check("benchmark: T3 clearing to NULL works (a refusal must not preserve a bad value)",
      (_bml.update_outcome_benchmark(_bmd, None, None),
       [r for r in _bml.outcomes_for_backfill() if r["decision_id"] == _bmd][0]["benchmark_return"]),
      (1, None))
check("benchmark: T3 unknown decision_id updates nothing",
      _bml.update_outcome_benchmark(999999, 0.1, 0.1), 0)

# T19 — the untrusted CLI must not be able to reach the trust seam. If a future flag
# exposed `trust_input_benchmark` on the reflect subparser, the caller-supplied
# benchmark path reopens silently and every other test here stays green.
_tick_src = (_REPO / "tick.py").read_text(encoding="utf-8")
# Match BOTH spellings: argparse maps "--trust-input-benchmark" to the dest
# `trust_input_benchmark`, so scanning only the underscore form would miss the
# idiomatic hyphenated flag and let the untrusted path reopen with the suite green.
_trust_lines = [ln.strip() for ln in _tick_src.splitlines()
                if "trust_input_benchmark" in ln or "trust-input-benchmark" in ln]
check_true("benchmark: T19 (sanity) the trust seam exists in tick.py", bool(_trust_lines))
check("benchmark: T19 no argparse flag registers the trust seam (the CLI cannot reach it)",
      [ln for ln in _trust_lines if "add_argument" in ln], [])
check("benchmark: T19 the seam is read only via getattr-with-default",
      [ln for ln in _trust_lines
       if "getattr" not in ln and "=" in ln and not ln.startswith("#")], [])


print(f"\n{PASS} passed, {FAIL} failed, {SKIP} skipped")
sys.exit(1 if FAIL else 0)
