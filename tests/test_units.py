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
# Hard 48h backstop: neither an absurd model proposal nor a mis-set config ceiling
# can push the next re-check past 2880 minutes.
check("clamp model 168h -> 48h hard cap", signals.clamp_review_minutes(168, 30, 1440), 1440.0)  # config ceiling still binds first
check("clamp huge config ceiling -> 48h hard cap", signals.clamp_review_minutes(168, 30, 100000), 2880.0)
check("clamp None with huge ceiling -> 48h hard cap", signals.clamp_review_minutes(None, 30, 100000), 2880.0)
check("HARD_REVIEW_CEILING_MIN is 48h", signals.HARD_REVIEW_CEILING_MIN, 2880)

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


# ===================== RISK METRICS (lib/risk.py, pure) =====================
from lib import risk  # noqa: E402

# --- mean_stdev: small-N guards + ddof=1 hand value ---
check("mean_stdev empty", risk.mean_stdev([]), (None, None, 0))
_m, _s, _n = risk.mean_stdev([0.05])
check("mean_stdev n=1 -> (mean, None, 1)", (_m, _s, _n), (0.05, None, 1))
_m, _s, _n = risk.mean_stdev([0.10, 0.20])
check("mean_stdev mean", round(_m, 4), 0.15)
check("mean_stdev ddof=1 stdev", round(_s, 4), 0.0707)

# --- volatility ---
check("volatility zero-variance = 0", risk.volatility([0.1, 0.1, 0.1]).value, 0.0)
check("volatility n<2 -> None", risk.volatility([0.1]).value, None)

# --- sharpe: guards + hand value + explicit annualized field (D6) ---
check("sharpe n<2 -> None", risk.sharpe([0.1]).value, None)
check_true("sharpe n<2 note", "n<2" in risk.sharpe([0.1]).note)
check("sharpe zero-variance -> None", risk.sharpe([0.1, 0.1, 0.1]).value, None)
check_true("sharpe zero-variance note", "zero variance" in risk.sharpe([0.1, 0.1, 0.1]).note)
_sh = risk.sharpe([0.02, -0.01, 0.03])
check("sharpe hand value", round(_sh.value, 2), 0.64)
check("sharpe annualized None without periods", _sh.annualized, None)
_sha = risk.sharpe([0.02, -0.01, 0.03], periods_per_year=50)
check_true("sharpe annualized is an explicit field", _sha.annualized is not None)
check("sharpe annualized = value*sqrt(50)", round(_sha.annualized, 6), round(_sha.value * (50 ** 0.5), 6))
check_true("sharpe annualized labeled ESTIMATE in render", "ESTIMATE" in _sha.render())

# --- sortino: no-downside guard + hand value ---
check("sortino all-positive -> None", risk.sortino([0.01, 0.02, 0.03]).value, None)
check_true("sortino all-positive note", "no downside" in risk.sortino([0.01, 0.02, 0.03]).note)
_so = risk.sortino([0.02, -0.04, 0.01])
check("sortino hand value", round(_so.value, 4), round((-0.01 / 3) / 0.04, 4))

# --- max_drawdown: both kinds + guards ---
check("max_drawdown equity 0.25", round(risk.max_drawdown([100, 120, 90, 110], kind="equity").value, 4), 0.25)
check("max_drawdown cumret 0.20", round(risk.max_drawdown([0.10, -0.20, 0.05], kind="cumret").value, 4), 0.20)
check("max_drawdown empty -> None", risk.max_drawdown([], kind="equity").value, None)
check_raises("max_drawdown bad kind raises", lambda: risk.max_drawdown([1], kind="bogus"), ValueError)

# --- hit_rate: reuses is_hit, drops Hold ---
_hr = risk.hit_rate([("Buy", 0.02), ("Sell", -0.03), ("Hold", 0.05)])
check("hit_rate 2/2 = 1.0", _hr.value, 1.0)
check("hit_rate n excludes Hold", _hr.n, 2)
check("hit_rate empty -> None", risk.hit_rate([]).value, None)

# --- win_loss_stats: profit factor + no-loss guard ---
_wl = risk.win_loss_stats([0.02, -0.04, 0.01])
check("profit_factor 0.75", round(_wl["profit_factor"].value, 4), 0.75)
check("avg_win mean", round(_wl["avg_win"].value, 4), 0.015)
check("avg_loss mean", round(_wl["avg_loss"].value, 4), -0.04)
check("profit_factor None when no losses", risk.win_loss_stats([0.02, 0.01])["profit_factor"].value, None)
check_true("profit_factor no-loss note", "no losing" in risk.win_loss_stats([0.02, 0.01])["profit_factor"].note)

# --- low-confidence flag honors low_n; proof present in render ---
check("low_confidence True when n<low_n", risk.sharpe([0.02, -0.01, 0.03], low_n=5).low_confidence, True)
check("low_confidence False when n>=low_n", risk.sharpe([0.02, -0.01, 0.03], low_n=2).low_confidence, False)
check_true("render shows formula + inputs (proof)", "formula:" in _sh.render() and "inputs:" in _sh.render())

# --- rolling_window_trend: shrinks on thin history ---
_tr = risk.rolling_window_trend([0.01, -0.02, 0.03, 0.01],
                                [("Buy", 0.01), ("Buy", -0.02), ("Sell", -0.03), ("Buy", 0.01)], window=10)
check_true("trend shrinks window on thin history", _tr["window"] < 10 and "shrunk" in _tr["note"])

# --- derive_guidance: band boundaries + insufficient + disclaimer ---
_TH = risk.GuidanceThresholds(low_confidence_min_n=2, hit_rate_elevated=0.6, hit_rate_reduced=0.4,
                              sharpe_elevated=0.5, sharpe_reduced=0.0)


def _gmetrics(hv, hn, sv, sn):
    return {"hit_rate": risk.Metric("hit_rate", hv, hn, "f", "i", unit="pct"),
            "sharpe": risk.Metric("sharpe", sv, sn, "f", "i")}


check_true("guidance INSUFFICIENT below min_n",
           "INSUFFICIENT DATA" in risk.derive_guidance("AAPL", _gmetrics(0.5, 1, 0.3, 1), _TH).text)
check_true("guidance ELEVATED at boundary",
           "ELEVATED" in risk.derive_guidance("AAPL", _gmetrics(0.60, 5, 0.50, 5), _TH).tier)
check_true("guidance REDUCED at boundary",
           "REDUCED" in risk.derive_guidance("AAPL", _gmetrics(0.40, 5, 0.30, 5), _TH).tier)
check_true("guidance NORMAL in-band",
           "NORMAL" in risk.derive_guidance("AAPL", _gmetrics(0.50, 5, 0.30, 5), _TH).tier)
check_true("guidance always carries sizing disclaimer",
           "does NOT change position sizing" in risk.derive_guidance("AAPL", _gmetrics(0.5, 5, 0.3, 5), _TH).text)

# --- ledger read-only return series (oldest-first, non-null filter) ---
_sled = _tmp_ledger()
_sd1 = _sled.record_decision(trade_date="2026-05-20", ticker="AAPL",
                             decided_at="2026-05-20T10:00:00-04:00", signal="Buy", intent="buy",
                             decision_price=100.0)
_sd2 = _sled.record_decision(trade_date="2026-05-22", ticker="AAPL",
                             decided_at="2026-05-22T10:00:00-04:00", signal="Sell", intent="sell",
                             decision_price=110.0)
_sled.record_decision(trade_date="2026-05-21", ticker="MSFT",
                      decided_at="2026-05-21T10:00:00-04:00", signal="Buy", intent="buy",
                      decision_price=200.0)  # left unresolved -> excluded from series
_sled.record_outcome(_sd1, resolved_at="2026-05-27T10:00:00-04:00", directional_return=0.05)
_sled.record_outcome(_sd2, resolved_at="2026-05-28T10:00:00-04:00", directional_return=-0.02)
# _sd3 left unresolved -> excluded from the return series (non-null filter)
check("ticker_return_series oldest-first + non-null",
      [round(r["directional_return"], 4) for r in _sled.ticker_return_series("AAPL")], [0.05, -0.02])
check("ticker_return_series isolates ticker", len(_sled.ticker_return_series("MSFT")), 0)
check("all_return_series oldest-first across tickers (unresolved excluded)",
      [r["ticker"] for r in _sled.all_return_series()], ["AAPL", "AAPL"])
check("all_tickers_with_decisions distinct + sorted", _sled.all_tickers_with_decisions(), ["AAPL", "MSFT"])
_sled.get_or_create_baseline("2026-05-20", 100.0, "2026-05-20T09:30:00-04:00")
_sled.get_or_create_baseline("2026-05-21", 102.0, "2026-05-21T09:30:00-04:00")
check("baseline_equity_series date order",
      [r["baseline_equity"] for r in _sled.baseline_equity_series()], [100.0, 102.0])

# --- config: MemoryConfig parsing + validation (defaults ON; bad bands raise) ---
def make_memory_config(memory_block):
    d = {"account_number": "12345678", "dry_run": True, "kill_switch_file": "/tmp/bw_kill_test",
         "watchlist": ["AAPL"],
         "risk": {"max_dollars_per_trade": 25, "daily_loss_halt_pct": 5.0, "daily_capital_deploy_cap": 75,
                  "max_open_position_per_ticker": 50, "min_buying_power_buffer": 5},
         "deepseek": {"chat_model": "deepseek-v4-flash", "reasoner_model": "deepseek-v4-pro"}}
    if memory_block is not None:
        d["memory"] = memory_block
    return load_config(_write_config(d))


check("memory defaults enabled (opt-out)", make_memory_config(None).memory.enabled, True)
check("memory explicit false disables", make_memory_config({"enabled": False}).memory.enabled, False)
_mc = make_memory_config(None).memory
check("memory defaults (min_n, window)", (_mc.low_confidence_min_n, _mc.rolling_window), (5, 10))
_thr = _mc.thresholds()
check("thresholds carry conviction bands", (_thr.hit_rate_elevated, _thr.sharpe_reduced), (0.60, 0.0))
check_true("memory dir resolved absolute", Path(_mc.dir).is_absolute())
check_raises("inverted hit-rate band raises",
             lambda: make_memory_config({"conviction": {"hit_rate_elevated": 0.3, "hit_rate_reduced": 0.5}}),
             ConfigError)
check_raises("inverted sharpe band raises",
             lambda: make_memory_config({"conviction": {"sharpe_elevated": 0.1, "sharpe_reduced": 0.5}}),
             ConfigError)
check_raises("low_confidence_min_n <= 0 raises",
             lambda: make_memory_config({"low_confidence_min_n": 0}), ConfigError)
check_raises("hit-rate threshold out of [0,1] raises",
             lambda: make_memory_config({"conviction": {"hit_rate_elevated": 1.5}}), ConfigError)


# ================= REFLECTIVE MEMORY (lib/reflect_memory.py) ================
from lib import reflect_memory as rmem  # noqa: E402

_memcfg = make_memory_config({"low_confidence_min_n": 2, "rolling_window": 2}).memory
_rmled = _tmp_ledger()


def _seed(led, ticker, date, signal, dprice, ret=None):
    did = led.record_decision(trade_date=date, ticker=ticker, decided_at=date + "T10:00:00-04:00",
                              signal=signal, intent=("buy" if signal in ("Buy", "Overweight") else "sell"),
                              decision_price=dprice)
    if ret is not None:
        led.record_outcome(did, resolved_at=date + "T15:00:00-04:00", holding_days=5,
                           directional_return=ret)
    return did


_seed(_rmled, "AAPL", "2026-05-18", "Buy", 100.0, 0.04)
_seed(_rmled, "AAPL", "2026-05-19", "Sell", 104.0, -0.02)
_seed(_rmled, "AAPL", "2026-05-20", "Buy", 102.0, 0.01)
_seed(_rmled, "TSLA", "2026-05-20", "Buy", 200.0, 0.03)

_bundle = rmem.build_metric_bundle(_rmled, "AAPL", _memcfg)
check_true("bundle has ticker + portfolio parts", "ticker" in _bundle and "portfolio" in _bundle)
_block = rmem.render_metric_block(_bundle["ticker"])
check_true("metric block shows proof (formula+inputs)", "formula:" in _block and "inputs:" in _block)
check_true("metric block shows guidance + sizing disclaimer",
           "GUIDANCE" in _block and "does NOT change position sizing" in _block)

_tmpdir = Path(tempfile.mkdtemp())
rmem.write_decision_snapshot("AAPL", "2026-05-21", signal="Underweight", decision_price=101.0,
                             bundle=_bundle, base_dir=_tmpdir, now_label="2026-05-21T10:00")
_apath = _tmpdir / "tickers" / "AAPL.md"
check_true("ticker file written", _apath.exists())
_txt = _apath.read_text()
check_true("D5 parity: rendered block matches file exactly", _block in _txt)
check("one SNAP block after first write", _txt.count("<!-- SNAP date="), 1)
rmem.write_decision_snapshot("AAPL", "2026-05-21", signal="Hold", decision_price=101.5,
                             bundle=_bundle, base_dir=_tmpdir, now_label="2026-05-21T11:00")
_txt2 = _apath.read_text()
check("idempotent: same-date write stays one SNAP", _txt2.count("<!-- SNAP date=2026-05-21"), 1)
check_true("idempotent: latest content wins", "decision_price=101.5" in _txt2)
rmem.write_decision_snapshot("AAPL", "2026-05-22", signal="Buy", decision_price=103.0,
                             bundle=_bundle, base_dir=_tmpdir)
_txt3 = _apath.read_text()
check("two SNAP blocks after new date", _txt3.count("<!-- SNAP date="), 2)
check_true("newest snapshot first", _txt3.index("2026-05-22") < _txt3.index("2026-05-21"))
check_true("no leftover .tmp file", not (_tmpdir / "tickers" / "AAPL.md.tmp").exists())

_seed(_rmled, "AAPL", "2026-05-25", "Buy", 105.0, 0.05)
_summary = rmem.update_after_outcome(_rmled, {"AAPL"}, _memcfg, _tmpdir, now_label="2026-05-30T16:00")
check("update reports AAPL updated", _summary["tickers_updated"], ["AAPL"])
check_true("update wrote portfolio.md", (_tmpdir / "portfolio.md").exists())
_txt4 = _apath.read_text()
check_true("resolved-outcomes table present", "Resolved outcomes" in _txt4)
check_true("update preserved snapshot history",
           "<!-- SNAP date=2026-05-22" in _txt4 and "<!-- SNAP date=2026-05-21" in _txt4)

_tmpdir2 = Path(tempfile.mkdtemp())
_rb = rmem.rebuild_all(_rmled, _memcfg, _tmpdir2)
check("rebuild covers all tickers", sorted(_rb["tickers_rebuilt"]), ["AAPL", "TSLA"])
check_true("rebuild wrote AAPL + TSLA + portfolio",
           (_tmpdir2 / "tickers" / "AAPL.md").exists() and (_tmpdir2 / "tickers" / "TSLA.md").exists()
           and (_tmpdir2 / "portfolio.md").exists())

# READ path + ordered fallback (D3)
_cfg_full = make_memory_config({"low_confidence_min_n": 2})
_ctx = rmem.safe_build_context(_rmled, "AAPL", _cfg_full)
check("safe_build_context source enriched", _ctx.source, "enriched")
check_true("full context >= compact (more recent decisions)", len(_ctx.full) >= len(_ctx.compact))
check_true("enriched context carries the per-ticker block", "deterministic risk/return" in _ctx.full)
check_true("bundle returned for snapshot reuse", _ctx.bundle is not None)

# D3 CRITICAL regression: builder failure -> falls back to scorecard, NOT empty
_orig_build = rmem.build_metric_bundle


def _boom(*a, **k):
    raise RuntimeError("forced failure")


rmem.build_metric_bundle = _boom
_ctx_fb = rmem.safe_build_context(_rmled, "AAPL", _cfg_full)
check("D3 fallback source = scorecard", _ctx_fb.source, "scorecard")
check_true("D3 fallback still returns the proven scorecard (not empty)",
           "AAPL" in _ctx_fb.full and _ctx_fb.full != "")
check("D3 fallback bundle is None", _ctx_fb.bundle, None)

_orig_sc = memory.scorecard


def _boom2(*a, **k):
    raise RuntimeError("forced")


memory.scorecard = _boom2
_ctx_empty = rmem.safe_build_context(_rmled, "AAPL", _cfg_full)
check("D3 both-fail source = empty", _ctx_empty.source, "empty")
check("D3 both-fail returns ''", _ctx_empty.full, "")
rmem.build_metric_bundle = _orig_build
memory.scorecard = _orig_sc

_cfg_off = make_memory_config({"enabled": False})
_ctx_off = rmem.safe_build_context(_rmled, "AAPL", _cfg_off)
check("disabled -> source scorecard", _ctx_off.source, "scorecard")
check("disabled -> no bundle", _ctx_off.bundle, None)

_ctx_e = rmem.safe_build_context(_tmp_ledger(), "ZZZZ", _cfg_full)
check_true("empty-ledger safe_build_context never raises", isinstance(_ctx_e.full, str))


# --- boundary invariant: the sizing path never imports the analysis-only modules ---
import lib.signals as _sigmod  # noqa: E402
_sigsrc = Path(_sigmod.__file__).read_text(encoding="utf-8")
check_true("signals.py never imports risk/reflect_memory (use-case-1/2 wall)",
           "import risk" not in _sigsrc and "reflect_memory" not in _sigsrc)


# --- D7: offline stub-LLM prompt injection (block present iff context) ---
from tradingagents.agents.researchers.bull_researcher import create_bull_researcher  # noqa: E402
from tradingagents.agents.researchers.bear_researcher import create_bear_researcher  # noqa: E402
from tradingagents.agents.risk_mgmt.aggressive_debator import create_aggressive_debator  # noqa: E402
from tradingagents.agents.risk_mgmt.conservative_debator import create_conservative_debator  # noqa: E402
from tradingagents.agents.risk_mgmt.neutral_debator import create_neutral_debator  # noqa: E402


class _RecLLM:
    """Records the prompt it was handed; returns a canned response (no network)."""
    def __init__(self):
        self.prompt = None

    def invoke(self, prompt):
        self.prompt = prompt
        return type("_R", (), {"content": "ok"})()


def _node_state(ctx):
    debate = {"history": "", "bull_history": "", "bear_history": "", "current_response": "", "count": 0}
    risk_state = {"history": "", "aggressive_history": "", "conservative_history": "",
                  "neutral_history": "", "current_aggressive_response": "",
                  "current_conservative_response": "", "current_neutral_response": "",
                  "count": 0, "latest_speaker": ""}
    return {
        "investment_debate_state": debate, "risk_debate_state": risk_state,
        "market_report": "m", "sentiment_report": "s", "news_report": "n", "fundamentals_report": "f",
        "asset_type": "stock", "trader_investment_plan": "BUY plan", "company_of_interest": "AAPL",
        "past_context_compact": ctx, "past_context": ctx,
    }


_MARKER = "Deterministic track-record context"
for _name, _factory in {"bull": create_bull_researcher, "bear": create_bear_researcher,
                        "aggressive": create_aggressive_debator,
                        "conservative": create_conservative_debator,
                        "neutral": create_neutral_debator}.items():
    _llm = _RecLLM()
    _factory(_llm)(_node_state("PROOF-CONTEXT-XYZ"))
    check_true(f"D7 {_name}: block injected when context present",
               _MARKER in _llm.prompt and "PROOF-CONTEXT-XYZ" in _llm.prompt)
    _llm2 = _RecLLM()
    _factory(_llm2)(_node_state(""))
    check_true(f"D7 {_name}: no block when context empty", _MARKER not in _llm2.prompt)

import tradingagents.agents.managers.research_manager as _rmmod  # noqa: E402
check_true("D7 research_manager wires track_record_block",
           "track_record_block(state)" in Path(_rmmod.__file__).read_text(encoding="utf-8"))


# --- T1: digest renders account-equity risk line; content_hash excludes it ---
_m_ar = copy.deepcopy(MODEL)
_base_hash = notify.digest_hash(_m_ar)
_m_ar["account_risk"] = {"drawdown_pct": 12.5, "drawdown_proof": "peak-to-trough on equity levels",
                         "sharpe": 0.42, "sharpe_n": 7}
_dg_ar = notify.build_digest(_m_ar)
check_true("digest text shows account-risk line", "Account risk: max drawdown 12.5%" in _dg_ar["text"])
check_true("digest html shows account-risk line", "Account risk" in _dg_ar["html"])
check_true("account-risk Sharpe shown with N", "daily Sharpe 0.42 (N=7)" in _dg_ar["text"])
check("account_risk excluded from content_hash (no spurious re-sends)",
      notify.digest_hash(_m_ar), _base_hash)
check_true("digest with no account_risk omits the line",
           "Account risk" not in notify.build_digest(MODEL)["text"])


# ============================================================================
# Stage 0 — strategy layer (strategy.yaml + lib/strategy.py) + ledger tables.
# Pure + hermetic: the committed strategy.yaml, temp files, and a temp db only.
# ============================================================================
import dataclasses as _dc  # noqa: E402
import lib.strategy as _strat  # noqa: E402
from lib.config import RiskConfig as _RiskConfig  # noqa: E402

_REPO = Path(__file__).resolve().parent.parent
_sc = _strat.load_strategy(_REPO / "strategy.yaml")
_base_yaml = (_REPO / "strategy.yaml").read_text(encoding="utf-8")


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

# --- deterministic book selection (truth table) ---
check("strategy: HOLD->core", _strat.select_active_book(_sc, None)[0], "core_55_45")
check("strategy: DEPLOY dial-OFF stays core (fail-safe)",
      _strat.select_active_book(_sc, {"core_pce_pct": 2.3})[0], "core_55_45")
check("strategy: STANDDOWN pce->core", _strat.select_active_book(_sc, {"core_pce_pct": 3.6})[0], "core_55_45")
check("strategy: STANDDOWN hike->core", _strat.select_active_book(_sc, {"fed_hike": True})[0], "core_55_45")
check("strategy: regime HOLD", _strat.regime_label(_sc, None), "HOLD")
check("strategy: regime DEPLOY", _strat.regime_label(_sc, {"core_pce_pct": 2.3}), "DEPLOY")
check("strategy: regime STAND_DOWN", _strat.regime_label(_sc, {"core_pce_pct": 3.6}), "STAND_DOWN")
_dialon = _dc.replace(_sc, books={**_sc.books,
    "dial_up_63_37": _dc.replace(_sc.books["dial_up_63_37"], enabled=True)})
check("strategy: DEPLOY dial-ON routes to dial-up",
      _strat.select_active_book(_dialon, {"core_pce_pct": 2.3})[0], "dial_up_63_37")
check("strategy: sleeve_thesis known", _strat.sleeve_thesis(_sc, "smh")["sleeve"], "Semiconductors")
check("strategy: sleeve_thesis unknown -> None", _strat.sleeve_thesis(_sc, "FOO"), None)


# --- config fail-safe + rebalance-knob defaults ---
def _safe_strategy(path):  # mirrors the load_config wrapper: garbled -> None
    try:
        return _strat.load_strategy(path)
    except Exception:
        return None


check("config: garbled strategy.yaml -> None (fail-safe contract)",
      _safe_strategy(_write_tmp("schema: 2\nnonsense: [")), None)
_rc = _RiskConfig(max_dollars_per_trade=100, daily_loss_halt_pct=20, daily_capital_deploy_cap=1000,
                  max_open_position_per_ticker=50, min_buying_power_buffer=5,
                  max_actions_per_ticker_per_day=1, max_analyses_per_ticker_per_day=1)
check("config: RiskConfig rebalance knobs default to today's behavior",
      (_rc.rebalance_enabled, _rc.cash_sleeve_ticker, _rc.rebalance_drift_band_pct, _rc.watchlist_from_strategy),
      (False, "SGOV", 5.0, False))

# --- ledger: the 6 new tables round-trip (temp db; fresh + existing) ---
_db = tempfile.mktemp(suffix=".db")
Ledger(_db)
_led = Ledger(_db)  # re-open: idempotent auto-create on a pre-existing db
_gid = _led.set_strategy_goal(created_at="t", target_return_pct=15, horizon_months=12,
    benchmark="SGOV", benchmark_annual_pct=3.6, constraint_note="", macro_thesis_version="v1",
    macro_thesis_json="{}", active_book="core_55_45", as_of="d", start_date="d", start_equity=100.0)
_gid2 = _led.set_strategy_goal(created_at="t2", target_return_pct=15, horizon_months=12,
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
check("ledger: goal_tracking series len", len(_led.goal_tracking_series(_gid2)), 1)
_led.upsert_thesis_state(goal_id=_gid2, as_of="d", regime="neutral", active_book="core_55_45",
    last_trigger=None, last_macro_json="{}", updated_at="t")
_led.upsert_thesis_state(goal_id=_gid2, as_of="d2", regime="standdown", active_book="core_55_45",
    last_trigger="hike", last_macro_json="{}", updated_at="t2")
check("ledger: thesis_state upsert in place", _led.get_thesis_state(_gid2)["regime"], "standdown")
Path(_db).unlink(missing_ok=True)


# ============================================================================
# Stage 1 — pure portfolio/goal math + signals target-aware clamp helpers.
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

# --- signals: target-aware helpers + the BYTE-IDENTICAL clamp guarantee ---
check("sig: room_under_target", signals.room_under_target(9.0, 2.0), 7.0)
check("sig: room_under_target at/over -> 0", signals.room_under_target(9.0, 12.0), 0.0)
check("sig: target sell trim (excess/quote)", signals.resolve_target_sell_quantity(10.0, 5.0, 60.0, 45.0), 3.0)
check("sig: target sell full_exit -> all held", signals.resolve_target_sell_quantity(10.0, 5.0, 60.0, 45.0, full_exit=True), 10.0)
check("sig: target sell at/under target -> 0", signals.resolve_target_sell_quantity(10.0, 5.0, 40.0, 45.0), 0.0)
check("sig: target sell never oversells", signals.resolve_target_sell_quantity(10.0, 5.0, 1000.0, 0.0), 10.0)
check("sig: target sell no quote -> 0", signals.resolve_target_sell_quantity(10.0, 0.0, 60.0, 45.0), 0.0)
_buy_args = dict(position_sizing=None, baseline_equity=100.0, buy_fraction=1.0, ceiling=25.0,
                 remaining_daily_cap=75.0, buying_power=100.0, buffer=5.0,
                 room_under_ticker_cap=50.0, position_pct=10.0)
check("sig: resolve_buy_dollars room=None == omitted (byte-identical)",
      signals.resolve_buy_dollars(**_buy_args),
      signals.resolve_buy_dollars(**_buy_args, room_under_target=None))
check("sig: target room binds the buy (only ever reduces)",
      signals.resolve_buy_dollars(**_buy_args, room_under_target=3.0)[0], 3.0)


# ============================================================================
# Stage 2 — read-only goal/target context into the analysis path.
# ============================================================================
import lib.strategy_context as _sctx  # noqa: E402
import lib.reflect_memory as _rm  # noqa: E402

# Build a temp ledger with an active goal + targets + an equity baseline.
_s2db = tempfile.mktemp(suffix=".db")
_s2 = Ledger(_s2db)
_s2gid = _s2.set_strategy_goal(created_at="t", target_return_pct=15, horizon_months=12,
    benchmark="SGOV", benchmark_annual_pct=3.6, constraint_note="", macro_thesis_version="v",
    macro_thesis_json="{}", active_book="core_55_45", as_of="2026-06-13",
    start_date="2026-01-01", start_equity=100.0)
for _tk, _w in [("URA", 7), ("ETH", 2)]:
    _s2.upsert_target_holding(goal_id=_s2gid, sleeve=("Uranium/Power" if _tk == "URA" else "Crypto ETH"),
        ticker=_tk, target_weight=_w, band=2, status="active", book="core_55_45",
        quotable=True, proxy_ticker=None, updated_at="t")
_s2.get_or_create_baseline("2026-07-02", 110.0, "2026-07-02T00:00:00Z")  # ahead of glidepath

# build_target_context: inactive / not-in-book / present
check("sctx: strategy_cfg None -> None", _sctx.build_target_context(_s2, "URA", None), None)
check("sctx: ticker not in book -> None", _sctx.build_target_context(_s2, "NOPE", _sc), None)
_tc_ura = _sctx.build_target_context(_s2, "URA", _sc)
check("sctx: URA sleeve", _tc_ura["sleeve"], "Uranium/Power")
check("sctx: URA tier CORE (weight 7)", _tc_ura["tier"], "CORE")
check("sctx: URA goal regime AHEAD", _tc_ura["goal_regime"], "AHEAD")
check("sctx: ETH tier SATELLITE (weight 2)",
      _sctx.build_target_context(_s2, "ETH", _sc)["tier"], "SATELLITE")


# render: disclaimer present, and the D2 WALL — no digit, no '$'
def _no_digit_no_dollar(s):
    return ("$" not in s) and not any(ch.isdigit() for ch in s)


_full = _sctx.render_target_block(_tc_ura, compact=False)
_compact = _sctx.render_target_block(_tc_ura, compact=True)
check_true("sctx: full block carries the 'does NOT change sizing' disclaimer",
           "does NOT change sizing" in _full)
check_true("sctx: compact carries the disclaimer", "does NOT change sizing" in _compact)
check_true("sctx: full block names the sleeve + regime", "Uranium/Power" in _full and "AHEAD" in _full)
check_true("sctx: D2 wall — full block has NO digit and NO '$'", _no_digit_no_dollar(_full))
check_true("sctx: D2 wall — compact block has NO digit and NO '$'", _no_digit_no_dollar(_compact))
check("sctx: render None -> ''", _sctx.render_target_block(None), "")
check("sctx: render '' for ticker not in book",
      _sctx.render_target_block(_sctx.build_target_context(_s2, "NOPE", _sc)), "")

# D2 wall (source): the renderer reads NO broker/limit symbols. Word-boundary
# match so the legitimate ledger method active_target_portfolio (which contains
# the substring "get_portfolio") is not a false positive — the code calls a
# LEDGER method, never the broker's get_portfolio MCP tool.
import re as _re  # noqa: E402
_sctx_src = (_REPO / "lib" / "strategy_context.py").read_text(encoding="utf-8")
for _forbidden in ("buying_power", "max_dollars_per_trade", "get_portfolio",
                   "place_equity_order", "RiskConfig", "remaining_daily_cap",
                   "get_equity_positions", "get_equity_quotes"):
    check_true(f"sctx: source never reads '{_forbidden}' (analysis path reads no limits)",
               _re.search(rf"\b{_re.escape(_forbidden)}\b", _sctx_src) is None)

# D5: bundle['target'] is captured once and matches build_target_context
from lib.config import MemoryConfig as _MC  # noqa: E402
_memcfg = _MC(enabled=True, dir=tempfile.mkdtemp(), risk_free_rate=0.0, low_confidence_min_n=5,
              rolling_window=10, periods_per_year=50.0, hit_rate_elevated=0.6, hit_rate_reduced=0.4,
              sharpe_elevated=0.5, sharpe_reduced=0.0)
_bundle = _rm.build_metric_bundle(_s2, "URA", _memcfg, strategy_cfg=_sc)
check("rm: bundle['target'] captured once (D5) matches build_target_context",
      _bundle["target"], _tc_ura)
check("rm: bundle['target'] None when no strategy_cfg",
      _rm.build_metric_bundle(_s2, "URA", _memcfg)["target"], None)

# D3: a target-block failure degrades, never shrinks the scorecard/risk context
_bundle_bad = dict(_bundle)
_bundle_bad["target"] = {"tier": "CORE"}  # missing keys -> render_target_block raises -> swallowed
_ctx = _rm.build_past_context(_bundle_bad, _s2, "URA", compact=False)
check_true("rm: build_past_context never raises on a malformed target block",
           isinstance(_ctx, str))
Path(_s2db).unlink(missing_ok=True)

# analyze.py needs no change: it already threads cfg into safe_build_context
_analyze_src = (_REPO / "analyze.py").read_text(encoding="utf-8")
check_true("analyze.py threads cfg into safe_build_context (cfg.strategy reaches the renderer)",
           "safe_build_context(" in _analyze_src)


# ============================================================================
# Stage 3 — construct/strategy-set/goal-track + target-aware _run_plan.
# CRITICAL: _run_plan output is byte-identical when the feature is off.
# ============================================================================
import copy as _copy  # noqa: E402
import tick as _tick  # noqa: E402
import lib.market as _market  # noqa: E402


def _cfg_rebal(enabled, strategy_path=None):
    d = {"account_number": "12345678", "dry_run": True, "kill_switch_file": "/tmp/k_s3",
         "watchlist": ["SMH"],
         "risk": {"max_dollars_per_trade": 25, "daily_loss_halt_pct": 5.0,
                  "daily_capital_deploy_cap": 75, "max_open_position_per_ticker": 50,
                  "min_buying_power_buffer": 5, "rebalance_enabled": enabled},
         "deepseek": {"chat_model": "deepseek-v4-flash", "reasoner_model": "deepseek-v4-pro"}}
    if strategy_path:
        d["strategy_path"] = strategy_path
    return load_config(_write_config(d))


_p_base = {"run_id": "FIX", "now_iso": "2026-06-15T10:00:00-04:00", "equity": 100.0,
           "buying_power": 100.0, "positions": {}, "quotes": {"SMH": 50.0},
           "analyses": [{"ticker": "SMH", "signal": "Buy", "position_pct": 50.0}]}
_p_tw = _copy.deepcopy(_p_base)
_p_tw["target_weights"] = {"SMH": {"intent": "buy", "target_dollars": 9.0}}

# CRITICAL byte-identical regression: rebalance OFF ignores target_weights entirely
_out_base = _tick._run_plan(_cfg_rebal(False), _tmp_ledger(), _copy.deepcopy(_p_base))
_out_disabled = _tick._run_plan(_cfg_rebal(False), _tmp_ledger(), _copy.deepcopy(_p_tw))
check("plan: BYTE-IDENTICAL when rebalance OFF (target_weights ignored)", _out_base, _out_disabled)
check("plan: classic buy clamps to ceiling 25",
      next(o for o in _out_base["orders"] if o["ticker"] == "SMH")["dollar_amount"], 25.0)

# rebalance ON: a small target binds the buy (one more clamp, never widens)
_out_on = _tick._run_plan(_cfg_rebal(True), _tmp_ledger(), _copy.deepcopy(_p_tw))
check("plan: target room binds the buy (9 < ceiling 25)",
      next(o for o in _out_on["orders"] if o["ticker"] == "SMH")["dollar_amount"], 9.0)
_p_big = _copy.deepcopy(_p_base)
_p_big["target_weights"] = {"SMH": {"intent": "buy", "target_dollars": 1000.0}}
check("plan: target NEVER overrides the per-trade ceiling",
      next(o for o in _tick._run_plan(_cfg_rebal(True), _tmp_ledger(), _p_big)["orders"]
           if o["ticker"] == "SMH")["dollar_amount"], 25.0)

# HALT precedence: a daily-loss-halt day -> zero orders even with target_weights
_led_h = _tmp_ledger()
_led_h.get_or_create_baseline(_market.trading_day_et(), 100.0, "2026-06-15T09:00:00-04:00")
_p_halt = _copy.deepcopy(_p_tw)
_p_halt["equity"] = 50.0
_out_halt = _tick._run_plan(_cfg_rebal(True), _led_h, _p_halt)
check("plan: daily-loss halt fires", _out_halt["halt"], True)
check("plan: halt -> ZERO orders even with target_weights", _out_halt["orders"], [])

# trim / exit pass (held overweight name flagged by construct)
_p_trim = {"run_id": "FIX", "now_iso": "2026-06-15T10:00:00-04:00", "equity": 100.0,
           "buying_power": 100.0, "positions": {"XLV": {"quantity": 2.0, "market_value": 20.0}},
           "quotes": {"XLV": 10.0}, "analyses": [],
           "target_weights": {"XLV": {"intent": "trim", "target_dollars": 9.0}}}
_xlv = next(o for o in _tick._run_plan(_cfg_rebal(True), _tmp_ledger(), _copy.deepcopy(_p_trim))["orders"]
            if o["ticker"] == "XLV")
check("plan: rebalance trim order kind", _xlv["order_kind"], "rebalance_trim")
check("plan: rebalance trim qty = excess/quote", _xlv["quantity"], 1.1)  # (20-9)/10
_p_exit = _copy.deepcopy(_p_trim)
_p_exit["target_weights"] = {"XLV": {"intent": "exit", "target_dollars": 0.0}}
_xlv_x = next(o for o in _tick._run_plan(_cfg_rebal(True), _tmp_ledger(), _p_exit)["orders"]
             if o["ticker"] == "XLV")
check("plan: rebalance exit sells all held", _xlv_x["quantity"], 2.0)
check("plan: rebalance exit order kind", _xlv_x["order_kind"], "rebalance_exit")
check("plan: NO trim/exit when rebalance OFF",
      _tick._run_plan(_cfg_rebal(False), _tmp_ledger(), _copy.deepcopy(_p_trim))["orders"], [])

# strategy-set -> construct -> goal-track lifecycle (offline, real strategy.yaml)
_cfg_s3 = _cfg_rebal(True, strategy_path=str(_REPO / "strategy.yaml"))
_led_s3 = _tmp_ledger()
_ss = _tick._run_strategy_set(_cfg_s3, _led_s3, {"equity": 100.0, "now_iso": "2026-06-13T10:00:00-04:00"})
check_true("strategy-set: writes the core book holdings", _ss["holdings"] >= 10)
check("strategy-set: active book is core", _ss["active_book"], "core_55_45")
check_true("strategy-set: exactly one active goal", _led_s3.get_active_goal() is not None)
_con = _tick._run_construct(_cfg_s3, _led_s3, {"equity": 100.0, "positions": {}, "macro_reading": None})
check("construct: proceeds with an active goal", _con["proceed"], True)
check("construct: SMH underweight -> buy intent", _con["target_weights"]["SMH"]["intent"], "buy")
check("construct: SOL flagged unquotable (skipped)", _con["target_weights"]["SOL"]["intent"], "skip_unquotable")
check("construct: SGOV held as cash residual", _con["target_weights"]["SGOV"]["intent"], "cash_residual")
check("construct: no active goal -> proceed False",
      _tick._run_construct(_cfg_s3, _tmp_ledger(), {"equity": 100.0})["proceed"], False)
_led_s3.get_or_create_baseline("2026-09-01", 110.0, "2026-09-01T00:00:00-04:00")
_gt = _tick._run_goal_track(_cfg_s3, _led_s3)
check("goal-track: records a snapshot", _gt["recorded"], True)
check_true("goal-track: regime present", _gt.get("regime") in ("AHEAD", "ON-TRACK", "BEHIND"))


# ============================================================================
# Stage 4 — continual learning + add/remove engine (auto-propose, human-apply).
# ============================================================================
import lib.universe as _uni  # noqa: E402
import lib.learn as _learn  # noqa: E402
import lib.risk as _risk  # noqa: E402

# --- universe transitions ---
check("uni: validate_add allow-listed+quotable", _uni.validate_add("SMH", ["SMH"], ["SMH"])[0], True)
check("uni: validate_add off-list -> blocked", _uni.validate_add("ZZZ", ["SMH"], ["SMH"])[0], False)
check("uni: validate_add unquotable -> blocked", _uni.validate_add("SOL", ["SOL"], ["SMH"])[0], False)
_rows4 = [{"ticker": "SMH", "sleeve": "Semis", "target_weight": 9, "band": 4, "status": "active"},
          {"ticker": "SGOV", "sleeve": "Cash", "target_weight": 91, "band": 0, "status": "active"}]
_nr4, _freed4 = _uni.apply_remove(_rows4, "SMH")
check("uni: apply_remove frees the weight", _freed4, 9.0)
check("uni: apply_remove -> exiting", next(r for r in _nr4 if r["ticker"] == "SMH")["status"], "exiting")
check("uni: apply_remove zeroes the weight", next(r for r in _nr4 if r["ticker"] == "SMH")["target_weight"], 0.0)
_redist4 = _uni.redistribute_to_cash(_nr4, _freed4, "SGOV")
check("uni: redistribute conserves freed weight to cash",
      next(r for r in _redist4 if r["ticker"] == "SGOV")["target_weight"], 100.0)
check("uni: validate_book ok at ~100", _uni.validate_book(_redist4)[0], True)
check("uni: validate_book rejects bad sum",
      _uni.validate_book([{"ticker": "X", "sleeve": "S", "target_weight": 50, "band": 1, "status": "active"}])[0], False)

# --- risk learning helpers ---
check("risk: sustained_underperf insufficient (N<min) -> None",
      _risk.sustained_underperformance([-0.1, -0.1], min_n=5).value, None)
check("risk: sustained_underperf flags a sustained bad run",
      _risk.sustained_underperformance([-0.05] * 8, min_n=5).value, 1.0)
check("risk: sustained_underperf healthy -> 0", _risk.sustained_underperformance([0.05] * 8, min_n=5).value, 0.0)
check_true("risk: contribution_vs_thesis signed", _risk.contribution_vs_thesis(-0.1, 9.0).value < 0)

# --- learn classifier + gates (use the real LearningConfig from strategy.yaml) ---
_lc = _cfg_s3.strategy.learning
check("learn: insufficient -> KEEP (None)",
      _learn.classify_holding([-0.1, -0.1], 9.0, goal_behind=True, learning_cfg=_lc), None)
check("learn: underperf + behind glidepath -> REMOVE",
      _learn.classify_holding([-0.05] * 8, 9.0, goal_behind=True, learning_cfg=_lc).kind, "PROPOSE_REMOVE")
check("learn: underperf + NOT behind -> FLAG",
      _learn.classify_holding([-0.05] * 8, 9.0, goal_behind=False, learning_cfg=_lc).kind, "FLAG_UNDERPERFORM")
check("learn: healthy -> KEEP", _learn.classify_holding([0.05] * 8, 9.0, goal_behind=True, learning_cfg=_lc), None)
check("learn: content_hash stable across reason text",
      _learn.Proposal("PROPOSE_REMOVE", "SMH", "Semis", "universe", "x").content_hash(),
      _learn.Proposal("PROPOSE_REMOVE", "SMH", "Semis", "universe", "y").content_hash())

# --- wall: learn.py never reaches the sizing side or the broker ---
_learn_src = (_REPO / "lib" / "learn.py").read_text(encoding="utf-8")
check_true("learn: never imports lib.signals",
           "import lib.signals" not in _learn_src and "from lib.signals" not in _learn_src)
for _ff in ("place_equity_order", "buying_power", "max_dollars_per_trade", "get_portfolio"):
    check_true(f"learn: source never references '{_ff}'",
               _re.search(rf"\b{_re.escape(_ff)}\b", _learn_src) is None)

# --- lifecycle: build_proposals + universe-apply gating (seeded ledger) ---
def _seed_returns(led, ticker, returns):
    for i, r in enumerate(returns):
        did = led.record_decision(trade_date=f"2026-01-{(i % 27) + 1:02d}", ticker=ticker,
                                  decided_at="t", signal="Buy", intent="buy", decision_price=100.0)
        led.record_outcome(did, resolved_at="t", holding_days=5, directional_return=r,
                           benchmark_return=0.0, alpha=r, realized_pnl=None, unrealized_pnl=None,
                           scored_against="directional")


_led4 = _tmp_ledger()
_g4 = _led4.set_strategy_goal(created_at="t", target_return_pct=15, horizon_months=12, benchmark="SGOV",
    benchmark_annual_pct=3.6, constraint_note="", macro_thesis_version="v", macro_thesis_json="{}",
    active_book="core_55_45", as_of="2026-06-13", start_date="2026-01-01", start_equity=100.0)
for _tk4, _w4, _sl4 in [("SMH", 9, "Semis"), ("SGOV", 45, "Cash")]:
    _led4.upsert_target_holding(goal_id=_g4, sleeve=_sl4, ticker=_tk4, target_weight=_w4,
        band=(4 if _tk4 == "SMH" else 0), status="active", book="core_55_45",
        quotable=True, proxy_ticker=None, updated_at="t")
_seed_returns(_led4, "SMH", [-0.05] * 8)
_progress4 = {"regime": "BEHIND", "ahead_behind_pct": -10.0}
_ps4 = _learn.build_proposals(_led4, _g4, _lc, "HOLD", _progress4)
check_true("learn: build_proposals -> SMH REMOVE (underperf+behind)",
           any(p.kind == "PROPOSE_REMOVE" and p.ticker == "SMH" for p in _ps4["all"]))
check("learn: REMOVE is needs-approval by default (universe tier, auto off)", len(_ps4["auto_apply"]), 0)
check_true("learn: STAND_DOWN -> a DERISK proposal",
           any(p.kind == "PROPOSE_DERISK" for p in _learn.build_proposals(_led4, _g4, _lc, "STAND_DOWN", _progress4)["all"]))
_rid4 = _led4.record_universe_proposal(goal_id=_g4, proposed_at="2026-09-01T00:00:00Z",
    kind="PROPOSE_REMOVE", ticker="SMH", sleeve="Semis", from_book=None, to_book=None,
    target_weight=9.0, tier="universe", content_hash="hash1", reason="test", goal_gap_pct=-10.0)
check("ledger: re-proposing the same open change is deduped (None)",
      _led4.record_universe_proposal(goal_id=_g4, proposed_at="x", kind="PROPOSE_REMOVE", ticker="SMH",
          sleeve="Semis", from_book=None, to_book=None, target_weight=9.0, tier="universe",
          content_hash="hash1", reason="dup", goal_gap_pct=None), None)
check("universe-apply: universe tier without --approve -> NOT applied",
      _tick._run_universe_apply(_cfg_s3, _led4, change_id=_rid4, approve=False)["applied"], False)
check("universe-apply: --approve applies",
      _tick._run_universe_apply(_cfg_s3, _led4, change_id=_rid4, approve=True)["applied"], True)
check("universe-apply: REMOVE set the holding to exiting (winds to zero, no order)",
      next(r for r in _led4.active_target_portfolio(_g4, statuses=("active", "exiting"))
           if r["ticker"] == "SMH")["status"], "exiting")

# --- run_lock (Stage 5 mutex, exercised here) ---
_ledL = _tmp_ledger()
check("ledger: run_lock acquires when free", _ledL.try_acquire_run_lock("h1", "2026-06-13T10:00:00", ttl_seconds=3600), True)
check("ledger: run_lock blocks a second holder while held",
      _ledL.try_acquire_run_lock("h2", "2026-06-13T10:01:00", ttl_seconds=3600), False)
check("ledger: run_lock steals a STALE lock", _ledL.try_acquire_run_lock("h2", "2026-06-13T12:00:00", ttl_seconds=3600), True)


# ============================================================================
# Stage 5 — deploy harness: run-lock wrapper + order-authorization guard +
# offline healthcheck. (The Agent-SDK supervisor + IaC are exercised by the
# AWS e2e drills documented in docs/DEPLOY.md, not the offline suite.)
# ============================================================================
import lib.runlock as _runlock  # noqa: E402
import deploy.runner.order_guard as _og  # noqa: E402
import deploy.runner.healthcheck as _hc  # noqa: E402

# run-lock context manager
_ledRL = _tmp_ledger()
_raised = False
with _runlock.run_lock(_ledRL, "h1", "2026-06-13T10:00:00", ttl_seconds=3600):
    try:
        with _runlock.run_lock(_ledRL, "h2", "2026-06-13T10:00:30", ttl_seconds=3600):
            pass
    except _runlock.RunLockError:
        _raised = True
check("runlock: a second acquire while held raises", _raised, True)
with _runlock.run_lock(_ledRL, "h3", "2026-06-13T11:00:00", ttl_seconds=3600):
    pass
check("runlock: released after exit (lock free)", _ledRL.run_lock_state(), None)

# order guard: ledger-backed ref_id check (decision D5)
_ledG = _tmp_ledger()
_ledG.reserve_order("REF-OK", _market.trading_day_et(), "SMH", side="buy", type="market",
                    dollar_amount=9.0, quantity=None, now_iso="t")
check("guard: read-only tool -> allow", _og.evaluate("get_portfolio", {}, _ledG)[0], True)
check("guard: order with a RESERVED ref_id -> allow",
      _og.evaluate("place_equity_order", {"ref_id": "REF-OK"}, _ledG)[0], True)
check("guard: order with an UNRESERVED ref_id -> DENY",
      _og.evaluate("place_equity_order", {"ref_id": "REF-HALLUCINATED"}, _ledG)[0], False)
check("guard: order with NO ref_id -> DENY", _og.evaluate("place_equity_order", {}, _ledG)[0], False)
check("guard: mcp-prefixed order tool name handled",
      _og.evaluate("mcp__robinhood-trading__place_equity_order", {"ref_id": "REF-OK"}, _ledG)[0], True)
check("guard: cancel with an unreserved ref_id -> DENY",
      _og.evaluate("cancel_equity_order", {"ref_id": "NOPE"}, _ledG)[0], False)

# offline healthcheck (config + market + ledger schema + imports)
check("healthcheck: offline self-test green", _hc.run_healthcheck()["ok"], True)


print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
