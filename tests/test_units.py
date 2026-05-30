#!/usr/bin/env python3
"""Plain-assert unit tests for the pure decision logic (no pytest dependency).

Run: .venv/bin/python tests/test_units.py
"""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

from lib import notify, signals  # noqa: E402
from lib.config import ConfigError, load_config  # noqa: E402
from lib.ledger import Ledger  # noqa: E402

PASS = 0
FAIL = 0


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


# --- plan_action: long-only mapping ---
check("Buy/no-pos", signals.plan_action("Buy", False), ("buy", 1.0))
check("Buy/pos", signals.plan_action("Buy", True), ("buy", 1.0))
check("Overweight tilt", signals.plan_action("Overweight", False), ("buy", 0.5))
check("Hold", signals.plan_action("Hold", True), ("hold", 0.0))
check("Sell w/ pos -> close", signals.plan_action("Sell", True), ("sell", 1.0))
check("Sell no pos -> skip (no short)", signals.plan_action("Sell", False), ("skip", 0.0))
check("Underweight w/ pos -> trim", signals.plan_action("Underweight", True), ("sell", 0.5))
check("Underweight no pos -> skip", signals.plan_action("Underweight", False), ("skip", 0.0))
check("ERROR -> skip", signals.plan_action("ERROR", True), ("skip", 0.0))

# --- parse_sizing_to_dollars ---
check("pct of equity", signals.parse_sizing_to_dollars("~5% of capital", 10000), 500.0)
check("dollar amount", signals.parse_sizing_to_dollars("about $250 worth", 10000), 250.0)
check("bare dollars word", signals.parse_sizing_to_dollars("300 dollars", 10000), 300.0)
check("unparseable -> None", signals.parse_sizing_to_dollars("a modest position", 10000), None)
check("empty -> None", signals.parse_sizing_to_dollars(None, 10000), None)

# --- resolve_buy_dollars clamps ---
# 5% of 10k = 500, ceiling 500 -> 500
d, src = signals.resolve_buy_dollars("5%", 10000, 1.0, ceiling=500,
                                     remaining_daily_cap=1500, buying_power=5000,
                                     buffer=200, room_under_ticker_cap=1000)
check("buy clamps to ceiling", (d, src), (500.0, "parsed"))

# ceiling lower than parsed size
d, _ = signals.resolve_buy_dollars("50%", 10000, 1.0, ceiling=300,
                                   remaining_daily_cap=1500, buying_power=5000,
                                   buffer=200, room_under_ticker_cap=1000)
check("ceiling wins", d, 300.0)

# buying power - buffer is the binding constraint
d, _ = signals.resolve_buy_dollars("50%", 10000, 1.0, ceiling=5000,
                                   remaining_daily_cap=9000, buying_power=450,
                                   buffer=200, room_under_ticker_cap=9000)
check("buying-power buffer binds", d, 250.0)

# per-ticker room binds
d, _ = signals.resolve_buy_dollars("50%", 10000, 1.0, ceiling=5000,
                                   remaining_daily_cap=9000, buying_power=9000,
                                   buffer=0, room_under_ticker_cap=120)
check("per-ticker room binds", d, 120.0)

# Overweight tilt halves the parsed size
d, _ = signals.resolve_buy_dollars("4%", 10000, 0.5, ceiling=5000,
                                   remaining_daily_cap=9000, buying_power=9000,
                                   buffer=0, room_under_ticker_cap=9000)
check("overweight halves (4% of 10k = 400 -> 200)", d, 200.0)

# unparseable sizing -> conservative fallback (min(ceiling, 100)), never fails open
d, src = signals.resolve_buy_dollars("a small starter", 10000, 1.0, ceiling=500,
                                     remaining_daily_cap=1500, buying_power=5000,
                                     buffer=200, room_under_ticker_cap=1000)
check("fallback amount", d, 100.0)
check("fallback source tagged", src, "fallback")

# no room -> zero -> skip
d, _ = signals.resolve_buy_dollars("50%", 10000, 1.0, ceiling=5000,
                                   remaining_daily_cap=0, buying_power=9000,
                                   buffer=0, room_under_ticker_cap=9000)
check("daily cap exhausted -> 0", d, 0.0)

# --- resolve_sell_quantity ---
check("full close", signals.resolve_sell_quantity(3.0, 1.0), 3.0)
check("trim half", signals.resolve_sell_quantity(3.0, 0.5), 1.5)
check("never oversell", signals.resolve_sell_quantity(3.0, 2.0), 3.0)
check("nothing held", signals.resolve_sell_quantity(0.0, 1.0), 0.0)


# ============================ EMAIL DIGEST ==================================
import copy  # noqa: E402


def _write_config(d: dict) -> str:
    tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
    yaml.safe_dump(d, tmp)
    tmp.close()
    return tmp.name


def make_config(notify_block=None):
    d = {
        "account_number": "12345678",
        "dry_run": True,
        "kill_switch_file": "/tmp/bw_kill_test",
        "watchlist": ["AAPL"],
        "risk": {"max_dollars_per_trade": 25, "daily_loss_halt_pct": 5.0,
                 "daily_capital_deploy_cap": 75, "max_open_position_per_ticker": 50,
                 "min_buying_power_buffer": 5},
        "deepseek": {"chat_model": "deepseek-v4-flash", "reasoner_model": "deepseek-v4-pro"},
    }
    if notify_block is not None:
        d["notify"] = notify_block
    return load_config(_write_config(d))


def _tmp_ledger():
    return Ledger(Path(tempfile.mkdtemp()) / "ledger.db")


MODEL = {
    "date": "2026-05-30", "now_iso": "2026-05-30T16:00:00-04:00", "kind": "digest",
    "dry_run": True, "subject_prefix": "[Quiver]",
    "equity": 101.2, "baseline_equity": 100.0, "drop_pct": 1.2,
    "halted": False, "halt_reason": None, "event_detail": None,
    "tickers": [
        {"ticker": "AAPL", "signal": "Buy", "intent": "buy", "status": "dry_run",
         "detail": None, "amount": 25.0, "qty": None, "side": "buy",
         "broker_order_id": None, "decision": "Strong fundamentals; phased entry.",
         "debate": "Bull wins on Services growth."},
        {"ticker": "MSFT", "signal": "Hold", "intent": "hold", "status": "skipped",
         "detail": "hold", "amount": None, "qty": None, "side": None,
         "broker_order_id": None, "decision": "Fairly valued; wait.", "debate": "Stalemate."},
    ],
}

# --- notify.summarize ---
check("summarize collapses whitespace", notify.summarize("a\n\n  b   c"), "a b c")
check("summarize truncates w/ ellipsis", notify.summarize("x" * 1000, 10), "x" * 10 + "…")
check("summarize None -> empty", notify.summarize(None), "")

# --- notify.build_digest ---
_d = notify.build_digest(MODEL)
check("digest has all keys", set(_d.keys()), {"subject", "html", "text", "content_hash", "kind"})
check_true("subject has prefix + date", "[Quiver]" in _d["subject"] and "2026-05-30" in _d["subject"])
check_true("subject carries DRY RUN tag", "[DRY RUN]" in _d["subject"])
check_true("text shows ticker + signal", "AAPL" in _d["text"] and "Buy" in _d["text"])
check_true("dry-run renders 'no order placed'", "no order placed" in _d["text"])
check_true("html is summarized (~1 screen)", len(_d["html"]) < 20000)

# --- content_hash: deterministic, ignores time/equity, reacts to decisions ---
_h = notify.digest_hash(MODEL)
check("hash deterministic", notify.digest_hash(copy.deepcopy(MODEL)), _h)
_m_noise = copy.deepcopy(MODEL)
_m_noise["now_iso"] = "2026-05-30T23:59:00-04:00"
_m_noise["equity"] = 999.0
_m_noise["drop_pct"] = -42.0
check("hash ignores timestamp + equity", notify.digest_hash(_m_noise), _h)
_m_flip = copy.deepcopy(MODEL)
_m_flip["tickers"][0]["status"] = "placed"
check_true("hash changes when an outcome flips", notify.digest_hash(_m_flip) != _h)

# --- halt + auth_error templates ---
_m_halt = copy.deepcopy(MODEL)
_m_halt.update(kind="halt", halted=True, halt_reason="daily_loss -6.1%")
_dh = notify.build_digest(_m_halt)
check_true("halt subject flagged", "HALT" in _dh["subject"])
check_true("halt hashes distinctly from digest", _dh["content_hash"] != _h)
_m_auth = {"date": "2026-05-30", "now_iso": "t", "kind": "auth_error", "dry_run": False,
           "subject_prefix": "[Quiver]", "equity": None, "baseline_equity": None,
           "drop_pct": None, "halted": False, "halt_reason": None,
           "event_detail": "401 token expired", "tickers": []}
_da = notify.build_digest(_m_auth)
check_true("auth subject flagged", "AUTH ERROR" in _da["subject"])
check_true("auth body renders detail", "401" in _da["text"] and len(_da["text"]) > 20)

# --- config: notify block fail-safe + validation ---
check("notify off when absent", make_config().notify.enabled, False)
_cfg = make_config({"enabled": True, "to": ["a@b.com"], "from": "x@y.com", "subject_prefix": "[BW]"})
check("notify parsed when enabled",
      (_cfg.notify.enabled, _cfg.notify.to, _cfg.notify.from_addr, _cfg.notify.subject_prefix),
      (True, ["a@b.com"], "x@y.com", "[BW]"))
check("enabled must be exactly True (string -> off)",
      make_config({"enabled": "yes", "to": ["a@b.com"], "from": "x@y.com"}).notify.enabled, False)
check_raises("bad recipient address raises",
             lambda: make_config({"enabled": True, "to": ["nope"], "from": "x@y.com"}), ConfigError)
check_raises("empty recipients raises",
             lambda: make_config({"enabled": True, "to": [], "from": "x@y.com"}), ConfigError)
check_raises("malformed from (if set) raises",
             lambda: make_config({"enabled": True, "to": ["a@b.com"], "from": "nope"}), ConfigError)
check("blank from ok when enabled (MCP sender used)",
      make_config({"enabled": True, "to": ["a@b.com"], "from": ""}).notify.from_addr, "")
check("omitted from ok when enabled",
      make_config({"enabled": True, "to": ["a@b.com"]}).notify.from_addr, "")

# --- ledger: read-only rollups + notification dedup ---
_led = _tmp_ledger()
DAY = "2026-05-30"
check("get_baseline None before open", _led.get_baseline(DAY), None)
_led.get_or_create_baseline(DAY, 100.0, "2026-05-30T09:35:00-04:00")
_b = _led.get_baseline(DAY)
check_true("get_baseline reads back", _b is not None and _b.baseline_equity == 100.0 and _b.halted is False)
_led.record_action(DAY, "AAPL", signal="Buy", intent="buy", status="dry_run", detail="ok", now_iso="t")
_led.record_action(DAY, "MSFT", signal="Hold", intent="hold", status="skipped", detail="hold", now_iso="t")
check("day_actions ordered by ticker", [a["ticker"] for a in _led.day_actions(DAY)], ["AAPL", "MSFT"])
_rid = _led.new_ref_id()
_led.reserve_order(_rid, DAY, "AAPL", side="buy", type="market", dollar_amount=25.0, quantity=None, now_iso="t")
_ords = _led.day_orders(DAY)
check("day_orders count", len(_ords), 1)
check("day_orders amount", _ords[0]["dollar_amount"], 25.0)
check("no notify hash initially", _led.last_notified_hash(DAY, "digest"), None)
_led.mark_notified(DAY, "digest", "abc123", "a@b.com", "t")
check("notify hash recorded", _led.last_notified_hash(DAY, "digest"), "abc123")
check("notify hash isolated by kind", _led.last_notified_hash(DAY, "halt"), None)
_led.mark_notified(DAY, "digest", "def456", "a@b.com", "t2")
check("notify hash replaced when content changes", _led.last_notified_hash(DAY, "digest"), "def456")

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
    d = {
        "account_number": "12345678", "dry_run": True, "kill_switch_file": "/tmp/bw_kill_test",
        "watchlist": ["AAPL"],
        "risk": {"max_dollars_per_trade": 25, "daily_loss_halt_pct": 5.0,
                 "daily_capital_deploy_cap": 75, "max_open_position_per_ticker": 50,
                 "min_buying_power_buffer": 5},
        "deepseek": {"chat_model": "deepseek-v4-flash", "reasoner_model": "deepseek-v4-pro"},
    }
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


# ======================= MULTI-RUN + MODEL-DRIVEN CADENCE ===================

# --- pure gates ---
check("cooldown ok when no prior action", signals.cooldown_ok(None, "2026-05-30T10:00:00-04:00", 60), True)
check("cooldown blocks within window",
      signals.cooldown_ok("2026-05-30T10:00:00-04:00", "2026-05-30T10:30:00-04:00", 60), False)
check("cooldown ok after window",
      signals.cooldown_ok("2026-05-30T10:00:00-04:00", "2026-05-30T11:05:00-04:00", 60), True)
check("cooldown bad timestamp fails open", signals.cooldown_ok("garbage", "also-bad", 60), True)
check("action cap allows under", signals.within_action_cap(2, 3), True)
check("action cap blocks at limit", signals.within_action_cap(3, 3), False)
check("material change: no prior -> True", signals.is_material_change("Buy", "buy", None, None), True)
check("material change: identical -> False", signals.is_material_change("Buy", "buy", "Buy", "buy"), False)
check("material change: diff signal -> True", signals.is_material_change("Sell", "sell", "Buy", "buy"), True)

# --- clamp_review_minutes (Python owns the bound) ---
check("clamp below floor -> floor", signals.clamp_review_minutes(0.1, 30, 120), 30.0)   # 6min -> 30
check("clamp above ceiling -> ceiling", signals.clamp_review_minutes(10, 30, 120), 120.0)  # 600 -> 120
check("clamp within -> exact", signals.clamp_review_minutes(1, 30, 120), 60.0)
check("clamp None -> ceiling", signals.clamp_review_minutes(None, 30, 120), 120.0)
check("clamp non-positive -> ceiling", signals.clamp_review_minutes(0, 30, 120), 120.0)
check("clamp respects a looser (closed) ceiling", signals.clamp_review_minutes(10, 30, 1440), 600.0)

# --- ledger: action events + per-ticker schedule ---
_rled = _tmp_ledger()
RDAY = "2026-05-30"
_rled.record_event(trade_date=RDAY, ticker="AAPL", ts="2026-05-30T10:00:00-04:00",
                   signal="Buy", intent="buy", status="dry_run", run_id="r1")
_rled.record_event(trade_date=RDAY, ticker="AAPL", ts="2026-05-30T11:00:00-04:00",
                   signal="Buy", intent="buy", status="blocked_guardrail", run_id="r2")  # not completed
check("trade_actions_today counts completed trades only", _rled.trade_actions_today(RDAY, "AAPL"), 1)
_last = _rled.last_trade_action(RDAY, "AAPL")
check("last_trade_action is the completed one",
      (_last["status"], _last["ts"]), ("dry_run", "2026-05-30T10:00:00-04:00"))
check("trade_actions_today isolates ticker", _rled.trade_actions_today(RDAY, "MSFT"), 0)
check("no schedule initially", _rled.get_schedule(RDAY, "AAPL"), None)
check("bump_analysis sets count 1", _rled.bump_analysis(RDAY, "AAPL", next_due_ts="2026-05-30T12:00:00-04:00"), 1)
check("bump_analysis increments", _rled.bump_analysis(RDAY, "AAPL", next_due_ts="2026-05-30T13:00:00-04:00"), 2)
_sched = _rled.get_schedule(RDAY, "AAPL")
check("schedule next_due updated", _sched["next_due_ts"], "2026-05-30T13:00:00-04:00")
check("schedule analyses_today", _sched["analyses_today"], 2)

# --- REGRESSION: ticker_action snapshot stays latest-per-ticker (digest unbroken) ---
_rled.record_action(RDAY, "AAPL", signal="Buy", intent="buy", status="dry_run", detail="1", now_iso="t1")
_rled.record_action(RDAY, "AAPL", signal="Sell", intent="sell", status="dry_run", detail="2", now_iso="t2")
check("day_actions = one latest row per ticker (digest regression)",
      [(a["ticker"], a["signal"]) for a in _rled.day_actions(RDAY)], [("AAPL", "Sell")])


def make_loop_config(loop_block=None, risk_extra=None):
    d = {
        "account_number": "12345678", "dry_run": True, "kill_switch_file": "/tmp/bw_kill_test",
        "watchlist": ["AAPL"],
        "risk": {"max_dollars_per_trade": 25, "daily_loss_halt_pct": 5.0,
                 "daily_capital_deploy_cap": 75, "max_open_position_per_ticker": 50,
                 "min_buying_power_buffer": 5},
        "deepseek": {"chat_model": "deepseek-v4-flash", "reasoner_model": "deepseek-v4-pro"},
    }
    if loop_block is not None:
        d["loop"] = loop_block
    if risk_extra:
        d["risk"].update(risk_extra)
    return load_config(_write_config(d))


# --- config: intraday + cadence parsing/validation (default = classic) ---
check("intraday defaults OFF (classic once-a-day)", make_loop_config().intraday_enabled, False)
check("intraday enabled only on exact True", make_loop_config({"intraday_enabled": True}).intraday_enabled, True)
check("intraday string 'yes' -> OFF (fail-safe)", make_loop_config({"intraday_enabled": "yes"}).intraday_enabled, False)
_lc = make_loop_config()
check("cadence defaults", (_lc.review_floor_min, _lc.review_ceiling_open_min, _lc.review_ceiling_min), (30, 120, 1440))
check_raises("inverted cadence bounds raise",
             lambda: make_loop_config({"review_floor_min": 200, "review_ceiling_open_min": 100}), ConfigError)
check_raises("cooldown <= 0 raises", lambda: make_loop_config({"per_ticker_cooldown_min": 0}), ConfigError)
check("intraday caps default to 1 (== once-a-day)",
      (_lc.risk.max_actions_per_ticker_per_day, _lc.risk.max_analyses_per_ticker_per_day), (1, 1))
check("action cap parsed",
      make_loop_config(risk_extra={"max_actions_per_ticker_per_day": 4}).risk.max_actions_per_ticker_per_day, 4)
check_raises("analysis cap <= 0 raises",
             lambda: make_loop_config(risk_extra={"max_analyses_per_ticker_per_day": 0}), ConfigError)


# ===================== STRUCTURED SIZING via position_pct (D6) ==============
# Structured position_pct (% of equity) takes precedence over prose, tagged 'structured'.
_d, _src = signals.resolve_buy_dollars("ignored prose", 1000, 1.0, ceiling=500,
                                       remaining_daily_cap=500, buying_power=900, buffer=0,
                                       room_under_ticker_cap=900, position_pct=10)
check("structured pct used (10% of 1000)", (_d, _src), (100.0, "structured"))
_d, _ = signals.resolve_buy_dollars(None, 1000, 1.0, ceiling=50,
                                    remaining_daily_cap=500, buying_power=900, buffer=0,
                                    room_under_ticker_cap=900, position_pct=20)
check("structured pct still clamped by ceiling", _d, 50.0)
_d, _ = signals.resolve_buy_dollars(None, 1000, 0.5, ceiling=500,
                                    remaining_daily_cap=500, buying_power=900, buffer=0,
                                    room_under_ticker_cap=900, position_pct=10)
check("structured pct * overweight tilt", _d, 50.0)
_d, _src = signals.resolve_buy_dollars("5%", 1000, 1.0, ceiling=500,
                                       remaining_daily_cap=500, buying_power=900, buffer=0,
                                       room_under_ticker_cap=900, position_pct=None)
check("prose path unchanged when no position_pct", (_d, _src), (50.0, "parsed"))
_d, _src = signals.resolve_buy_dollars("5%", 1000, 1.0, ceiling=500,
                                       remaining_daily_cap=500, buying_power=900, buffer=0,
                                       room_under_ticker_cap=900, position_pct=0)
check("non-positive position_pct -> prose fallback", (_d, _src), (50.0, "parsed"))


# ================= PHASE 5: LIMIT ENTRIES + PROTECTIVE STOPS =================

# --- pure pricing (Python owns the numbers; model only seeds the stop) ---
check("marketable limit adds slippage", signals.marketable_limit_price(100.0, 0.3), 100.3)
check("marketable limit None on bad quote", signals.marketable_limit_price(0, 0.3), None)
check("whole shares floors", signals.whole_shares_for_dollars(100, 30.0), 3)
check("whole shares 0 when budget < 1 share", signals.whole_shares_for_dollars(25, 196.0), 0)
check("whole shares 0 on bad price", signals.whole_shares_for_dollars(100, 0), 0)
check("stop default from pct (8% below 100)", signals.resolve_stop_price(100.0, None, 8.0), 92.0)
check("stop uses model seed when in band", signals.resolve_stop_price(100.0, 95.0, 8.0), 95.0)
check("stop model too tight -> clamped to near (98)", signals.resolve_stop_price(100.0, 99.5, 8.0), 98.0)
check("stop model too wide -> clamped to far (84)", signals.resolve_stop_price(100.0, 50.0, 8.0), 84.0)
check("stop None on bad fill", signals.resolve_stop_price(0, 90, 8.0), None)
check_true("stop is strictly below fill", signals.resolve_stop_price(100.0, None, 8.0) < 100.0)


def make_order_config(order_block):
    d = {"account_number": "12345678", "dry_run": True, "kill_switch_file": "/tmp/bw_kill_test",
         "watchlist": ["AAPL"],
         "risk": {"max_dollars_per_trade": 25, "daily_loss_halt_pct": 5.0, "daily_capital_deploy_cap": 75,
                  "max_open_position_per_ticker": 50, "min_buying_power_buffer": 5},
         "deepseek": {"chat_model": "deepseek-v4-flash", "reasoner_model": "deepseek-v4-pro"}}
    if order_block is not None:
        d["order"] = order_block
    return load_config(_write_config(d))


# --- config: order types + protective stop ---
check("buy_type market default", make_order_config(None).buy_type, "market")
check("buy_type limit ok", make_order_config({"buy_type": "limit"}).buy_type, "limit")
check_raises("bad buy_type raises", lambda: make_order_config({"buy_type": "stop"}), ConfigError)
check("protective stop defaults off", make_order_config(None).protective_stop_enabled, False)
_oc = make_order_config({"protective_stop": {"enabled": True, "stop_pct": 8.0}})
check("protective stop parsed",
      (_oc.protective_stop_enabled, _oc.protective_stop_pct, _oc.protective_stop_tif), (True, 8.0, "gtc"))
check_raises("stop_pct out of (0,100) raises",
             lambda: make_order_config({"protective_stop": {"enabled": True, "stop_pct": 150}}), ConfigError)

# --- ledger: order-lifecycle columns + protective-stop tracking ---
_oled = _tmp_ledger()
ODAY = "2026-05-30"
_oled.reserve_order("entry1", ODAY, "AAPL", side="buy", type="limit", dollar_amount=None,
                    quantity=2, now_iso="t", order_kind="entry", limit_price=196.5)
_o = _oled.get_order("entry1")
check("order limit_price stored", _o["limit_price"], 196.5)
check("order_kind stored", _o["order_kind"], "entry")
check("order default state reserved", _o["state"], "reserved")
_oled.reserve_order("stop1", ODAY, "AAPL", side="sell", type="stop_market", dollar_amount=None,
                    quantity=2, now_iso="t", order_kind="protective_stop", stop_price=180.0,
                    parent_ref_id="entry1")
check("no open stops while merely reserved", _oled.open_protective_stops("AAPL"), [])
_oled.set_order_state("stop1", "stop_placed")
_stops = _oled.open_protective_stops("AAPL")
check("open stop found once placed", [s["ref_id"] for s in _stops], ["stop1"])
check("open stop carries parent + price",
      (_stops[0]["parent_ref_id"], _stops[0]["stop_price"]), ("entry1", 180.0))
_oled.set_order_state("stop1", "cancelled")
check("cancelled stop no longer open", _oled.open_protective_stops("AAPL"), [])


print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
