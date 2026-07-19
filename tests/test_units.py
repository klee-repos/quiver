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
                                     buffer=200)
check("buy clamps to ceiling", (d, src), (500.0, "parsed"))

# ceiling lower than parsed size
d, _ = signals.resolve_buy_dollars("50%", 10000, 1.0, ceiling=300,
                                   remaining_daily_cap=1500, buying_power=5000,
                                   buffer=200)
check("ceiling wins", d, 300.0)

# buying power - buffer is the binding constraint
d, _ = signals.resolve_buy_dollars("50%", 10000, 1.0, ceiling=5000,
                                   remaining_daily_cap=9000, buying_power=450,
                                   buffer=200)
check("buying-power buffer binds", d, 250.0)

# Overweight tilt halves the parsed size
d, _ = signals.resolve_buy_dollars("4%", 10000, 0.5, ceiling=5000,
                                   remaining_daily_cap=9000, buying_power=9000,
                                   buffer=0)
check("overweight halves (4% of 10k = 400 -> 200)", d, 200.0)

# unparseable sizing -> conservative fallback (min(ceiling, 100)), never fails open
d, src = signals.resolve_buy_dollars("a small starter", 10000, 1.0, ceiling=500,
                                     remaining_daily_cap=1500, buying_power=5000,
                                     buffer=200)
check("fallback amount", d, 100.0)
check("fallback source tagged", src, "fallback")

# daily cap exhausted -> zero -> skip
d, _ = signals.resolve_buy_dollars("50%", 10000, 1.0, ceiling=5000,
                                   remaining_daily_cap=0, buying_power=9000,
                                   buffer=0)
check("daily cap exhausted -> 0", d, 0.0)

# --- resolve_sell_quantity ---
check("full close", signals.resolve_sell_quantity(3.0, 1.0), 3.0)
check("trim half", signals.resolve_sell_quantity(3.0, 0.5), 1.5)
check("never oversell", signals.resolve_sell_quantity(3.0, 2.0), 3.0)
check("nothing held", signals.resolve_sell_quantity(0.0, 1.0), 0.0)


# --- resolve_sell_quantity_min_notional (sub-$1 fractional sell guard) ---
# RH rejects any fractional order with notional < $1. A model trim on a thin
# position can land below that floor; we bump the qty UP to clear $1 (clamped to
# held), or skip cleanly when the WHOLE position is under the floor (dust).
_mn = signals.resolve_sell_quantity_min_notional
# trim already clears $1 -> unchanged
check("trim clears floor", _mn(2.0, 1.0, 140.0), (1.0, "trim"))
# trim under $1 but whole position >= $1 -> bumped to 1/quote, clamped to held
check("bumped to $1", _mn(0.02, 0.005, 140.0), (round(1.0 / 140.0, 6), "bumped_to_min"))
# bumped qty never exceeds held (long-only invariant): a $1 bump would need more
# than held -> capped at held. Here held=0.008 (=$1.12, whole >= $1) but the
# cheapest qty clearing $1 is 1/140=0.007143 (< held), so we bump to 0.007143.
check("bump clamped to held", _mn(0.008, 0.004, 140.0), (round(1.0 / 140.0, 6), "bumped_to_min"))
# whole position under $1 (dust) -> skip_dust, no error
check("dust whole pos <$1", _mn(0.004, 0.002, 140.0), (0.0, "skip_dust"))  # 0.004*140=$0.56
# no usable quote -> defensive skip (never synthesize a price to force a sell)
check("no quote -> skip", _mn(2.0, 1.0, None), (0.0, "skip_no_quote"))
check("zero quote -> skip", _mn(2.0, 1.0, 0.0), (0.0, "skip_no_quote"))
# nothing held / nothing to trim
check("nothing held", _mn(0.0, 1.0, 140.0), (0.0, "skip_nothing"))
check("nothing to trim", _mn(2.0, 0.0, 140.0), (0.0, "skip_nothing"))
# custom floor ($2) respected
check("custom $2 floor", _mn(0.02, 0.005, 140.0, min_notional=2.0),
      (round(2.0 / 140.0, 6), "bumped_to_min"))
# held just over the floor: whole pos >= $1 so not dust; bump to the cheapest qty
# clearing $1 (1/140=0.007143), which is < held (0.0072) -> a sliver stays.
check("bump leaves sliver", _mn(0.0072, 0.003, 140.0),
      (round(1.0 / 140.0, 6), "bumped_to_min"))  # 0.007143*140=$1.00; 0.0072-0.007143 dust stays

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
      _mn(0.02, 0.005, 140.0, min_notional=1.0, bump_to_min=True),
      (round(1.0 / 140.0, 6), "bumped_to_min"))
# default (bump_to_min omitted) is the pre-F2 bump behavior (back-compat).
check("F2 default bump_to_min=True (back-compat)",
      _mn(0.02, 0.005, 140.0), (round(1.0 / 140.0, 6), "bumped_to_min"))



# ============================ EMAIL DIGEST ==================================
import copy  # noqa: E402


def _write_config(d: dict) -> str:
    tmp = tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False, encoding="utf-8")
    yaml.safe_dump(d, tmp)
    tmp.close()
    return tmp.name


def make_config(notify_block=None):
    import os as _os  # env-isolate: ambient NOTIFY_TO/NOTIFY_ALERTS_TO take precedence
    # Snapshot + restore so this isolation does not LEAK to later tests: the offline
    # healthcheck (and any test that loads the real config.yaml with notify.enabled:true)
    # resolves notify.to from NOTIFY_TO, so a permanent pop here would break it.
    _saved = {_k: _os.environ.pop(_k) for _k in ("NOTIFY_TO", "NOTIFY_ALERTS_TO")
              if _k in _os.environ}
    d = {
        "account_number": "12345678",
        "dry_run": True,
        "kill_switch_file": "/tmp/bw_kill_test",
        "risk": {"max_dollars_per_trade": 25, "daily_loss_halt_pct": 5.0,
                 "daily_capital_deploy_cap": 75,
                 "min_buying_power_buffer": 5},
        "glm": {"chat_model": "glm-5.2", "reasoner_model": "glm-5.2"},
    }
    if notify_block is not None:
        d["notify"] = notify_block
    try:
        return load_config(_write_config(d))
    finally:
        _os.environ.update(_saved)


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
# stage-keyed dedup: same (date, kind) different stage -> independent rows (no clobber)
_led.mark_notified(DAY, "error", "errplan", "a@b.com", "t", stage="plan")
_led.mark_notified(DAY, "error", "errprune", "a@b.com", "t", stage="prune")
check("notify rows isolated by stage (plan)", _led.last_notified_hash(DAY, "error", "plan"), "errplan")
check("notify rows isolated by stage (prune)", _led.last_notified_hash(DAY, "error", "prune"), "errprune")
check("notify stage default '' isolated", _led.last_notified_hash(DAY, "error"), None)

# ============================ ALERT EMAIL KINDS ============================
# The new `error` kind + the canonical stages + the cross-sender-stable alert hash.
_m_err = {"date": "2026-05-30", "now_iso": "t", "kind": "error", "severity": "critical",
          "stage": "plan", "dry_run": False, "event_detail": "daily cap math blew up",
          "subject_prefix": "[Quiver]", "tickers": []}
_de = notify.build_digest(_m_err)
check_true("error subject names the stage", "TICK FAILED" in _de["subject"] and "plan" in _de["subject"])
check_true("error body has a 'what to do' block", "What to do" in _de["html"] and "What to do" in _de["text"])
check_true("error html escapes + shows detail", "daily cap math" in _de["text"])
# auth_error carries actionable recovery in BOTH html and text (parity — locked-screen text fallback)
_m_a2 = {"date": "2026-05-30", "now_iso": "t", "kind": "auth_error", "dry_run": False,
         "event_detail": "401", "subject_prefix": "[Quiver]", "tickers": []}
_da2 = notify.build_digest(_m_a2)
check_true("auth_error text has /mcp + remote desktop",
           "/mcp" in _da2["text"] and "remotedesktop.google.com" in _da2["text"])
check_true("auth_error html has /mcp + remote desktop",
           "/mcp" in _da2["html"] and "remotedesktop.google.com" in _da2["html"])
check("auth_error stage derived", notify.stage_of(_m_a2), "broker_auth")
check("halt stage derived", notify.stage_of({"kind": "halt"}), "daily_loss_halt")
# warning severity: no alarmist CTA, self-heal copy
_m_warn = {"date": "2026-05-30", "now_iso": "t", "kind": "error", "severity": "warning",
           "stage": "prune", "dry_run": True, "event_detail": "blip", "subject_prefix": "[Quiver]",
           "tickers": []}
check("warning -> no action steps", notify.action_steps(_m_warn), [])
check_true("warning subject says hiccup", "hiccup" in notify.build_digest(_m_warn)["subject"])
# halt alert PRODUCTION shape (kind set; halted/halt_reason UNSET, as run_tick.py builds it):
# the rm-KILL recovery must appear in BOTH html AND text (the text is the lock-screen
# fallback for the single most critical alert) — regression guard for the parity bug.
_m_halt_alert = {"date": "d", "now_iso": "t", "kind": "halt", "dry_run": False,
                 "subject_prefix": "[Quiver]", "event_detail": "KILL written", "tickers": []}
_dha = notify.build_digest(_m_halt_alert)
check_true("halt alert text carries rm KILL recovery", "rm /opt/quiver/KILL" in _dha["text"])
check_true("halt alert html carries rm KILL recovery", "rm /opt/quiver/KILL" in _dha["html"])
check_true("halt alert text is not the misleading 'Halt: none'", "Halt: none" not in _dha["text"])
# non-finite equity/pct must fall back to em-dash (corrupt broker read shouldn't print +nan%)
check("nan money -> em-dash", notify._fmt_money(float("nan")), "—")
check("inf pct -> em-dash", notify._fmt_pct(float("inf")), "—")
check_true("nan pnl pill -> em-dash", "—" in notify._pnl_pill(float("nan")))
# alert hash: cross-sender stable (ignores event_detail/equity/time), keyed on (kind, stage, severity, dry_run)
_he = notify.digest_hash(_m_err)
check("alert hash ignores event_detail (cross-sender stable)",
      notify.digest_hash({**_m_err, "event_detail": "totally different text", "equity": 9}), _he)
check_true("alert hash changes with stage", notify.digest_hash({**_m_err, "stage": "commit"}) != _he)
check_true("alert hash changes with kind", notify.digest_hash({**_m_err, "kind": "auth_error"}) != _he)
check_true("alert hash changes with dry_run", notify.digest_hash({**_m_err, "dry_run": True}) != _he)
# digest hash is unchanged by the refactor (the 169-test skeleton invariant holds — re-pin)
check("digest hash still skeleton-keyed (ignores stage field)",
      notify.digest_hash({**MODEL, "stage": "whatever"}), notify.digest_hash(MODEL))

# --- notify config: per-event toggles + alerts_to ---
_cfg_def = make_config({"enabled": True, "to": ["a@b.com"], "from": "x@y.com"})
check("on_complete defaults ON", _cfg_def.notify.on_complete, True)
check("on_error defaults ON", _cfg_def.notify.on_error, True)
check("on_warning defaults OFF", _cfg_def.notify.on_warning, False)
check("alerts_to falls back to `to`", _cfg_def.notify.alerts_to, ["a@b.com"])
check("on_warning explicit true parsed",
      make_config({"enabled": True, "to": ["a@b.com"], "on_warning": True}).notify.on_warning, True)
check("on_complete fail-safe (only explicit false disables)",
      make_config({"enabled": True, "to": ["a@b.com"], "on_complete": "nope"}).notify.on_complete, True)
check("alerts_to override parsed",
      make_config({"enabled": True, "to": ["a@b.com"], "alerts_to": ["p@b.com"]}).notify.alerts_to, ["p@b.com"])
check_raises("on_error with explicit-empty alerts_to raises",
             lambda: make_config({"enabled": True, "to": ["a@b.com"], "alerts_to": []}), ConfigError)

# --- design system: email-client-safety invariants (shared across all kinds) ---
for _k, _extra in (("digest", {"tickers": MODEL["tickers"]}),
                   ("auth_error", {"event_detail": "401", "tickers": []}),
                   ("halt", {"halted": True, "halt_reason": "-6%", "tickers": []}),
                   ("error", {"severity": "critical", "stage": "plan", "tickers": []})):
    _mk = {"date": "2026-05-30", "now_iso": "t", "kind": _k, "dry_run": True,
           "subject_prefix": "[Quiver]", "equity": 100.0, "baseline_equity": 100.0,
           "drop_pct": 0.0, **_extra}
    _hk = notify.build_digest(_mk)["html"]
    check_true(f"{_k}: full email document (doctype+html+head)",
               _hk.startswith("<!DOCTYPE html>") and "<html lang=\"en\"" in _hk and "<head>" in _hk)
    check_true(f"{_k}: head carries charset+viewport+color-scheme",
               "charset=\"utf-8\"" in _hk and "width=device-width" in _hk and "color-scheme" in _hk)
    check_true(f"{_k}: Outlook-safe table layout (role=presentation, 640 cap, MSO ghost)",
               "role=\"presentation\"" in _hk and "max-width:640px" in _hk and "PixelsPerInch" in _hk)
    check_true(f"{_k}: hidden preheader present", "display:none" in _hk and "mso-hide:all" in _hk)
    check_true(f"{_k}: shared brand chrome (Quiver header + footer)",
               ">Quiver<" in _hk and "autonomous trading" in _hk)
# alerts draw a colored status circle (not an emoji we depend on) + a severity word
_ha = notify.build_digest({"date": "d", "now_iso": "t", "kind": "auth_error",
                           "event_detail": "401", "subject_prefix": "[Quiver]", "tickers": []})["html"]
check_true("alert draws a status circle (border-radius cell)", "border-radius:20px" in _ha)
check_true("alert carries a non-color severity word", "CRITICAL" in _ha)
# P&L pill is never color-only: it carries an explicit sign + arrow
_hp = notify.build_digest({"date": "d", "now_iso": "t", "kind": "digest", "dry_run": False,
                           "subject_prefix": "[Quiver]", "equity": 110.0, "baseline_equity": 100.0,
                           "drop_pct": 10.0, "tickers": []})["html"]
check_true("gain pill carries ▲ + sign (colorblind-safe)", "▲ +10.00%" in _hp)
_hn = notify.build_digest({"date": "d", "now_iso": "t", "kind": "digest", "dry_run": False,
                           "subject_prefix": "[Quiver]", "equity": 90.0, "baseline_equity": 100.0,
                           "drop_pct": -10.0, "tickers": []})["html"]
check_true("loss pill carries ▼ + sign (colorblind-safe)", "▼ -10.00%" in _hn)
# digest subject front-loads the money (P&L before the buy/sell counts)
check_true("digest subject leads with P&L",
           notify.build_digest(MODEL)["subject"].index("%") < notify.build_digest(MODEL)["subject"].index("buy"))

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
        "risk": {"max_dollars_per_trade": 25, "daily_loss_halt_pct": 5.0,
                 "daily_capital_deploy_cap": 75,
                 "min_buying_power_buffer": 5},
        "glm": {"chat_model": "glm-5.2", "reasoner_model": "glm-5.2"},
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

# realized P&L is a NAME-level figure the orchestrator copies onto EVERY pending decision
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

# --- T8: cross-sectional sliced scorecard ("what KIND of call wins") --------
_sliced_rows = [
    # momentum_breakout: 3 calls, 2 hits (alpha-graded)
    {"signal": "Buy", "intent": "buy", "basis": "momentum_breakout", "alpha": 0.03},
    {"signal": "Buy", "intent": "buy", "basis": "momentum_breakout", "alpha": 0.02},
    {"signal": "Buy", "intent": "buy", "basis": "momentum_breakout", "alpha": -0.04},
    # mean_reversion: 3 calls, 0 hits (a losing thesis category)
    {"signal": "Buy", "intent": "buy", "basis": "mean_reversion", "alpha": -0.02},
    {"signal": "Buy", "intent": "buy", "basis": "mean_reversion", "alpha": -0.05},
    {"signal": "Buy", "intent": "buy", "basis": "mean_reversion", "alpha": -0.01},
    # a skip must NOT pollute the slice
    {"signal": "Underweight", "intent": "skip", "basis": "momentum_breakout", "alpha": 0.20},
    # a thin basis (1 call) is omitted (< min_n)
    {"signal": "Buy", "intent": "buy", "basis": "rare_thesis", "alpha": 0.09},
]
_sliced = memory.build_sliced_scorecard(_sliced_rows, "basis", "strategy basis", min_n=3)
check_true("sliced: momentum_breakout shows 2/3 (67%)", "momentum_breakout: 2/3 (67%)" in _sliced)
check_true("sliced: mean_reversion shows 0/3 (0%)", "mean_reversion: 0/3 (0%)" in _sliced)
check_true("sliced: thin basis (<min_n) omitted", "rare_thesis" not in _sliced)
check_true("sliced: skip excluded from the slice (no +20% pollution)", "+20" not in _sliced)
check("sliced: empty rows -> ''", memory.build_sliced_scorecard([], "basis", "x"), "")

# --- T1: ledger return-series excludes skips + carries alpha/intent ---------
_skled = _tmp_ledger()
_sk_buy = _skled.record_decision(trade_date="2026-06-01", ticker="NVDA",
                                 decided_at="2026-06-01T10:00:00-04:00", signal="Buy", intent="buy",
                                 decision_price=100.0)
_sk_skip = _skled.record_decision(trade_date="2026-06-02", ticker="NVDA",
                                  decided_at="2026-06-02T10:00:00-04:00", signal="Underweight",
                                  intent="skip", decision_price=100.0)
_skled.record_outcome(_sk_buy, resolved_at="2026-06-06T10:00:00-04:00", holding_days=5,
                      directional_return=0.04, alpha=0.01)
_skled.record_outcome(_sk_skip, resolved_at="2026-06-07T10:00:00-04:00", holding_days=5,
                      directional_return=0.09)
_ser = _skled.ticker_return_series("NVDA")
check("return-series excludes skip rows", [r["id"] for r in _ser], [_sk_buy])
check("return-series carries alpha", _ser[0]["alpha"], 0.01)
check("return-series can opt back into skips", len(_skled.ticker_return_series("NVDA", exclude_skips=False)), 2)

# --- T3: conviction primitive (parse + persist) ----------------------------
# (F8: the PortfolioDecision/render_pm_decision schema lived in tradingagents/,
# now deleted. The same parse/extract/persist semantics are covered here against
# a markdown fixture — the EVE brain emits the same **Label**: value lines.)
import analyze as _analyze  # noqa: E402
_pmd = "**Rating**: Buy\n**Conviction**: 82\n**Uncertainty**: 15\nExecutive summary."
check("parse conviction from PM markdown", _analyze.parse_pm_field_float(_pmd, "Conviction"), 82.0)
check("parse uncertainty from PM markdown", _analyze.parse_pm_field_float(_pmd, "Uncertainty"), 15.0)
_ef = _analyze.extract_fields({"trader_investment_plan": "**Action**: Buy", "final_trade_decision": _pmd}, "Buy", "SMH")
check("extract_fields surfaces conviction", _ef["conviction"], 82.0)
check("extract_fields surfaces uncertainty", _ef["uncertainty"], 15.0)
# conviction omitted when the label is absent (model gave none)
check("conviction None when label absent", _analyze.parse_pm_field_float("no labels here", "Conviction"), None)
# persistence + migration round-trip
_cvled = _tmp_ledger()
_cvid = _cvled.record_decision(trade_date="2026-06-20", ticker="SMH",
                               decided_at="2026-06-20T10:00:00-04:00", signal="Buy", intent="buy",
                               conviction=82.0, uncertainty=15.0, data_quality='{"bars_n":250}')
_cvrow = _cvled.get_decision(_cvid)
check("ledger persists conviction", _cvrow["conviction"], 82.0)
check("ledger persists uncertainty", _cvrow["uncertainty"], 15.0)
check("ledger persists data_quality json", _cvrow["data_quality"], '{"bars_n":250}')


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
        "risk": {"max_dollars_per_trade": 25, "daily_loss_halt_pct": 5.0,
                 "daily_capital_deploy_cap": 75,
                 "min_buying_power_buffer": 5},
        "glm": {"chat_model": "glm-5.2", "reasoner_model": "glm-5.2"},
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

# --- config: F2/F3 anti-churn knobs parse + validate (defaults when absent; reject negative) ---
check("config: min_trade_notional defaults 5 when absent", make_loop_config().risk.min_trade_notional, 5.0)
check("config: hysteresis defaults 2 when absent",
      make_loop_config().risk.conviction_rebalance_min_delta_pct, 2.0)
check("config: min_trade_notional parses explicit value",
      make_loop_config(risk_extra={"min_trade_notional": 10}).risk.min_trade_notional, 10.0)
check("config: hysteresis parses explicit value",
      make_loop_config(risk_extra={"conviction_rebalance_min_delta_pct": 3.5}).risk.conviction_rebalance_min_delta_pct, 3.5)
check("config: min_trade_notional 0 disables (falls back to RH $1 at call sites)",
      make_loop_config(risk_extra={"min_trade_notional": 0}).risk.min_trade_notional, 0.0)
check_raises("config: negative min_trade_notional raises",
             lambda: make_loop_config(risk_extra={"min_trade_notional": -1}), ConfigError)
check_raises("config: negative hysteresis raises",
             lambda: make_loop_config(risk_extra={"conviction_rebalance_min_delta_pct": -1}), ConfigError)
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
                                       position_pct=10)
check("structured pct used (10% of 1000)", (_d, _src), (100.0, "structured"))
_d, _ = signals.resolve_buy_dollars(None, 1000, 1.0, ceiling=50,
                                    remaining_daily_cap=500, buying_power=900, buffer=0,
                                    position_pct=20)
check("structured pct still clamped by ceiling", _d, 50.0)
_d, _ = signals.resolve_buy_dollars(None, 1000, 0.5, ceiling=500,
                                    remaining_daily_cap=500, buying_power=900, buffer=0,
                                    position_pct=10)
check("structured pct * overweight tilt", _d, 50.0)
_d, _src = signals.resolve_buy_dollars("5%", 1000, 1.0, ceiling=500,
                                       remaining_daily_cap=500, buying_power=900, buffer=0,
                                       position_pct=None)
check("prose path unchanged when no position_pct", (_d, _src), (50.0, "parsed"))
_d, _src = signals.resolve_buy_dollars("5%", 1000, 1.0, ceiling=500,
                                       remaining_daily_cap=500, buying_power=900, buffer=0,
                                       position_pct=0)
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
         "risk": {"max_dollars_per_trade": 25, "daily_loss_halt_pct": 5.0, "daily_capital_deploy_cap": 75,
                  "min_buying_power_buffer": 5},
         "glm": {"chat_model": "glm-5.2", "reasoner_model": "glm-5.2"}}
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
         "risk": {"max_dollars_per_trade": 25, "daily_loss_halt_pct": 5.0, "daily_capital_deploy_cap": 75,
                  "min_buying_power_buffer": 5},
         "glm": {"chat_model": "glm-5.2", "reasoner_model": "glm-5.2"}}
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

# --- THE DECISION WALL (enumerated over lib/, not hand-listed) ---------------
# THE INVARIANT: every module under lib/ sits on the ANALYSIS side of the wall —
# it must not IMPORT the limit/broker-carrying modules (Mechanism A: AST over the
# real import nodes, because these modules describe the wall in their docstrings
# and a text scan would false-positive on "never imports lib.risk"), and must not
# REFERENCE a broker call or a trading limit (Mechanism B: word-boundary source
# scan, so the ledger's active_target_portfolio is not read as get_portfolio).
#
# The roster is ENUMERATED FROM THE FILESYSTEM. This is the whole point: a NEW
# module is INSIDE the wall by default and must be exempted ON PURPOSE below.
# (The roster used to be a hand-maintained tuple, which silently left every
# module nobody remembered to add — i.e. every new one — outside the wall.)
#
# Binding-side modules opt out via _WALL_GRANTS, and a grant is ITEMIZED rather
# than a blanket pass: each names the exact wall items it may use, so an exempt
# module stays walled against everything it was NOT granted. Three meta-checks
# keep roster+grants honest: the roster can't be empty (a bad cwd would make
# every check below pass vacuously), a grant can't name a module that no longer
# exists, and a grant can't be dead weight.
import ast as _ast  # noqa: E402
import re as _re  # noqa: E402  (used by the source-scans below + later blocks)

_REPO = Path(__file__).resolve().parent.parent
_WALL_IMPORTS = {"lib.risk", "lib.signals", "lib.config", "lib.levers"}
_WALL_SYMBOLS = ("buying_power", "max_dollars_per_trade", "get_portfolio",
                 "place_equity_order", "get_equity_positions", "get_equity_quotes",
                 "RiskConfig", "remaining_daily_cap")

# The ONLY binding-side opt-outs. Value = the exact wall items that module may
# reach for; every other item still applies to it.
_WALL_GRANTS = {
    # Owns the limit values themselves — config IS the source of the dollar caps.
    "lib/config.py":      {"lib.risk", "RiskConfig", "max_dollars_per_trade"},
    # The limits / underperformance module: the binding side's own logic.
    "lib/risk.py":        {"lib.signals"},
    # The sizing/clamp engine: reads buying_power off a PASSED-IN snapshot dict
    # (never the broker) and owns the daily-cap arithmetic.
    "lib/signals.py":     {"buying_power", "remaining_daily_cap"},
    # Offline wall REPLAY: its whole job is to re-run the deterministic wall over
    # history, so it imports the wall and synthesizes a simulated broker snapshot.
    "lib/wall_replay.py": {"lib.signals", "buying_power"},
    # Continual-learning scorer: may read risk's underperformance helpers
    # (reflect_memory's side of the wall) and NOTHING else. Narrow on purpose —
    # a blanket exemption would drop its never-imports-lib.signals guarantee.
    "lib/learn.py":       {"lib.risk"},
}


def _imported_modules(src: str) -> set:
    mods = set()
    for _node in _ast.walk(_ast.parse(src)):
        if isinstance(_node, _ast.Import):
            mods.update(a.name for a in _node.names)
        elif isinstance(_node, _ast.ImportFrom) and _node.module:
            mods.add(_node.module)
    return mods


def _wall_violations(src: str) -> set:
    """Every wall item a module actually reaches for (imports + limit/broker symbols)."""
    return ((_imported_modules(src) & _WALL_IMPORTS)
            | {s for s in _WALL_SYMBOLS if _re.search(rf"\b{_re.escape(s)}\b", src)})


_WALL_ROSTER = sorted(p.relative_to(_REPO).as_posix() for p in _REPO.glob("lib/**/*.py"))
# Meta-guard: an empty/short roster would make every wall check below pass VACUOUSLY.
check_true(f"wall: roster enumerates lib/ ({len(_WALL_ROSTER)} found, want >= 40)",
           len(_WALL_ROSTER) >= 40)
# Meta-guard: a grant naming a module that no longer exists is a stale exemption
# sitting there pretending to protect something.
for _g in sorted(_WALL_GRANTS):
    check_true(f"wall: grant target {_g} still exists (no stale exemption)", _g in _WALL_ROSTER)

for _mod in _WALL_ROSTER:
    _src = (_REPO / _mod).read_text(encoding="utf-8")
    _granted = _WALL_GRANTS.get(_mod, set())
    _reached = _wall_violations(_src)
    _bad = _reached - _granted
    check_true(f"{_mod}: reaches no ungranted limit/broker item (decision wall)"
               + (f" -> {sorted(_bad)}; it is analysis-side, or add an itemized grant" if _bad else ""),
               not _bad)
    # Meta-guard: a grant the module no longer needs must be REMOVED, not left to
    # quietly widen the opt-out surface.
    _dead = _granted - _reached
    check_true(f"{_mod}: every wall grant is load-bearing"
               + (f" -> drop {sorted(_dead)} from _WALL_GRANTS" if _dead else ""), not _dead)

# REVERSE wall: the self-learning levers module must NOT import the sizing/gate
# modules (a lever can't call sizing or the consistency gate; it only records
# evaluation inputs). lib.levers owns its own SQLite writes; EVE never touches
# SQLite.
_lev_src = (Path(__file__).resolve().parent.parent / "lib" / "levers.py").read_text(encoding="utf-8")
_lev_bad = _imported_modules(_lev_src) & {"lib.signals", "lib.risk", "lib.config"}
check_true("lib/levers.py: imports none of lib.signals/risk/config (reverse wall)", not _lev_bad)
check_true("lib/levers.py: never imports sqlite3's broker/limits (pure registry)",
           "tradingagents" not in _lev_src and "broker" not in _lev_src.lower())


# --- D7: EVE brain past_context injection (block present iff context) -------
# (F8: the legacy bull/bear/debator factories lived in tradingagents/, deleted.
# The EVE brain (quiver_eve/run/decide.mjs) receives past_context via stdin from
# analyze.py:_run_eve. Verify the injection path is wired: decide.mjs reads stdin
# into its prompt, and analyze.py passes past_context as the subprocess stdin.)
import analyze as _az_d7  # noqa: E402
_azsrc = Path(_az_d7.__file__).read_text(encoding="utf-8")
check_true("D7: analyze.py passes past_context to the EVE brain via stdin",
           "ctx_stdin" in _azsrc and "past_context" in _azsrc)
_decide_src = (Path(_az_d7.__file__).resolve().parent / "quiver_eve" / "run" / "decide.mjs")
if _decide_src.exists():
    _dsrc = _decide_src.read_text(encoding="utf-8")
    check_true("D7: decide.mjs reads past_context from stdin into the prompt",
               "readStdin" in _dsrc and "PAST_CONTEXT" in _dsrc)
else:
    check_true("D7: decide.mjs present", False)  # fail loudly if the brain is missing


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
                  min_buying_power_buffer=5,
                  max_actions_per_ticker_per_day=1, max_analyses_per_ticker_per_day=1)
check("config: RiskConfig rebalance knobs default to today's behavior",
      (_rc.rebalance_enabled, _rc.cash_sleeve_ticker, _rc.rebalance_drift_band_pct, _rc.reconcile_unmanaged),
      (False, "SGOV", 5.0, True))
# F2/F3 anti-churn knobs default on the dataclass (min trade size $5, hysteresis 2 weight-pts).
check("config: F2/F3 anti-churn knobs default (min_trade_notional=5, hysteresis=2)",
      (_rc.min_trade_notional, _rc.conviction_rebalance_min_delta_pct), (5.0, 2.0))

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

# --- item 1: band = a real no-trade dead-band with a config fallback + edge dollars ---
check("pf: resolve_band per-name binds", _pf.resolve_band_pct(3.0, 7.0, 5.0), 3.0)
check("pf: resolve_band fallback to config when 0", _pf.resolve_band_pct(0.0, 20.0, 5.0), 5.0)
check("pf: resolve_band capped at weight*0.5", _pf.resolve_band_pct(8.0, 6.0, 5.0), 3.0)
check("pf: resolve_band config fallback also capped", _pf.resolve_band_pct(0.0, 4.0, 5.0), 2.0)
check("pf: resolve_band weight 0 -> band 0 (no strand; full unwind to target 0)",
      _pf.resolve_band_pct(3.0, 0.0, 5.0), 0.0)
# regression (review): a conviction-zeroed but still-ACTIVE held name must not keep a band-sized
# residual. band resolves to 0 at target 0, so the trim winds it fully to 0 (not stranded at 5% of eq).
_bz = [{"ticker": "ZZZ", "sleeve": "s", "target_weight": 0, "band": 0, "status": "active", "quotable": True}]
_bkz = {r["ticker"]: r for r in _pf.construct_target_book(_bz, {"ZZZ": 16.0}, 200.0, default_band_pct=5.0)}
check("pf: target-0 active held name has band_dollars 0 (no strand)", _bkz["ZZZ"]["band_dollars"], 0.0)
check("pf: target-0 active held name -> trim toward exact 0", _bkz["ZZZ"]["intent"], "trim")
# and the sell sizing winds it fully out (excess over 0+0 = full held mv)
check("sig: target-0 trim (band 0) sells the whole overshoot to 0",
      signals.resolve_target_sell_quantity(1.6, 10.0, 16.0, 0.0, upper_band_dollars=0.0), 1.6)
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
_cled.set_strategy_goal(created_at="t", target_return_pct=15, horizon_months=12,
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
_sled.set_strategy_goal(created_at="t", target_return_pct=15, horizon_months=12,
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

# --- signals: target-aware helpers + the BYTE-IDENTICAL clamp guarantee ---
check("sig: room_under_target", signals.room_under_target(9.0, 2.0), 7.0)
check("sig: room_under_target at/over -> 0", signals.room_under_target(9.0, 12.0), 0.0)
check("sig: target sell trim (excess/quote)", signals.resolve_target_sell_quantity(10.0, 5.0, 60.0, 45.0), 3.0)
check("sig: target sell full_exit -> all held", signals.resolve_target_sell_quantity(10.0, 5.0, 60.0, 45.0, full_exit=True), 10.0)
check("sig: target sell at/under target -> 0", signals.resolve_target_sell_quantity(10.0, 5.0, 40.0, 45.0), 0.0)
check("sig: target sell never oversells", signals.resolve_target_sell_quantity(10.0, 5.0, 1000.0, 0.0), 10.0)
check("sig: target sell no quote -> 0", signals.resolve_target_sell_quantity(10.0, 0.0, 60.0, 45.0), 0.0)
# item 1 (trim->edge): with a band, trim only the excess over target+band (60-45-5=10 -> 2sh),
# NOT the full excess over target (which would be 3sh). A name settles at the upper edge.
check("sig: trim to band EDGE not exact target",
      signals.resolve_target_sell_quantity(10.0, 5.0, 60.0, 45.0, upper_band_dollars=5.0), 2.0)
check("sig: within band (target+band) -> no trim",
      signals.resolve_target_sell_quantity(10.0, 5.0, 48.0, 45.0, upper_band_dollars=5.0), 0.0)
check("sig: full_exit ignores band (all held)",
      signals.resolve_target_sell_quantity(10.0, 5.0, 60.0, 45.0, full_exit=True, upper_band_dollars=5.0), 10.0)
check("sig: band=0 == classic trim-to-target (byte-identical)",
      signals.resolve_target_sell_quantity(10.0, 5.0, 60.0, 45.0, upper_band_dollars=0.0), 3.0)
# buy->target is unchanged: room_under_target with no band fills to the EXACT target
check("sig: buy fills to exact target (buy->target, no band)", signals.room_under_target(45.0, 30.0), 15.0)
_buy_args = dict(position_sizing=None, baseline_equity=100.0, buy_fraction=1.0, ceiling=25.0,
                 remaining_daily_cap=75.0, buying_power=100.0, buffer=5.0,
                 position_pct=10.0)
check("sig: resolve_buy_dollars room=None == omitted (byte-identical)",
      signals.resolve_buy_dollars(**_buy_args),
      signals.resolve_buy_dollars(**_buy_args, room_under_target=None))
check("sig: target room binds the buy (only ever reduces)",
      signals.resolve_buy_dollars(**_buy_args, room_under_target=3.0)[0], 3.0)


# --- PTJ defense: R:R gate, risk-based sizing, vol/trend scalars, de-risk (pure) ---
# F1 reward_risk_ratio
check("ptj: R:R = (target-entry)/(entry-stop)", signals.reward_risk_ratio(100, 90, 130), 3.0)
check("ptj: R:R missing target -> None (ungraded)", signals.reward_risk_ratio(100, 90, None), None)
check("ptj: R:R missing stop -> None", signals.reward_risk_ratio(100, None, 130), None)
check("ptj: R:R stop not below entry -> None", signals.reward_risk_ratio(100, 110, 130), None)
check("ptj: R:R target not above entry -> None", signals.reward_risk_ratio(100, 90, 95), None)
# F2 risk_budget_cap — B1: OFF (None) at pct<=0; C9: TIGHT stop -> LARGER cap
check("ptj: risk_cap OFF at pct=0 -> None (B1)", signals.risk_budget_cap(10000, 0.0, 100, 90), None)
check("ptj: risk_cap OFF at pct None -> None", signals.risk_budget_cap(10000, None, 100, 90), None)
check("ptj: risk_cap 1% mid stop", signals.risk_budget_cap(10000, 1.0, 100, 90), 1000.0)
check("ptj: risk_cap TIGHT stop -> LARGER cap (C9)", signals.risk_budget_cap(10000, 1.0, 100, 98), 5000.0)
check("ptj: risk_cap WIDE stop -> smaller cap", signals.risk_budget_cap(10000, 1.0, 100, 80), 500.0)
check_true("ptj: tight>mid>wide cap ordering",
           signals.risk_budget_cap(10000, 1.0, 100, 98) > signals.risk_budget_cap(10000, 1.0, 100, 90)
           > signals.risk_budget_cap(10000, 1.0, 100, 80))
check("ptj: risk_cap invalid stop -> None", signals.risk_budget_cap(10000, 1.0, 100, 100), None)
# F3 downside_vol_scalar — B2: 1.0 (true no-op) at target<=0/None
check("ptj: vol_scalar OFF at target=0 -> 1.0 (B2)", signals.downside_vol_scalar(0.3, 0.0), 1.0)
check("ptj: vol_scalar OFF at target None -> 1.0 (B2)", signals.downside_vol_scalar(0.3, None), 1.0)
check("ptj: vol_scalar vol<=target -> 1.0", signals.downside_vol_scalar(0.1, 0.2), 1.0)
check("ptj: vol_scalar shrinks on high vol", signals.downside_vol_scalar(0.4, 0.2), 0.5)
check("ptj: vol_scalar floored", signals.downside_vol_scalar(10.0, 0.2), 0.25)
check("ptj: vol_scalar unknown vol -> 1.0", signals.downside_vol_scalar(None, 0.2), 1.0)
# F4 trend_gate_scalar — buy-only; off default -> 1.0
check("ptj: trend OFF mode -> 1.0", signals.trend_gate_scalar("DOWNTREND", 0.1, "off"), 1.0)
check("ptj: trend soft uptrend -> 1.0", signals.trend_gate_scalar("UPTREND", 0.1, "soft"), 1.0)
check("ptj: trend soft downtrend -> 0.5", signals.trend_gate_scalar("DOWNTREND", 0.1, "soft"), 0.5)
check("ptj: trend soft neg-momentum -> 0.5", signals.trend_gate_scalar("UPTREND", -0.1, "soft"), 0.5)
check("ptj: trend block downtrend -> 0.0", signals.trend_gate_scalar("DOWNTREND", 0.1, "block"), 0.0)
check("ptj: trend unknown -> 1.0 (fail-open)", signals.trend_gate_scalar(None, None, "soft"), 1.0)
# F6 derisk_trim_fraction — deepest breached tier
_tiers = [{"at_drawdown_pct": 5, "trim_pct": 25}, {"at_drawdown_pct": 10, "trim_pct": 50}]
check("ptj: derisk -6% -> 25% tier", signals.derisk_trim_fraction(-6.0, _tiers), 0.25)
check("ptj: derisk -11% -> deepest 50% tier", signals.derisk_trim_fraction(-11.0, _tiers), 0.5)
check("ptj: derisk -3% -> no tier", signals.derisk_trim_fraction(-3.0, _tiers), 0.0)
check("ptj: derisk gain (drop>=0) -> 0", signals.derisk_trim_fraction(2.0, _tiers), 0.0)
check("ptj: derisk empty tiers -> 0", signals.derisk_trim_fraction(-9.0, []), 0.0)
check("ptj: derisk None drop -> 0", signals.derisk_trim_fraction(None, _tiers), 0.0)
# resolve_buy_dollars: new kwargs default None == omitted (BYTE-IDENTICAL); risk_cap binds;
# exposure_scalar guarded (0.0 zeroes, NOT treated as 1.0 — C6)
check("ptj: risk_cap/exposure None == omitted (byte-identical)",
      signals.resolve_buy_dollars(**_buy_args, risk_cap=None, exposure_scalar=None),
      signals.resolve_buy_dollars(**_buy_args))
check("ptj: risk_cap binds the buy (only reduces)",
      signals.resolve_buy_dollars(**_buy_args, risk_cap=3.0)[0], 3.0)
check("ptj: exposure_scalar 0.5 shrinks base (10->5)",
      signals.resolve_buy_dollars(**_buy_args, exposure_scalar=0.5)[0], 5.0)
check("ptj: exposure_scalar 0.0 zeroes the buy (C6, not 1.0)",
      signals.resolve_buy_dollars(**_buy_args, exposure_scalar=0.0)[0], 0.0)

# F3 downside_deviation (lib/trend) — the arg the vol floor consumes
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

# F8/F9 event_risk producer (pure, wall-clean)
from lib import event_risk as _evr  # noqa: E402
check("evr: days_until forward", _evr.days_until("2026-06-20", "2026-06-25"), 5)
check("evr: days_until past -> None", _evr.days_until("2026-06-20", "2026-06-15"), None)
check("evr: severity at event -> 1.0", _evr.event_severity(0), 1.0)
check("evr: severity mid horizon", _evr.event_severity(5, horizon_days=10), 0.5)
check("evr: severity beyond horizon -> 0", _evr.event_severity(15, horizon_days=10), 0.0)
check("evr: severity no event -> 0", _evr.event_severity(None), 0.0)
check("evr: non-confirm shrugs off bad news -> +", _evr.non_confirmation(0.05, -1), 0.5)
check("evr: non-confirm can't rally on good news -> -", _evr.non_confirmation(-0.05, 1), -0.5)
check("evr: non-confirm confirmed -> 0", _evr.non_confirmation(0.05, 1), 0.0)
check("evr: non-confirm no news -> 0", _evr.non_confirmation(0.05, None), 0.0)
_ed = _evr.build_descriptor(now_iso="2026-06-20", event_iso="2026-06-22", downside_vol=0.3, trend_regime="UPTREND")
check("evr: descriptor severity from near event", _ed.get("severity"), 0.8)
check("evr: descriptor carries downside_vol", _ed.get("downside_vol"), 0.3)
check("evr: descriptor carries trend_regime", _ed.get("trend_regime"), "UPTREND")
_ed2 = _evr.build_descriptor(now_iso="2026-06-20", recent_return=-0.05, news_polarity=1)  # bearish non-confirm, no event
check("evr: bearish non-confirm raises severity even with no event", _ed2.get("severity"), 0.5)

# F10 coordination-fade (shadow descriptor; NOT wired into the live gate)
from lib.dataflows import congress as _cong  # noqa: E402
check("f10: fade high for a fresh bill", _cong.coordination_fade({"status": "introduced"}), 1.0)
check("f10: fade low once through both chambers", _cong.coordination_fade({"status": "passed_both"}), 0.1)
check("f10: fade 0 once enacted", _cong.coordination_fade({"status": "became_law"}), 0.0)
check("f10: fade monotone in progress",
      _cong.coordination_fade({"status": "committee"}) > _cong.coordination_fade({"status": "passed_one"}), True)


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
for _tk, _w in [("URA", 7), ("ETHA", 2)]:
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
check("sctx: ETHA tier SATELLITE (weight 2)",
      _sctx.build_target_context(_s2, "ETHA", _sc)["tier"], "SATELLITE")


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

# D2 decision-path regression + intended behavior: the coarse goal-regime injected into the
# analysis prompt (via compute_from_ledger) is byte-identical while NO external flows are
# recorded, and only shifts once a deposit is deliberately recorded (deposits are not gains).
# Isolated ledger so it never mutates _s2. start 100 @2026-01-01, one baseline 110 @2026-07-02.
_d2db = tempfile.mktemp(suffix=".db")
_d2 = Ledger(_d2db)
_d2gid = _d2.set_strategy_goal(created_at="t", target_return_pct=15, horizon_months=12,
    benchmark="SGOV", benchmark_annual_pct=3.6, constraint_note="", macro_thesis_version="v",
    macro_thesis_json="{}", active_book="core_55_45", as_of="2026-06-13",
    start_date="2026-01-01", start_equity=100.0)
_d2.upsert_target_holding(goal_id=_d2gid, sleeve="Uranium/Power", ticker="URA", target_weight=7,
    band=2, status="active", book="core_55_45", quotable=True, proxy_ticker=None, updated_at="t")
_d2.get_or_create_baseline("2026-07-02", 110.0, "2026-07-02T00:00:00Z")
check("sctx: D2 regime byte-identical under zero flows (AHEAD)",
      _sctx.build_target_context(_d2, "URA", _sc)["goal_regime"], "AHEAD")
_d2.record_cash_flow(trade_date="2026-02-01", amount=40.0, note="deposit", created_at="t")
check("sctx: D2 regime shifts only after a deposit is recorded (BEHIND)",
      _sctx.build_target_context(_d2, "URA", _sc)["goal_regime"], "BEHIND")
Path(_d2db).unlink(missing_ok=True)
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
         "risk": {"max_dollars_per_trade": 25, "daily_loss_halt_pct": 5.0,
                  "daily_capital_deploy_cap": 75,
                  "min_buying_power_buffer": 5, "rebalance_enabled": enabled},
         "glm": {"chat_model": "glm-5.2", "reasoner_model": "glm-5.2"}}
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

# rebalance ON: the deterministic buy-to-target pass OWNS the book name (the LLM's
# dribble buy is suppressed) and deploys it to its target dollars, clamped by the caps.
_out_on = _tick._run_plan(_cfg_rebal(True), _tmp_ledger(), _copy.deepcopy(_p_tw))
_smh_on = next(o for o in _out_on["orders"] if o["ticker"] == "SMH")
check("plan: rebalance deploys book name to target (9 < ceiling 25)", _smh_on["dollar_amount"], 9.0)
check("plan: book-name buy routed through the rebalance pass", _smh_on["order_kind"], "rebalance_buy")
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

# --- rebalance BUY-TO-TARGET pass: the book deploys deterministically -------------
# An underweight book name the LLM did NOT buy (it merely HELD it) is still deployed to
# its target weight by the deterministic pass — the book owns sizing, not the model.
_p_dep = {"run_id": "FIX", "now_iso": "2026-06-15T10:00:00-04:00", "equity": 100.0,
          "buying_power": 100.0, "positions": {}, "quotes": {"SMH": 50.0},
          "analyses": [{"ticker": "SMH", "signal": "Hold", "position_pct": 0.0}],
          "target_weights": {"SMH": {"intent": "buy", "target_dollars": 9.0}}}
_smh_dep = next(o for o in _tick._run_plan(_cfg_rebal(True), _tmp_ledger(),
                                           _copy.deepcopy(_p_dep))["orders"] if o["ticker"] == "SMH")
check("plan: rebalance buy deploys an underweight book name the LLM HELD", _smh_dep["dollar_amount"], 9.0)
check("plan: rebalance buy order kind", _smh_dep["order_kind"], "rebalance_buy")
check("plan: rebalance buy signal tag", _smh_dep["signal"], "REBALANCE")
# rebalance OFF: the held name is NOT deployed (classic path; no buy-to-target).
check("plan: rebalance OFF -> held book name not deployed",
      [o for o in _tick._run_plan(_cfg_rebal(False), _tmp_ledger(), _copy.deepcopy(_p_dep))["orders"]
       if o["ticker"] == "SMH"], [])
# Running cash pool bounds total buys: avail = buying_power(15.5) - buffer(5) = 10.5; SMH takes
# its $10 target, leaving $0.5 — below the $1 broker minimum, so SOXX is skipped (not bounced).
_p_x = {"run_id": "FIX", "now_iso": "2026-06-15T10:00:00-04:00", "equity": 100.0,
        "buying_power": 15.5, "positions": {}, "quotes": {"SMH": 50.0, "SOXX": 50.0}, "analyses": [],
        "target_weights": {"SMH": {"intent": "buy", "target_dollars": 10.0},
                           "SOXX": {"intent": "buy", "target_dollars": 10.0}}}
_o_x = _tick._run_plan(_cfg_rebal(True), _tmp_ledger(), _copy.deepcopy(_p_x))["orders"]
check("plan: cash pool — first name deploys to target",
      next(o["dollar_amount"] for o in _o_x if o["ticker"] == "SMH"), 10.0)
check("plan: cash pool exhausted — second name skipped (no sub-$1 bounce)",
      [o for o in _o_x if o["ticker"] == "SOXX"], [])
# The cash sleeve (intent cash_residual) is never bought — stays as idle cash ballast.
_p_cash = {"run_id": "FIX", "now_iso": "2026-06-15T10:00:00-04:00", "equity": 100.0,
           "buying_power": 100.0, "positions": {}, "quotes": {}, "analyses": [],
           "target_weights": {"SGOV": {"intent": "cash_residual", "target_dollars": 23.0}}}
check("plan: cash sleeve (cash_residual) is never bought",
      _tick._run_plan(_cfg_rebal(True), _tmp_ledger(), _copy.deepcopy(_p_cash))["orders"], [])

# --- FIX: a limit BUY persists its NOTIONAL (shares*limit_price) to the orders row, so the
#     cross-tick daily-deploy-cap reseed (day_buys_total) sees it. A NULL dollar_amount was
#     invisible -> a later same-day tick could re-deploy the same dollars past the cap.
_cfg_lim = load_config(_write_config(
    {"account_number": "12345678", "dry_run": False, "kill_switch_file": "/tmp/k_lim",
     "risk": {"max_dollars_per_trade": 1000, "daily_loss_halt_pct": 50.0,
              "daily_capital_deploy_cap": 1000, "min_buying_power_buffer": 5},
     "order": {"buy_type": "limit"},
     "glm": {"chat_model": "glm-5.2", "reasoner_model": "glm-5.2"}}))
_led_lim = _tmp_ledger()
_lim_day = _market.trading_day_et()
_out_lim = _tick._run_plan(_cfg_lim, _led_lim, {
    "run_id": "LIM", "now_iso": "2026-06-15T10:00:00-04:00", "equity": 1000.0,
    "buying_power": 1000.0, "positions": {}, "quotes": {"SMH": 50.0},
    "analyses": [{"ticker": "SMH", "signal": "Buy", "position_pct": 10.0}]})
_lim_order = next(o for o in _out_lim["orders"] if o["ticker"] == "SMH")
check_true("plan: a limit buy actually reserved (whole shares >= 1)", _lim_order["type"] == "limit")
check_true("limit buy NOTIONAL is counted by day_buys_total (cross-tick cap stays honest)",
           abs(_led_lim.day_buys_total(_lim_day)
               - round(_lim_order["quantity"] * _lim_order["limit_price"], 2)) < 0.01
           and _led_lim.day_buys_total(_lim_day) > 0.0)

# --- AUTH_ERROR detector: collision-free vs TICK.md prose -------------------------
# The supervisor must NOT fire on the literal "AUTH_ERROR" that TICK.md legitimately
# contains (the bug), but MUST fire on the unique sentinel `tick.py auth-stop` emits, OR
# on the ledger auth_error marker (which survives a failed alert email).
_SENTINEL = _tick.AUTH_STOP_SENTINEL


def _detect_auth(combined, marked):  # mirrors run_tick.py's detector (pure form)
    return (_SENTINEL in combined) or bool(marked)


_tickmd_text = (_REPO / "TICK.md").read_text(encoding="utf-8")
check_true("auth: sentinel is ABSENT from TICK.md (collision-free)", _SENTINEL not in _tickmd_text)
check_true("auth: literal AUTH_ERROR still present in TICK.md (the trap we route around)",
           "AUTH_ERROR" in _tickmd_text)
_tickmd_like = ('log `AUTH_ERROR`, fire the alert kind:"auth_error" stage:"broker_auth" '
                '... run `tick.py auth-stop` ... and STOP.')
check("auth: TICK.md-like prose (AUTH_ERROR, no sentinel) does NOT false-positive",
      _detect_auth(_tickmd_like, marked=False), False)
check("auth: real auth-stop sentinel in stdout fires",
      _detect_auth('{"event": "%s", "stage": "broker_auth"}' % _SENTINEL, marked=False), True)
check("auth: ledger marker fires even when sentinel absent (failed-email path)",
      _detect_auth(_tickmd_like, marked="somehash"), True)
check("auth: cmd_auth_stop emits the sentinel", _tick.cmd_auth_stop(None)["event"], _SENTINEL)

# strategy-set -> construct -> goal-track lifecycle (offline, real strategy.yaml)
_cfg_s3 = _cfg_rebal(True, strategy_path=str(_REPO / "tests" / "fixtures" / "strategy_fixture.yaml"))
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

# --- self-reconciliation: a HELD position not in the book is wound to zero -------
# construct flags off-book holdings; plan winds them down (gated reconcile_unmanaged).
_con_rec = _tick._run_construct(_cfg_s3, _led_s3, {"equity": 100.0,
    "positions": {"AAPL": {"market_value": 12.0}, "SMH": {"market_value": 2.0},
                  "SGOV": {"market_value": 50.0}}, "macro_reading": None})
check_true("construct: off-book holding (AAPL) flagged unmanaged", "AAPL" in _con_rec["unmanaged"])
check("construct: orphan gets a full-exit intent", _con_rec["target_weights"]["AAPL"]["intent"], "exit")
check_true("construct: orphan carries the orphan flag", _con_rec["target_weights"]["AAPL"].get("orphan") is True)
check_true("construct: in-book SMH NOT flagged unmanaged", "SMH" not in _con_rec["unmanaged"])
check_true("construct: cash SGOV NOT flagged unmanaged", "SGOV" not in _con_rec["unmanaged"])

_p_rec = {"run_id": "REC", "now_iso": "2026-06-15T10:00:00-04:00", "equity": 100.0,
          "buying_power": 100.0, "positions": {"AAPL": {"quantity": 2.0, "market_value": 12.0}},
          "quotes": {"AAPL": 6.0}, "analyses": [], "target_weights": _con_rec["target_weights"]}
_out_rec = _tick._run_plan(_cfg_s3, _tmp_ledger(), _copy.deepcopy(_p_rec))
_aapl = next((o for o in _out_rec["orders"] if o["ticker"] == "AAPL"), None)
check_true("plan: reconcile sells the off-book holding (full exit)",
           _aapl is not None and _aapl["order_kind"] == "reconcile_exit" and _aapl["quantity"] == 2.0)
check("plan: reconcile sell is long-only (a SELL)", _aapl["side"], "sell")
# reconcile OFF (and rebalance off) -> the off-book holding is left untouched
_cfg_norec = load_config(_write_config({
    "account_number": "12345678", "dry_run": True, "kill_switch_file": "/tmp/k_norec",
    "risk": {"max_dollars_per_trade": 25, "daily_loss_halt_pct": 5.0, "daily_capital_deploy_cap": 75,
             "min_buying_power_buffer": 5,
             "rebalance_enabled": False, "reconcile_unmanaged": False},
    "glm": {"chat_model": "glm-5.2", "reasoner_model": "glm-5.2"},
    "strategy_path": str(_REPO / "tests" / "fixtures" / "strategy_fixture.yaml")}))
_out_norec = _tick._run_plan(_cfg_norec, _tmp_ledger(), _copy.deepcopy(_p_rec))
check("plan: reconcile OFF leaves the off-book holding untouched",
      [o for o in _out_norec["orders"] if o["ticker"] == "AAPL"], [])
check("config: reconcile_unmanaged defaults ON", _cfg_s3.risk.reconcile_unmanaged, True)
check("config: reconcile_unmanaged explicit false respected", _cfg_norec.risk.reconcile_unmanaged, False)

# CRITICAL fail-safe: an active goal with an EMPTY target book (corruption / an
# interrupted setup) must NOT make reconcile sell the whole held portfolio.
_led_empty = _tmp_ledger()
_led_empty.set_strategy_goal(created_at="t", target_return_pct=15, horizon_months=12,
    benchmark="SGOV", benchmark_annual_pct=3.6, constraint_note="", macro_thesis_version="v",
    macro_thesis_json="{}", active_book="core_ai", as_of="d", start_date="d", start_equity=100.0)
_con_empty = _tick._run_construct(_cfg_s3, _led_empty, {"equity": 100.0,
    "positions": {"SMH": {"market_value": 30.0}, "CEG": {"market_value": 20.0}}, "macro_reading": None})
check("construct: empty book emits NO orphans (no mass liquidation)", _con_empty["unmanaged"], [])
check_true("construct: empty book target_weights has no orphan exits",
           not any(v.get("orphan") for v in _con_empty["target_weights"].values()))

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

_led_s3.get_or_create_baseline("2026-09-01", 110.0, "2026-09-01T00:00:00-04:00")
_gt = _tick._run_goal_track(_cfg_s3, _led_s3)
check("goal-track: records a snapshot", _gt["recorded"], True)
check_true("goal-track: regime present", _gt.get("regime") in ("AHEAD", "ON-TRACK", "BEHIND"))

# ---- deposit auto-capture wired through _run_goal_track + config + flow-suggest CLI ----
def _cfg_goal_dict(goal_block=None):
    d = {"account_number": "12345678", "dry_run": True, "kill_switch_file": "/tmp/k_goal",
         "risk": {"max_dollars_per_trade": 25, "daily_loss_halt_pct": 5.0,
                  "daily_capital_deploy_cap": 75, "min_buying_power_buffer": 5},
         "glm": {"chat_model": "glm-5.2", "reasoner_model": "glm-5.2"}}
    if goal_block is not None:
        d["goal"] = goal_block
    return d

def _cfg_goal(auto, **over):
    return load_config(_write_config(_cfg_goal_dict({"auto_capture_flows": auto, **over})))

def _seed_jump_ledger(start_equity, jump_to, *later):
    """A ledger: active goal (start @ 2026-01-01) + a start->jump_to jump on 2026-01-03
    + optional later baselines (to settle/persist or revert the jump)."""
    led = _tmp_ledger()
    led.set_strategy_goal(created_at="t", target_return_pct=15, horizon_months=12,
        benchmark="SGOV", benchmark_annual_pct=3.6, constraint_note="", macro_thesis_version="v",
        macro_thesis_json="{}", active_book="core_55_45", as_of="d",
        start_date="2026-01-01", start_equity=start_equity)
    led.get_or_create_baseline("2026-01-01", start_equity, "2026-01-01T00:00:00Z")
    led.get_or_create_baseline("2026-01-03", jump_to, "2026-01-03T00:00:00Z")
    for dte, eq in later:
        led.get_or_create_baseline(dte, eq, dte + "T00:00:00Z")
    return led

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
check("flow: auto-capture idempotent across ticks", len(_led_on.cash_flows()), 1)
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

# --- universe derivation: preflight's `pending` comes from the PORTFOLIO book,
#     never a hand-maintained watchlist (cash + non-quotable filtered out) ---
_uni_from_goal = _tick._analysis_universe(_cfg_s3, _led_s3)  # ledger has an active goal
check_true("universe: derived from the active goal book (engine names)",
           "SMH" in _uni_from_goal and "URA" in _uni_from_goal)
check_true("universe: cash residual (SGOV) excluded", "SGOV" not in _uni_from_goal)
check_true("universe: non-quotable (SOL) excluded", "SOL" not in _uni_from_goal)
# No goal seeded yet -> falls back to strategy.yaml's default book (same names).
_uni_fallback = _tick._analysis_universe(_cfg_s3, _tmp_ledger())
check_true("universe: falls back to strategy.yaml book before strategy-set runs",
           "SMH" in _uni_fallback and "SGOV" not in _uni_fallback and "SOL" not in _uni_fallback)
# No strategy at all -> empty universe (fail-safe: no portfolio, no trades).
_cfg_nostrat = _cfg_rebal(False, strategy_path="/nonexistent/strategy.yaml")
check("universe: no strategy -> empty (fail-safe no-op)",
      _tick._analysis_universe(_cfg_nostrat, _tmp_ledger()), [])

# Precedence (yaml=source, ledger=runtime): with a goal present the LEDGER book wins
# over strategy.yaml. DIVERGENT data so a yaml-first regression is actually caught.
_led_div = _tmp_ledger()
_gdiv = _led_div.set_strategy_goal(created_at="t", target_return_pct=15, horizon_months=12,
    benchmark="SGOV", benchmark_annual_pct=3.6, constraint_note="", macro_thesis_version="v",
    macro_thesis_json="{}", active_book="core_55_45", as_of="d", start_date="d", start_equity=100.0)
_led_div.upsert_target_holding(goal_id=_gdiv, sleeve="Semis", ticker="ZZZZ", target_weight=55, band=5,
    status="active", book="core_55_45", quotable=True, proxy_ticker=None, updated_at="t")
_led_div.upsert_target_holding(goal_id=_gdiv, sleeve="Cash", ticker="SGOV", target_weight=45, band=0,
    status="active", book="core_55_45", quotable=True, proxy_ticker=None, updated_at="t")
check("universe: active ledger goal WINS over strategy.yaml (no yaml fallback)",
      _tick._analysis_universe(_cfg_s3, _led_div), ["ZZZZ"])

# Active goal whose engines are ALL wound down (only cash active) -> empty universe,
# and it must NOT revert to the stale strategy.yaml book (a de-risked book stays de-risked).
_led_wound = _tmp_ledger()
_tick._run_strategy_set(_cfg_s3, _led_wound, {"equity": 100.0, "now_iso": "2026-06-13T10:00:00-04:00"})
_gw = _led_wound.get_active_goal()["id"]
for _r in _led_wound.active_target_portfolio(_gw, statuses=("active",)):
    if _r["sleeve"] != "Cash":
        _led_wound.set_holding_status(_gw, _r["ticker"], "exiting", "t")
check("universe: active goal all-exiting -> [] (no stale yaml fallback)",
      _tick._analysis_universe(_cfg_s3, _led_wound), [])

# --- cmd_preflight wiring (offline; market open/time gates monkeypatched) -------
_pf_saved = (_market.is_regular_session_open, _market.minutes_since_open)
_market.is_regular_session_open = lambda *a, **k: True
_market.minutes_since_open = lambda *a, **k: 999
try:
    _pf_day = _market.trading_day_et()
    # (a) active goal, nothing acted -> proceed; pending = engine names (no cash/non-quotable)
    _led_pf = _tmp_ledger()
    _tick._run_strategy_set(_cfg_s3, _led_pf, {"equity": 100.0, "now_iso": "2026-06-13T10:00:00-04:00"})
    _pf_a = _tick._run_preflight(_cfg_s3, _led_pf)
    check("preflight: proceeds on the portfolio universe", _pf_a["proceed"], True)
    check_true("preflight: pending = engine names, cash/non-quotable excluded",
               "SMH" in _pf_a["pending"] and "SGOV" not in _pf_a["pending"] and "SOL" not in _pf_a["pending"])
    # (b) every engine already acted today -> no-op with the RENAMED reason (TICK.md-coupled)
    for _t in _pf_a["pending"]:
        _led_pf.record_action(_pf_day, _t, signal="Hold", intent="hold", status="skipped",
                              detail="x", now_iso="t")
    _pf_b = _tick._run_preflight(_cfg_s3, _led_pf)
    check("preflight: all acted -> proceed False", _pf_b["proceed"], False)
    check("preflight: renamed no-op reason string", _pf_b["reason"], "all_portfolio_tickers_already_acted_today")
    # (c) no goal + no strategy -> no_portfolio_universe (fail-safe, not an error)
    _pf_c = _tick._run_preflight(_cfg_nostrat, _tmp_ledger())
    check("preflight: empty universe -> proceed False", _pf_c["proceed"], False)
    check_true("preflight: no_portfolio_universe reason", _pf_c["reason"].startswith("no_portfolio_universe"))
    # (d) wind-down work (rebalance exiting OR reconcile) keeps an empty-universe tick alive
    check("preflight: rebalance+reconcile keep the tick alive to wind down",
          _tick._run_preflight(_cfg_s3, _led_wound)["proceed"], True)
    # (e) reconcile-only (rebalance OFF, reconcile ON) ALSO keeps it alive (independent of rebalance)
    _cfg_reconly = _cfg_rebal(False, strategy_path=str(_REPO / "tests" / "fixtures" / "strategy_fixture.yaml"))
    check("preflight: reconcile alone keeps the tick alive (winddown, no rebalance)",
          _tick._run_preflight(_cfg_reconly, _led_wound)["proceed"], True)
    # (f) BOTH rebalance and reconcile OFF -> the wound-down book correctly no-ops
    check("preflight: rebalance+reconcile both OFF -> proceed False",
          _tick._run_preflight(_cfg_norec, _led_wound)["proceed"], False)
    # (g) CRASH-RECOVERY guard: a name with an order RESERVED but never finalized (a crash
    #     between broker place and commit) is EXCLUDED from `pending` so it is NOT re-analyzed
    #     /re-planned (which would mint a SECOND ref_id -> a real double-buy). The tick still
    #     PROCEEDS and carries the stuck order to STEP 6 reconcile (same ref_id).
    _led_unf = _tmp_ledger()
    _tick._run_strategy_set(_cfg_s3, _led_unf, {"equity": 100.0, "now_iso": "2026-06-13T10:00:00-04:00"})
    _pf_pre = _tick._run_preflight(_cfg_s3, _led_unf)["pending"]
    _stuck = _pf_pre[0]
    _led_unf.reserve_order("UNF-1", _pf_day, _stuck, side="buy", type="market",
                           dollar_amount=25.0, quantity=None, now_iso="t")
    _pf_g = _tick._run_preflight(_cfg_s3, _led_unf)
    check_true("preflight: unfinalized-order name EXCLUDED from pending (no re-plan/double-place)",
               _stuck not in _pf_g["pending"] and len(_pf_g["pending"]) == len(_pf_pre) - 1)
    check("preflight: a stuck-order tick still proceeds (for STEP 6 reconcile)", _pf_g["proceed"], True)
    check_true("preflight: the stuck order is carried to STEP 6 reconcile",
               any(o["ticker"] == _stuck for o in _pf_g["unfinalized"]))
    # all engine names stuck -> empty pending, but reconcile-only keeps the tick alive
    # even with rebalance+reconcile OFF (would have no-op'd + stranded the orders before the fix).
    for _i, _t in enumerate(_pf_pre):
        _led_unf.reserve_order(f"UNF-ALL-{_i}", _pf_day, _t, side="buy", type="market",
                               dollar_amount=25.0, quantity=None, now_iso="t")
    _pf_all = _tick._run_preflight(_cfg_norec, _led_unf)
    check("preflight: all-names-stuck -> empty pending", _pf_all["pending"], [])
    check("preflight: all-names-stuck still proceeds purely to reconcile", _pf_all["proceed"], True)
finally:
    _market.is_regular_session_open, _market.minutes_since_open = _pf_saved


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
# content_hash is tier-AWARE (so the open-row dedup index + the stored tier the apply gate
# reads stay accurate); change_key is tier-FREE (so recurrence + cooldown pool across sources).
# The same real change from two signals: DISTINCT content_hash, IDENTICAL change_key.
_p_uni = _learn.Proposal("PROPOSE_ADD", "CEG", "Power", "universe", "screener")
_p_leg = _learn.Proposal("PROPOSE_ADD", "CEG", "Power", "legislative", "catalyst")
check_true("learn: content_hash DIFFERS across tiers (dedup stays tier-aware)",
           _p_uni.content_hash() != _p_leg.content_hash())
check("learn: change_key IDENTICAL across tiers (pooled read key)",
      _p_uni.change_key(), _p_leg.change_key())
check_true("learn: change_key differs from content_hash (distinct keys)",
           _p_uni.change_key() != _p_uni.content_hash())
check_true("learn: change_key varies by (kind,ticker)",
           _p_uni.change_key() != _learn.Proposal("PROPOSE_REMOVE", "CEG", "Power", "universe", "x").change_key()
           and _p_uni.change_key() != _learn.Proposal("PROPOSE_ADD", "URA", "Power", "universe", "x").change_key())

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
# deploy e2e drills documented in docs/DEPLOY.md, not the offline suite.)
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


# ============================================================================
# Prompts-as-markdown loader + the human/agent-editable strategy doc.
# ============================================================================
import lib.prompts as _prompts  # noqa: E402
import lib.strategy_doc as _sdoc  # noqa: E402

check_true("prompts: orchestrator prompt loads from md", "execution orchestrator" in _prompts.load_prompt("orchestrator"))
check_true("prompts: framework agent prompt loads from md", "close_50_sma" in _prompts.load_prompt("agents/market_analyst"))
check_true("prompts: raw load preserves a langchain placeholder", "{asset_label}" in _prompts.load_prompt("agents/news_analyst"))
check_true("prompts: kwargs fill the placeholder", "ZZZ-specific" in _prompts.load_prompt("agents/news_analyst", asset_label="ZZZ"))
check_raises("prompts: missing prompt raises", lambda: _prompts.load_prompt("nonexistent_xyz"), FileNotFoundError)

# strategy doc append (the agent maintains STRATEGY.md as it learns)
_tmp_strat = Path(tempfile.mktemp(suffix=".md"))
_tmp_strat.write_text("# x\n## 7. Agent learnings\nintro\n\n- _(none yet)_\n", encoding="utf-8")
check("sdoc: append replaces the placeholder", _sdoc.append_learning("first note", date="2026-06-14", doc_path=_tmp_strat), True)
_t1 = _tmp_strat.read_text(encoding="utf-8")
check_true("sdoc: dated bullet written + placeholder gone",
           "- **2026-06-14** — first note" in _t1 and "_(none yet)_" not in _t1)
_sdoc.append_learning("second note", date="2026-06-14", doc_path=_tmp_strat)
check_true("sdoc: second append adds another bullet",
           _tmp_strat.read_text(encoding="utf-8").count("- **2026-06-14**") == 2)
check("sdoc: missing doc -> no-op (False, never crashes a tick)",
      _sdoc.append_learning("x", date="d", doc_path=Path("/no/such/STRATEGY.md")), False)


# ============================================================================
# CONSISTENCY LAYER (Component A-F): memory-grounded strategy-consistency gate,
# regime hysteresis/confirmation, strategic change-log, decision proof.
# ============================================================================
import lib.risk as _crisk  # noqa: E402
import json as _json_mod  # noqa: E402

# --- A: pure direction / reversal helpers ---
check("cons: direction_of buy=open", signals.direction_of("buy"), "open")
check("cons: direction_of sell=close", signals.direction_of("sell"), "close")
check("cons: direction_of hold=neutral", signals.direction_of("hold"), "neutral")
check_true("cons: is_reversal buy->sell", signals.is_reversal("buy", "sell"))
check_true("cons: is_reversal sell->buy", signals.is_reversal("sell", "buy"))
check_true("cons: continuation buy->buy is NOT a reversal", not signals.is_reversal("buy", "buy"))
check_true("cons: no prior stance is NOT a reversal", not signals.is_reversal(None, "sell"))
check_true("cons: hold is neutral, not a reversal", not signals.is_reversal("hold", "sell"))
check("cons: reversal_rate counts flips", signals.reversal_rate(["buy", "sell", "buy", "hold", "buy"]), (2, 3))
check("cons: reversal_rate empty", signals.reversal_rate([]), (0, 0))
check("cons: discretionary reversals (no triggers) = 2",
      signals.count_discretionary_reversals([{"intent": "buy"}, {"intent": "sell"}, {"intent": "buy"}]), 2)
check("cons: a plan-triggered reversal does NOT count toward churn",
      signals.count_discretionary_reversals(
          [{"intent": "buy"}, {"intent": "sell", "plan_trigger": "stop_hit"}, {"intent": "buy"}]), 1)

# --- A: strategy_consistency_verdict — every branch ---
_V = signals.strategy_consistency_verdict
check("cons: continuation allowed", _V(prior_intent="buy", new_intent="buy", plan_trigger=None,
      basis_changed=False, recent_discretionary_reversals=9, max_discretionary_reversals=1), (True, "continuation_or_neutral"))
check_true("cons: plan-trigger reversal allowed (overrides churn)",
           _V(prior_intent="buy", new_intent="sell", plan_trigger="stop_hit", basis_changed=False,
              recent_discretionary_reversals=9, max_discretionary_reversals=1)[0])
check("cons: ungrounded reversal suppressed", _V(prior_intent="buy", new_intent="sell", plan_trigger=None,
      basis_changed=False, recent_discretionary_reversals=0, max_discretionary_reversals=1), (False, "ungrounded_reversal"))
check("cons: basis-change reversal within budget allowed", _V(prior_intent="buy", new_intent="sell", plan_trigger=None,
      basis_changed=True, recent_discretionary_reversals=0, max_discretionary_reversals=1), (True, "recorded_basis_change"))
check("cons: basis-change OVER budget = churn suppressed", _V(prior_intent="buy", new_intent="sell", plan_trigger=None,
      basis_changed=True, recent_discretionary_reversals=1, max_discretionary_reversals=1), (False, "basis_churn"))
check("cons: max=0 blocks even a basis change (stop/target only)", _V(prior_intent="buy", new_intent="sell",
      plan_trigger=None, basis_changed=True, recent_discretionary_reversals=0, max_discretionary_reversals=0), (False, "basis_churn"))
check("cons: disabled gate always allows", _V(prior_intent="buy", new_intent="sell", plan_trigger=None,
      basis_changed=False, recent_discretionary_reversals=0, max_discretionary_reversals=1, enabled=False), (True, "gate_disabled"))

# --- F: signal_stability metric (reuses signals.reversal_rate) ---
_sm = _crisk.signal_stability(["buy", "sell", "buy"])
check("cons: signal_stability value = reversals/transitions", round(_sm.value, 3), 1.0)
check("cons: signal_stability n = transitions", _sm.n, 2)
check("cons: signal_stability None for <2 stances", _crisk.signal_stability(["buy"]).value, None)

# --- C: regime hysteresis + confirmation ---
import types as _types2  # noqa: E402
_mcfg = _types2.SimpleNamespace(macro_thesis=_types2.SimpleNamespace(
    deploy_trigger_pce_pct=2.5, standdown_trigger_pce_pct=3.5, standdown_on_hike=True))
check("cons: banded regime clears standdown band", _strat.regime_label_banded(_mcfg, {"core_pce_pct": 3.65}, 0.1), _strat.REGIME_STAND_DOWN)
check("cons: banded regime WITHIN band reads HOLD", _strat.regime_label_banded(_mcfg, {"core_pce_pct": 3.55}, 0.1), _strat.REGIME_HOLD)
check("cons: banded regime clears deploy band", _strat.regime_label_banded(_mcfg, {"core_pce_pct": 2.35}, 0.1), _strat.REGIME_DEPLOY)
check("cons: fed hike is categorical standdown", _strat.regime_label_banded(_mcfg, {"fed_hike": True}, 0.1), _strat.REGIME_STAND_DOWN)
check("cons: normalize legacy lowercase", _strat.normalize_regime("standdown"), _strat.REGIME_STAND_DOWN)
# confirmation state machine
_r1 = _strat.regime_with_confirmation(None, _strat.REGIME_STAND_DOWN, confirm_n=2, min_dwell_days=5, today="2026-06-01")
check_true("cons: 1st reading -> pending, no change", _r1["effective_regime"] == _strat.REGIME_HOLD and not _r1["changed"] and _r1["confirm_count"] == 1)
_st1 = {"regime": "neutral", "pending_regime": "standdown", "confirm_count": 1, "regime_since": None}
_r2 = _strat.regime_with_confirmation(_st1, _strat.REGIME_STAND_DOWN, confirm_n=2, min_dwell_days=5, today="2026-06-02")
check_true("cons: 2nd consecutive -> confirmed flip", _r2["effective_regime"] == _strat.REGIME_STAND_DOWN and _r2["changed"] and _r2["regime_since"] == "2026-06-02")
_st2 = {"regime": "standdown", "pending_regime": "deploy", "confirm_count": 2, "regime_since": "2026-06-02"}
_r3 = _strat.regime_with_confirmation(_st2, _strat.REGIME_DEPLOY, confirm_n=2, min_dwell_days=5, today="2026-06-04")
check_true("cons: confirmed but dwell-locked stays put", _r3["effective_regime"] == _strat.REGIME_STAND_DOWN and not _r3["changed"] and "dwell" in _r3["reason"])
_r4 = _strat.regime_with_confirmation(_st2, _strat.REGIME_DEPLOY, confirm_n=2, min_dwell_days=5, today="2026-06-09")
check_true("cons: flips once dwell elapses", _r4["effective_regime"] == _strat.REGIME_DEPLOY and _r4["changed"])
_st3 = {"regime": "standdown", "pending_regime": "deploy", "confirm_count": 1, "regime_since": "2026-06-02"}
_r5 = _strat.regime_with_confirmation(_st3, _strat.REGIME_STAND_DOWN, confirm_n=2, min_dwell_days=5, today="2026-06-05")
check_true("cons: a stable reading clears the pending candidate", _r5["pending_regime"] is None and _r5["confirm_count"] == 0)

# --- ledger: new consistency reads + migrations ---
_lc2 = _tmp_ledger()
_did = _lc2.record_decision(trade_date="2026-06-10", ticker="NVDA", decided_at="2026-06-10T10:00:00",
                            signal="Buy", intent="buy", decision_price=100.0, stop_loss=90.0,
                            basis="momentum", plan_trigger=None, target_price=130.0,
                            proof_json='{"verdict":{"allowed":true,"reason":"continuation_or_neutral"}}')
check_true("cons: record_decision returns an id", isinstance(_did, int) and _did > 0)
_dec = _lc2.get_decision(_did)
check("cons: decision persisted basis", _dec["basis"], "momentum")
check("cons: decision persisted plan_trigger NULL", _dec["plan_trigger"], None)
check("cons: decision persisted proof_json round-trips",
      _json_mod.loads(_dec["proof_json"])["verdict"]["reason"], "continuation_or_neutral")
# completed-trade reads need an actions event
_lc2.record_event(trade_date="2026-06-10", ticker="NVDA", ts="2026-06-10T10:00:01", signal="Buy",
                  intent="buy", status="dry_run")
_lct = _lc2.last_completed_trade("NVDA")
check("cons: last_completed_trade intent", _lct["intent"], "buy")
check("cons: last_completed_trade joins the decision's stop", _lct["stop_loss"], 90.0)
check("cons: last_completed_trade joins basis", _lct["basis"], "momentum")
check("cons: last_completed_trade None for untraded name", _lc2.last_completed_trade("ZZZZ"), None)
check("cons: recent_completed_trades returns the buy", len(_lc2.recent_completed_trades("NVDA", 6)), 1)
# strategy change log
_g5 = _lc2.set_strategy_goal(created_at="t", target_return_pct=15, horizon_months=12, benchmark="SGOV",
    benchmark_annual_pct=3.6, constraint_note="", macro_thesis_version="v", macro_thesis_json="{}",
    active_book="core_55_45", as_of="x", start_date="2026-01-01", start_equity=100.0)
_lc2.record_strategy_change(goal_id=_g5, changed_at="2026-06-12T00:00:00", change_type="regime",
                            from_value="HOLD", to_value="STAND_DOWN", trigger="macro_reading", reason="confirmed")
_hist = _lc2.strategy_change_history(_g5)
check("cons: strategy_change_history records the regime change", len(_hist), 1)
check("cons: change row carries from->to", (_hist[0]["from_value"], _hist[0]["to_value"]), ("HOLD", "STAND_DOWN"))
check("cons: strategy_change_history filters by type",
      len(_lc2.strategy_change_history(_g5, change_type="weight")), 0)
# universe-proposal history reads (E1/E2). Recurrence + cooldown key on change_key
# (pooled across tiers); the identity read stays on content_hash.
_ch = "consid_hash"
_ck = "consid_key"
_lc2.record_universe_proposal(goal_id=_g5, proposed_at="2026-06-10T00:00:00Z", kind="PROPOSE_REMOVE",
    ticker="XYZ", sleeve="s", from_book=None, to_book=None, target_weight=5.0, tier="universe",
    content_hash=_ch, change_key=_ck, reason="r", goal_gap_pct=-5.0)
check("cons: count_proposal_recurrence = 1 distinct day", _lc2.count_proposal_recurrence(_g5, _ck), 1)
check("cons: last_decided_universe_change None while proposed", _lc2.last_decided_universe_change(_g5, _ch), None)
check("cons: last_decided_change_key None while proposed", _lc2.last_decided_change_key(_g5, _ck), None)
# POOLING across tiers (the P0a fix): the SAME change proposed by two sources on two
# distinct days pools its confirm-days (reuse goal _g5 from the block above).
_pk = _learn.Proposal("PROPOSE_ADD", "CEG", "Power", "universe", "x").change_key()
_lc2.record_universe_proposal(goal_id=_g5, proposed_at="2026-06-10T00:00:00Z", kind="PROPOSE_ADD",
    ticker="CEG", sleeve="Power", from_book=None, to_book=None, target_weight=4.0, tier="universe",
    content_hash=_learn.Proposal("PROPOSE_ADD","CEG","Power","universe","x").content_hash(),
    change_key=_pk, reason="screener", goal_gap_pct=None)
_lc2.record_universe_proposal(goal_id=_g5, proposed_at="2026-06-11T00:00:00Z", kind="PROPOSE_ADD",
    ticker="CEG", sleeve="Power", from_book=None, to_book=None, target_weight=4.0, tier="legislative",
    content_hash=_learn.Proposal("PROPOSE_ADD","CEG","Power","legislative","y").content_hash(),
    change_key=_pk, reason="catalyst", goal_gap_pct=None)
check("cons: recurrence POOLS the same change across tiers (2 distinct days)",
      _lc2.count_proposal_recurrence(_g5, _pk), 2)

# MIGRATION: an old universe_change_log with NO change_key column -> Ledger() adds it AND
# backfills every row to match Proposal.change_key() byte-for-byte (so pre-migration rows
# pool with post-migration ones). Mirrors the shipped migration-test precedent.
import sqlite3 as _sqlite3
_dbp_mig = tempfile.mktemp(suffix=".db")
_mc = _sqlite3.connect(_dbp_mig)
_mc.executescript("""
CREATE TABLE universe_change_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT, goal_id INTEGER NOT NULL, proposed_at TEXT,
    kind TEXT, ticker TEXT, sleeve TEXT, from_book TEXT, to_book TEXT, target_weight REAL,
    tier TEXT, content_hash TEXT NOT NULL, reason TEXT NOT NULL, goal_gap_pct REAL,
    status TEXT NOT NULL DEFAULT 'proposed', decided_at TEXT, decided_by TEXT);
INSERT INTO universe_change_log (goal_id, kind, ticker, tier, content_hash, reason)
    VALUES (1, 'PROPOSE_ADD', 'CEG', 'legislative', 'oldhash', 'pre-migration row');
""")
_mc.commit(); _mc.close()
Ledger(_dbp_mig)  # constructing runs ensure_schema -> _migrate_universe_change_key
_mc2 = _sqlite3.connect(_dbp_mig)
_mig_cols = {r[1] for r in _mc2.execute("PRAGMA table_info(universe_change_log)").fetchall()}
check_true("migrate: change_key column added to legacy universe_change_log", "change_key" in _mig_cols)
_mig_key = _mc2.execute("SELECT change_key FROM universe_change_log WHERE content_hash='oldhash'").fetchone()[0]
_mc2.close()
check("migrate: backfill matches Proposal.change_key() byte-for-byte",
      _mig_key, _learn.Proposal("PROPOSE_ADD", "CEG", "Power", "legislative", "x").change_key())

# migrations idempotent on a fresh + re-opened db
_dbp2 = tempfile.mktemp(suffix=".db")
Ledger(_dbp2)
_reopen = Ledger(_dbp2)  # re-running migrations must not raise
check_true("cons: migrations idempotent on re-open", _reopen.get_active_goal() is None)

# --- config: risk.consistency validation ---
def _cfg_with_consistency(cons_block):
    d = {"account_number": "12345678", "dry_run": True, "kill_switch_file": "/tmp/kcons",
         "risk": {"max_dollars_per_trade": 25, "daily_loss_halt_pct": 5.0,
                  "daily_capital_deploy_cap": 75,
                  "min_buying_power_buffer": 5, "consistency": cons_block},
         "glm": {"chat_model": "glm-5.2", "reasoner_model": "glm-5.2"}}
    return load_config(_write_config(d))

_cc = _cfg_with_consistency({"enabled": False, "max_discretionary_reversals": 2, "flip_window": 4, "loss_catalyst_pct": 10.0})
check("cons-cfg: parsed enabled false", _cc.risk.consistency_enabled, False)
check("cons-cfg: parsed max_discretionary_reversals", _cc.risk.max_discretionary_reversals, 2)
check("cons-cfg: parsed flip_window", _cc.risk.consistency_flip_window, 4)
check("cons-cfg: default ON when block absent", _cfg_rebal(False).risk.consistency_enabled, True)
check_raises("cons-cfg: negative max_discretionary_reversals raises",
             lambda: _cfg_with_consistency({"max_discretionary_reversals": -1}), ConfigError)
check_raises("cons-cfg: zero flip_window raises",
             lambda: _cfg_with_consistency({"flip_window": 0}), ConfigError)

# --- tick._run_plan integration: the gate suppresses random flips, allows grounded ones ---
def _cfg_cons(**risk):
    d = {"account_number": "12345678", "dry_run": True, "kill_switch_file": "/tmp/kc_int",
         "risk": {"max_dollars_per_trade": 25, "daily_loss_halt_pct": 50.0, "daily_capital_deploy_cap": 1000,
                  "min_buying_power_buffer": 5, **risk},
         "glm": {"chat_model": "glm-5.2", "reasoner_model": "glm-5.2"}}
    return load_config(_write_config(d))

def _seed_buy(led, ticker, day, *, stop=90.0, basis="momentum", price=100.0):
    led.record_decision(trade_date=day, ticker=ticker, decided_at=f"{day}T10:00:00-04:00", signal="Buy",
                        intent="buy", decision_price=price, stop_loss=stop, basis=basis)
    led.record_event(trade_date=day, ticker=ticker, ts=f"{day}T10:00:01-04:00", signal="Buy",
                     intent="buy", status="dry_run")

_Cg = _cfg_cons()
_today = _market.trading_day_et()
# random reversal (no basis, quote above stop) -> SUPPRESSED
_lr = _tmp_ledger(); _seed_buy(_lr, "AAPL", _today)
_pl = {"run_id": "G1", "now_iso": _today + "T11:00:00-04:00", "equity": 100.0, "buying_power": 100.0,
       "positions": {"AAPL": {"quantity": 1.0, "market_value": 105.0}}, "quotes": {"AAPL": 105.0},
       "analyses": [{"ticker": "AAPL", "signal": "Sell"}]}
_lr.clear_day(_today)  # simulate a later day (lift the per-day dedup)
_op = _tick._run_plan(_Cg, _lr, _copy.deepcopy(_pl))
check("cons-plan: ungrounded reversal places NO order", [o for o in _op["orders"] if o["ticker"] == "AAPL"], [])
check_true("cons-plan: decision marked ungrounded_reversal",
           any(d.get("detail") == "consistency:ungrounded_reversal" for d in _op["decisions"]))
# stop-hit reversal (quote below stop) -> ALLOWED
_lr2 = _tmp_ledger(); _seed_buy(_lr2, "AAPL", _today)
_pl2 = _copy.deepcopy(_pl); _pl2["quotes"] = {"AAPL": 85.0}; _pl2["positions"] = {"AAPL": {"quantity": 1.0, "market_value": 85.0}}
_lr2.clear_day(_today)
_op2 = _tick._run_plan(_Cg, _lr2, _pl2)
check_true("cons-plan: stop-hit reversal IS allowed (plan trigger)",
           any(o["ticker"] == "AAPL" and o["intent"] == "sell" for o in _op2["orders"]))
# basis-change reversal -> ALLOWED
_lr3 = _tmp_ledger(); _seed_buy(_lr3, "AAPL", _today)
_pl3 = _copy.deepcopy(_pl); _pl3["analyses"] = [{"ticker": "AAPL", "signal": "Sell", "basis": "thesis_broken"}]
_lr3.clear_day(_today)
_op3 = _tick._run_plan(_Cg, _lr3, _pl3)
check_true("cons-plan: basis-change reversal IS allowed", any(o["ticker"] == "AAPL" for o in _op3["orders"]))
# disabled gate -> the same random reversal goes through (byte-compatible classic)
_Cgoff = _cfg_cons(consistency={"enabled": False})
_lr4 = _tmp_ledger(); _seed_buy(_lr4, "AAPL", _today)
_lr4.clear_day(_today)
_op4 = _tick._run_plan(_Cgoff, _lr4, _copy.deepcopy(_pl))
check_true("cons-plan: gate disabled -> reversal allowed (classic)", any(o["ticker"] == "AAPL" for o in _op4["orders"]))
# proof token persisted on the suppressed decision
_dprows = _lr.decisions_with_outcomes("AAPL", limit=3)
check_true("cons-plan: a proof_json was persisted with the decision",
           any(r.get("proof_json") for r in _dprows))

# --- REGRESSION: adversarial-review bug fixes ---
# (1) recent_completed_trades must NOT fan out on duplicate decision rows. Multiple
# decisions for the same (day,ticker,intent) + one completed event = exactly ONE row.
_lf = _tmp_ledger()
for _i in range(3):  # 3 decision rows for the SAME (day,ticker,buy)
    _lf.record_decision(trade_date="2026-06-10", ticker="DUP", decided_at=f"2026-06-10T1{_i}:00:00",
                        signal="Buy", intent="buy", decision_price=100.0)
_lf.record_event(trade_date="2026-06-10", ticker="DUP", ts="2026-06-10T13:00:00", signal="Buy",
                 intent="buy", status="dry_run")
check("regr: recent_completed_trades does NOT fan out on duplicate decisions",
      len(_lf.recent_completed_trades("DUP", 6)), 1)
# flip budget stays correct despite duplicate decisions: buy,sell,buy,sell = 3 reversals
_lf2 = _tmp_ledger()
for _d, _intent in [("2026-06-01", "buy"), ("2026-06-02", "sell"), ("2026-06-03", "buy"), ("2026-06-04", "sell")]:
    for _k in range(2):  # 2 duplicate decisions per (day,intent)
        _lf2.record_decision(trade_date=_d, ticker="FLP", decided_at=f"{_d}T1{_k}:00:00",
                             signal=("Buy" if _intent == "buy" else "Sell"), intent=_intent, decision_price=100.0)
    _lf2.record_event(trade_date=_d, ticker="FLP", ts=f"{_d}T13:00:00",
                      signal=("Buy" if _intent == "buy" else "Sell"), intent=_intent, status="dry_run")
_recent = list(reversed(_lf2.recent_completed_trades("FLP", 6)))
check("regr: flip count correct (3) despite duplicate decision rows",
      signals.count_discretionary_reversals(_recent), 3)

# (2) reconcile/rebalance SYSTEM sells must NOT be read as a discretionary stance.
_ls = _tmp_ledger()
_ls.record_event(trade_date="2026-06-10", ticker="SYS", ts="2026-06-10T13:00:00", signal="RECONCILE",
                 intent="sell", status="placed")
check("regr: a RECONCILE sell is NOT a prior discretionary stance", _ls.last_completed_trade("SYS"), None)
_ls.record_event(trade_date="2026-06-10", ticker="SYS2", ts="2026-06-10T13:00:00", signal="REBALANCE",
                 intent="sell", status="dry_run")
check("regr: REBALANCE sells excluded from recent_completed_trades", _ls.recent_completed_trades("SYS2", 6), [])

# (3) regime confirmation: a missing/None macro_reading holds the current regime (no erosion).
_lc3 = _tmp_ledger()
_g3 = _lc3.set_strategy_goal(created_at="t", target_return_pct=15, horizon_months=12, benchmark="SGOV",
    benchmark_annual_pct=3.6, constraint_note="", macro_thesis_version="v", macro_thesis_json="{}",
    active_book="dial_up_63_37", as_of="x", start_date="2026-01-01", start_equity=100.0)
_lc3.upsert_thesis_state(goal_id=_g3, as_of="2026-06-01", regime="deploy", active_book="dial_up_63_37",
                         last_trigger="DEPLOY", last_macro_json="{}", updated_at="t", regime_since="2026-06-01")
_cfg_s3b = _cfg_rebal(True, strategy_path=str(_REPO / "tests" / "fixtures" / "strategy_fixture.yaml"))
_goalrow = _lc3.get_active_goal()
_eff_none = _tick._confirm_and_persist_regime(_cfg_s3b, _lc3, _goalrow, None, "2026-06-15T10:00:00-04:00", "2026-06-15")
check("regr: a None macro_reading HOLDS the current regime (no erosion to HOLD)", _eff_none, _strat.REGIME_DEPLOY)
check_true("regr: a None reading logs NO regime change",
           len(_lc3.strategy_change_history(_g3, change_type="regime")) == 0)

# (4) dwell-lock clamp: a FUTURE regime_since (clock skew) must LOCK, not pin forever / skip.
_dw_future = _strat.regime_with_confirmation(
    {"regime": "standdown", "pending_regime": "deploy", "confirm_count": 2, "regime_since": "2026-12-31"},
    _strat.REGIME_DEPLOY, confirm_n=2, min_dwell_days=5, today="2026-06-07")
check_true("regr: a future regime_since dwell-LOCKS (clamped), does not flip", not _dw_future["changed"])
_dw_bad = _strat.regime_with_confirmation(
    {"regime": "standdown", "pending_regime": "deploy", "confirm_count": 2, "regime_since": "not-a-date"},
    _strat.REGIME_DEPLOY, confirm_n=2, min_dwell_days=5, today="2026-06-07")
check_true("regr: an unparseable regime_since dwell-LOCKS (never skips the lock)", not _dw_bad["changed"])



# --- (F8) GLM/provider client wiring tests removed: tradingagents/ is deleted,
# the EVE brain uses @ai-sdk/openai + OpenRouter directly (quiver_eve/agent/agent.ts).
# The config-parsing tests below (glm: block, legacy deepseek:, per-role providers)
# are KEPT — they test lib.config.load_config, which survives. ---

# lib/config.py reads the glm: block; legacy deepseek: still loads (fail-safe back-compat).
check("config reads glm: block", make_config().chat_model, "glm-5.2")

# (F8) build_glm_config/build_agents_config + the GLMChatOpenAI/MiniMax/factory/
# capabilities/extra_body checks removed — they tested tradingagents.llm_clients,
# which is deleted. The EVE brain wires GLM+DeepSeek via @ai-sdk/openai in
# quiver_eve/agent/agent.ts; its provider options are exercised by the live e2e.

_legacy_d = {
    "account_number": "12345678", "dry_run": True, "kill_switch_file": "/tmp/bw_kill_test",
    "risk": {"max_dollars_per_trade": 25, "daily_loss_halt_pct": 5.0,
             "daily_capital_deploy_cap": 75, "min_buying_power_buffer": 5},
    "deepseek": {"chat_model": "deepseek-v4-flash", "reasoner_model": "deepseek-v4-pro"},
}
check("config back-compat: legacy deepseek: block still loads",
      load_config(_write_config(_legacy_d)).chat_model, "deepseek-v4-flash")

# Per-role providers parse from config + survive validation (the live mixed setup).
_mix_d = {
    "account_number": "12345678", "dry_run": True, "kill_switch_file": "/tmp/bw_kill_test",
    "risk": {"max_dollars_per_trade": 25, "daily_loss_halt_pct": 5.0,
             "daily_capital_deploy_cap": 75, "min_buying_power_buffer": 5},
    "glm": {"chat_model": "deepseek-v4-flash", "chat_provider": "deepseek",
            "reasoner_model": "glm-5.2", "reasoner_provider": "glm"},
}
_mc = load_config(_write_config(_mix_d))
check("config: chat_provider=deepseek", _mc.chat_provider, "deepseek")
check("config: reasoner_provider=glm", _mc.reasoner_provider, "glm")
check("config: chat_model=deepseek-v4-flash", _mc.chat_model, "deepseek-v4-flash")
check("config: reasoner_model=glm-5.2", _mc.reasoner_model, "glm-5.2")
# Block-default inference: absent provider keys -> both default to the block's provider.
check("config: glm block, absent providers default to glm",
      (make_config().chat_provider, make_config().reasoner_provider), ("glm", "glm"))
check("config: legacy deepseek block, absent providers default to deepseek",
      (load_config(_write_config(_legacy_d)).chat_provider,
       load_config(_write_config(_legacy_d)).reasoner_provider), ("deepseek", "deepseek"))
# Unknown provider is rejected at config load (fail-fast, fail-safe).
_bad_prov_d = dict(_mix_d); _bad_prov_d["glm"] = dict(_mix_d["glm"], chat_provider="not-a-provider")
check_raises("config: unknown provider raises",
             lambda: load_config(_write_config(_bad_prov_d)), ConfigError)


# ======================= T5: ANALYSIS-DRIVEN ALLOCATION ENGINE ==============
from lib import allocate as _alloc  # noqa: E402

# --- effective_conviction (pure rating x conviction x uncertainty mapping) ---
check("conv: Buy default 0.70", _alloc.effective_conviction({"signal": "Buy"}), 0.70)
check("conv: Overweight default 0.55", _alloc.effective_conviction({"signal": "Overweight"}), 0.55)
check("conv: explicit conviction wins", _alloc.effective_conviction({"signal": "Buy", "conviction": 90}), 0.90)
check("conv: Hold -> None (keep prior)", _alloc.effective_conviction({"signal": "Hold"}), None)
check("conv: Sell -> 0.0 (exit signal)", _alloc.effective_conviction({"signal": "Sell"}), 0.0)
check("conv: ERROR -> None (D2 hold prior)", _alloc.effective_conviction({"signal": "ERROR"}), None)
check("conv: missing analysis -> None", _alloc.effective_conviction(None), None)
check("conv: Underweight halved", _alloc.effective_conviction({"signal": "Underweight", "conviction": 40}), 0.20)
check("conv: uncertainty damps", round(_alloc.effective_conviction(
    {"signal": "Buy", "conviction": 80, "uncertainty": 50}), 4), 0.6)  # 0.8 * (1 - 0.5*0.5)

# --- allocate_targets: conviction scales weight; cash is the residual --------
_POL = {"per_name_max_pct": 80, "sleeve_max_pct": 90, "cash_floor_pct": 10,
        "smoothing_alpha": 1.0, "min_weight_floor_pct": 0.1}  # alpha=1 -> no smoothing (test fresh)
_cands2 = [{"ticker": "AAA", "sleeve": "x", "prior_weight": 0}, {"ticker": "BBB", "sleeve": "y", "prior_weight": 0}]
_al = _alloc.allocate_targets(_cands2, {"AAA": {"signal": "Buy", "conviction": 90},
                                        "BBB": {"signal": "Buy", "conviction": 30}}, policy=_POL)
check_true("alloc: higher conviction gets more weight", _al.weights["AAA"] > _al.weights["BBB"])
check_true("alloc: engine sums to deployable budget (90)", abs(sum(_al.weights.values()) - 90.0) < 0.01)
check_true("alloc: cash is the residual (~10)", abs(_al.cash_pct - 10.0) < 0.01)

# --- F3: apply_target_hysteresis (material-change gate on the conviction target) ------------
# Keep a name at its PRIOR (static book) weight unless the fresh reading moved it >= min_delta;
# REVERT (not blend) on a sub-threshold move so a persistent tiny lean can't slow-accumulate.
_hy = _alloc.apply_target_hysteresis
check("F3 hyst: sub-delta keeps prior (|14-15|=1 < 2)", _hy({"AAA": 14.0}, {"AAA": 15.0}, 2.0), {"AAA": 15.0})
check("F3 hyst: at-threshold applies new (|13-15|=2, NOT < 2)", _hy({"AAA": 13.0}, {"AAA": 15.0}, 2.0), {"AAA": 13.0})
check("F3 hyst: supra-delta applies new", _hy({"AAA": 9.0}, {"AAA": 15.0}, 2.0), {"AAA": 9.0})
check("F3 hyst: fresh add (no prior) takes new weight", _hy({"NEW": 5.0}, {}, 2.0), {"NEW": 5.0})
check("F3 hyst: min_delta 0 disables (identity)", _hy({"AAA": 14.0}, {"AAA": 15.0}, 0.0), {"AAA": 14.0})
check("F3 hyst: case-insensitive prior lookup", _hy({"AAA": 14.0}, {"aaa": 15.0}, 2.0), {"AAA": 15.0})
# revert-not-blend: a 1pt daily nudge never walks the target off its static anchor.
check("F3 hyst: revert prevents 1pt/day accumulation", _hy({"AAA": 14.0}, {"AAA": 15.0}, 2.0), {"AAA": 15.0})

# --- D2: ERROR / missing HOLDS prior weight, never sells to cash -------------
_alD2 = _alloc.allocate_targets(
    [{"ticker": "SMH", "sleeve": "c", "prior_weight": 15}, {"ticker": "CEG", "sleeve": "p", "prior_weight": 9}],
    {"SMH": {"signal": "ERROR"}, "CEG": {"signal": "Buy", "conviction": 80}}, policy=_POL)
check("D2: ERROR name holds its prior weight (no sell)", _alD2.weights["SMH"], 15.0)
check_true("D2: a healthy name still gets allocated alongside", _alD2.weights["CEG"] > 0)
_alMiss = _alloc.allocate_targets([{"ticker": "BOT", "sleeve": "r", "prior_weight": 5}], {}, policy=_POL)
check("D2: missing analysis holds prior", _alMiss.weights.get("BOT"), 5.0)

# --- Hold keeps prior; Sell goes to 0 ---------------------------------------
_alHold = _alloc.allocate_targets(
    [{"ticker": "QQQ", "sleeve": "h", "prior_weight": 8}, {"ticker": "ANET", "sleeve": "n", "prior_weight": 6}],
    {"QQQ": {"signal": "Hold"}, "ANET": {"signal": "Sell", "conviction": 70}}, policy=_POL)
check("Hold keeps prior weight", _alHold.weights.get("QQQ"), 8.0)
check("Sell drops the name to 0", _alHold.weights.get("ANET", 0.0), 0.0)

# --- cash floor is a HARD floor even with many max-conviction names ----------
_alFloor = _alloc.allocate_targets(
    [{"ticker": t, "sleeve": t, "prior_weight": 0} for t in ("A", "B", "C", "D")],
    {t: {"signal": "Buy", "conviction": 100} for t in ("A", "B", "C", "D")},
    policy={"per_name_max_pct": 50, "sleeve_max_pct": 100, "cash_floor_pct": 10, "smoothing_alpha": 1.0})
check_true("cash floor: engine never exceeds 100 - cash_floor", sum(_alFloor.weights.values()) <= 90.0 + 1e-6)

# --- per-name cap clips a dominant conviction; excess water-fills others -----
_alCap = _alloc.allocate_targets(
    [{"ticker": "BIG", "sleeve": "a", "prior_weight": 0}, {"ticker": "SM1", "sleeve": "b", "prior_weight": 0},
     {"ticker": "SM2", "sleeve": "c", "prior_weight": 0}],
    {"BIG": {"signal": "Buy", "conviction": 100}, "SM1": {"signal": "Buy", "conviction": 10},
     "SM2": {"signal": "Buy", "conviction": 10}},
    policy={"per_name_max_pct": 30, "sleeve_max_pct": 100, "cash_floor_pct": 0, "smoothing_alpha": 1.0})
check_true("per-name cap clips the dominant name to <=30", _alCap.weights["BIG"] <= 30.0 + 1e-6)
check_true("water-fill pushes excess onto the smaller names", _alCap.weights["SM1"] > 10.0)

# --- per-sleeve cap limits concentration into one sleeve --------------------
_alSleeve = _alloc.allocate_targets(
    [{"ticker": "P1", "sleeve": "Power", "prior_weight": 0}, {"ticker": "P2", "sleeve": "Power", "prior_weight": 0},
     {"ticker": "C1", "sleeve": "Compute", "prior_weight": 0}],
    {"P1": {"signal": "Buy", "conviction": 90}, "P2": {"signal": "Buy", "conviction": 90},
     "C1": {"signal": "Buy", "conviction": 20}},
    policy={"per_name_max_pct": 100, "sleeve_max_pct": 40, "cash_floor_pct": 0, "smoothing_alpha": 1.0})
check_true("sleeve cap: Power sleeve total <= 40",
           _alSleeve.weights.get("P1", 0) + _alSleeve.weights.get("P2", 0) <= 40.0 + 1e-6)

# --- EWMA smoothing: a big fresh change is damped toward prior (stickiness) --
_alSmooth = _alloc.allocate_targets(
    [{"ticker": "X", "sleeve": "x", "prior_weight": 20}],
    {"X": {"signal": "Underweight", "conviction": 1}},  # fresh wants ~0
    policy={"per_name_max_pct": 100, "sleeve_max_pct": 100, "cash_floor_pct": 0, "smoothing_alpha": 0.4})
check_true("EWMA: a one-day collapse is damped (stays well above 0)", _alSmooth.weights["X"] > 10.0)

# --- calibration multiplier: a learned-winner gets more than an equal peer ---
_alCal = _alloc.allocate_targets(
    [{"ticker": "W", "sleeve": "a", "prior_weight": 0}, {"ticker": "L", "sleeve": "b", "prior_weight": 0}],
    {"W": {"signal": "Buy", "conviction": 60}, "L": {"signal": "Buy", "conviction": 60}},
    calibration={"W": 1.5, "L": 1.0}, policy={"per_name_max_pct": 100, "sleeve_max_pct": 100,
                                              "cash_floor_pct": 0, "smoothing_alpha": 1.0})
check_true("calibration: the learned winner gets more weight", _alCal.weights["W"] > _alCal.weights["L"])

# --- item 4: Underweight is REDUCE-ONLY (renormalization can't inflate a lean-smaller name) ---
_POLro = {"per_name_max_pct": 80, "sleeve_max_pct": 90, "cash_floor_pct": 5, "smoothing_alpha": 1.0}
# The churn case: a lone Underweight on a concentrated book. Pre-fix the freed budget
# water-filled UP into it (SOXX 7% prior -> a ~80% target here); now it is capped at prior.
_alRO = _alloc.allocate_targets(
    [{"ticker": "SOXX", "sleeve": "c", "prior_weight": 7},
     {"ticker": "SMH", "sleeve": "c", "prior_weight": 15}],
    {"SOXX": {"signal": "Underweight", "conviction": 58}, "SMH": {"signal": "ERROR"}}, policy=_POLro)
check_true("item4: lone Underweight never exceeds prior (no upward inflation)",
           _alRO.weights["SOXX"] <= 7.0 + 1e-6)
# Underweight still REDUCES below prior when it shares the budget with a stronger add.
_alRO2 = _alloc.allocate_targets(
    [{"ticker": "UW", "sleeve": "a", "prior_weight": 20}, {"ticker": "BUY", "sleeve": "b", "prior_weight": 0}],
    {"UW": {"signal": "Underweight", "conviction": 10}, "BUY": {"signal": "Buy", "conviction": 95}}, policy=_POLro)
check_true("item4: Underweight still reduces below prior when outbid", _alRO2.weights["UW"] < 20.0)
# A capped Buy peer's spillover must NOT water-fill into the Underweight name.
_alRO3 = _alloc.allocate_targets(
    [{"ticker": "UW", "sleeve": "a", "prior_weight": 5}, {"ticker": "BIG", "sleeve": "b", "prior_weight": 0}],
    {"UW": {"signal": "Underweight", "conviction": 50}, "BIG": {"signal": "Buy", "conviction": 100}},
    policy={"per_name_max_pct": 30, "sleeve_max_pct": 100, "cash_floor_pct": 0, "smoothing_alpha": 1.0})
check_true("item4: Underweight excluded from water-fill (stays <= prior)", _alRO3.weights.get("UW", 0.0) <= 5.0 + 1e-6)
# A genuine Buy is untouched — reduce-only never blocks an add growing above its prior.
_alRO4 = _alloc.allocate_targets(
    [{"ticker": "ADD", "sleeve": "a", "prior_weight": 5}],
    {"ADD": {"signal": "Buy", "conviction": 90}}, policy=_POLro)
check_true("item4: a Buy still grows above prior (reduce-only doesn't touch adds)", _alRO4.weights["ADD"] > 5.0)

# --- F3: conviction_deploy_factor (book-conviction deployment scalar; reallocation teeth) -----
# A broadly-BEARISH book deploys less -> raises cash; a bullish book deploys full. Takes RAW
# effective_conviction (0..1) so curve_k/calibration stay deployment-neutral. Defaults: full
# deploy at mean 0.45, floor 0.70.
_cdf = _alloc.conviction_deploy_factor
check("F3 dep: bullish book (Buy 0.70) -> full deploy 1.0", _cdf([0.70, 0.70, 0.70]), 1.0)
check("F3 dep: all-bearish book (Underweight ~0.175) -> floor 0.70", _cdf([0.175, 0.175, 0.175]), 0.70)
_mixf = _cdf([0.50, 0.30])  # mean 0.40 -> 0.40/0.45 = 0.888...
check_true("F3 dep: mixed book strictly between floor and 1.0", 0.70 < _mixf < 1.0)
check("F3 dep: mixed value == mean/ref", round(_mixf, 4), round((0.40 / 0.45), 4))
check("F3 dep: empty (no fresh conviction) -> 1.0", _cdf([]), 1.0)
check("F3 dep: None values filtered", _cdf([None, 0.70, None]), 1.0)
check_true("F3 dep: monotone (weaker book <= stronger book)", _cdf([0.30, 0.30]) <= _cdf([0.55, 0.55]))
check("F3 dep: min_factor=1.0 DISABLES F3 (always full deploy)",
      _cdf([0.10, 0.10], {"conviction_deploy_min_factor": 1.0}), 1.0)
check("F3 dep: curve_k/calibration are deployment-neutral (RAW ec in)",
      _cdf([0.175, 0.175], {"conviction_curve_k": 2.0, "smoothing_alpha": 0.9}), _cdf([0.175, 0.175]))
check("F3 dep: ref<=0 guard -> 1.0", _cdf([0.10], {"conviction_full_deploy_at": 0.0}), 1.0)

# --- regime scalar: STAND_DOWN derisks (more cash) --------------------------
_alRegime = _alloc.allocate_targets(
    [{"ticker": "R", "sleeve": "a", "prior_weight": 0}], {"R": {"signal": "Buy", "conviction": 100}},
    regime_scalar=0.5, policy={"per_name_max_pct": 100, "sleeve_max_pct": 100, "cash_floor_pct": 10,
                               "smoothing_alpha": 1.0, "regime_min_factor": 0.5})
check_true("regime: derisk deploys less -> more cash", _alRegime.cash_pct > 10.0)

# --- D3: a hold_floor (suppressed reversal) can't be cut below prior ---------
_alD3 = _alloc.allocate_targets(
    [{"ticker": "SMH", "sleeve": "c", "prior_weight": 15}],
    {"SMH": {"signal": "Sell", "conviction": 80}},   # wants 0, but the gate suppressed the reversal
    hold_floors={"SMH": 15.0}, policy={"per_name_max_pct": 40, "sleeve_max_pct": 100,
                                       "cash_floor_pct": 0, "smoothing_alpha": 1.0})
check("D3: suppressed reversal holds prior weight (not sold)", _alD3.weights.get("SMH"), 15.0)

# --- conviction curve k>1 sharpens (concentrates into the best idea) ---------
_eqpol = {"per_name_max_pct": 100, "sleeve_max_pct": 100, "cash_floor_pct": 0, "smoothing_alpha": 1.0}
_lin = _alloc.allocate_targets(
    [{"ticker": "H", "sleeve": "a", "prior_weight": 0}, {"ticker": "Lo", "sleeve": "b", "prior_weight": 0}],
    {"H": {"signal": "Buy", "conviction": 80}, "Lo": {"signal": "Buy", "conviction": 40}},
    policy={**_eqpol, "conviction_curve_k": 1.0})
_sharp = _alloc.allocate_targets(
    [{"ticker": "H", "sleeve": "a", "prior_weight": 0}, {"ticker": "Lo", "sleeve": "b", "prior_weight": 0}],
    {"H": {"signal": "Buy", "conviction": 80}, "Lo": {"signal": "Buy", "conviction": 40}},
    policy={**_eqpol, "conviction_curve_k": 2.0})
check_true("conviction curve k>1 concentrates into the higher-conviction name",
           _sharp.weights["H"] > _lin.weights["H"])


# ---------------------------------------------------------------------------
# Pipeline-decided sizing + dynamic universe (Q1/Q3). Pure offline units; the
# tick-level construct/ADD flows live in tests/test_e2e_pipeline.py.
# ---------------------------------------------------------------------------
from lib import calibrate as _calib  # noqa: E402
from lib import universe as _univ  # noqa: E402
from lib import screener as _scr  # noqa: E402

# --- calibration multiplier (Q1): directionally correct, low-N -> 1.0, clamped ---
def _crows(ret, n, intent="buy", signal="Buy"):
    return [{"intent": intent, "signal": signal, "directional_return": ret} for _ in range(n)]
check_true("calib: all-hit (n>=min) -> multiplier > 1", _calib.calibration_multiplier(_crows(0.05, 6)) > 1.0)
check_true("calib: all-miss -> multiplier < 1", _calib.calibration_multiplier(_crows(-0.05, 6)) < 1.0)
check("calib: low-N (< min_n) -> exactly 1.0", _calib.calibration_multiplier(_crows(0.05, 3)), 1.0)
check("calib: skips excluded -> 1.0", _calib.calibration_multiplier(_crows(0.05, 6, intent="skip")), 1.0)
check_true("calib: perfect record clamped to <= hi", _calib.calibration_multiplier(_crows(0.05, 100)) <= 1.5)
check_true("calib: terrible record clamped to >= lo", _calib.calibration_multiplier(_crows(-0.05, 100)) >= 0.5)

# --- universe apply_add / conserve_to_cash / validate_book guard (Q3) ---
_ubook = [{"sleeve": "Tech", "ticker": "AAA", "target_weight": 30, "band": 5, "status": "active", "quotable": 1},
          {"sleeve": "Cash", "ticker": "SGOV", "target_weight": 70, "band": 0, "status": "active", "quotable": 1}]
_rows_add, _applied, _why = _univ.apply_add(_ubook, "BBB", "Tech", 10, 4)
check("apply_add: applies the proposed weight", _applied, 10.0)
check("apply_add: conserves book to ~100", round(sum(r["target_weight"] for r in _rows_add if r["status"] != "removed"), 2), 100.0)
check("apply_add: funds from cash (70 -> 60)", [r for r in _rows_add if r["ticker"] == "SGOV"][0]["target_weight"], 60.0)
check_true("apply_add: new ticker is now in the analysis universe", "BBB" in _univ.tradable_universe(_rows_add))
_rows_no, _applied_no, _why_no = _univ.apply_add(_ubook, "BBB", "Tech", 90, 4)  # cash only 70
check("apply_add: insufficient cash -> applied 0 (refused)", _applied_no, 0.0)
check_true("apply_add: insufficient cash -> reason explains", "insufficient" in _why_no)
check("conserve_to_cash: +delta frees to cash", _univ.conserve_to_cash(_ubook, 5)[1]["target_weight"], 75.0)
check("conserve_to_cash: -delta funds from cash", _univ.conserve_to_cash(_ubook, -5)[1]["target_weight"], 65.0)
_negbook = [{"ticker": "AAA", "target_weight": 60, "band": 5, "status": "active", "sleeve": "Tech"},
            {"ticker": "BBB", "target_weight": 45, "band": 5, "status": "active", "sleeve": "Tech"},
            {"ticker": "SGOV", "target_weight": -5, "band": 0, "status": "active", "sleeve": "Cash"}]
check_true("validate_book: rejects a negative weight (sum ~100)", _univ.validate_book(_negbook)[0] is False)

# --- screener (Q3): allow-list/held/sector/mcap/pe filter + momentum rank + fail-safe ---
def _prov(screen):
    return [{"ticker": "NVDA", "sector": "Technology", "market_cap": 2e12, "pe": 45, "momentum": 0.3},
            {"ticker": "AMD", "sector": "Technology", "market_cap": 2e11, "pe": 50, "momentum": 0.1},
            {"ticker": "HELD", "sector": "Technology", "market_cap": 3e11, "pe": 30, "momentum": 0.9},
            {"ticker": "SMALL", "sector": "Technology", "market_cap": 1e9, "pe": 10, "momentum": 0.9},
            {"ticker": "NOTLIST", "sector": "Technology", "market_cap": 9e11, "pe": 20, "momentum": 0.9}]
_sleeves = {"Tech": {"screen": {"sector": "Technology", "market_cap_min": 50e9, "pe_max": 60}}, "Cash": {}}
_cands = _scr.screen_book(_sleeves, ["NVDA", "AMD", "HELD"], ["HELD"], _prov, top_n_per_sleeve=3)
check("screener: keeps allow-listed, unheld, screen-passing; ranked by momentum",
      [c["ticker"] for c in _cands], ["NVDA", "AMD"])
check("screener: provider error -> [] (fail-safe, universe never grows on bad data)",
      _scr.screen_book(_sleeves, ["NVDA"], [], (lambda s: (_ for _ in ()).throw(RuntimeError("down")))), [])
check("screener: a sleeve with no `screen` is skipped",
      _scr.screen_book({"Cash": {}}, ["NVDA"], [], _prov), [])


# --- EVE migration: lib/rating + lib/levers + the EVE-markdown->fields contract --
# These pin the brain-swap's pure-Python seams WITHOUT burning tokens. The wall is
# untouched; the EVE brain (Node) is exercised by tests/test_e2e_live.py.

# lib/rating: the 5-tier signal derivation ported from tradingagents (survives F8).
from lib import rating as _rating  # noqa: E402
check("rating: explicit Rating label -> 5-tier", _rating.parse_rating("**Rating**: Buy"), "Buy")
check("rating: label tolerant of markdown bold", _rating.parse_rating("blah\n**Rating**: **Overweight**\n"), "Overweight")
check("rating: falls back to first 5-tier word", _rating.parse_rating("we rate this a Hold for now"), "Hold")
check("rating: no rating word -> default Hold", _rating.parse_rating("nothing here"), "Hold")
check("rating: default arg respected", _rating.parse_rating("nothing", default="Sell"), "Sell")

# lib/levers: the self-learning registry. Pure, wall-side, Python owns the SQLite.
from lib import levers as _lev  # noqa: E402
_levled = _tmp_ledger()
with _levled._conn() as _c:
    _lev.ensure_schema(_c)
    # propose is idempotent on name
    _lid = _lev.propose(_c, "options_flow", "data_source", {"feed": "odd_lot"}, "test")
    _lid2 = _lev.propose(_c, "options_flow", "data_source", {}, "dup")
    check("levers: propose idempotent on name", _lid, _lid2)
    check("levers: proposal starts as proposed", _c.execute("SELECT status FROM eval_levers WHERE id=?", (_lid,)).fetchone()[0], "proposed")
    # active list empty until activated
    check("levers: no active levers before activate", _lev.active_levers(_c), [])
    _lev.activate(_c, _lid)
    check("levers: active after activate", [l.name for l in _lev.active_levers(_c)], ["options_flow"])
    # record_use is idempotent per (lever, decision); bumps decisions_used
    _lev.record_use(_c, _lid, 100)
    _lev.record_use(_c, _lid, 100)  # dup -> no double count
    _lev.record_use(_c, _lid, 101)
    check("levers: record_use idempotent + counts", [l.decisions_used for l in _lev.active_levers(_c)], [2])
    # scoring: underperforming lever auto-retires after min_decisions
    for _did in (102, 103, 104, 105, 106, 107):
        _lev.record_use(_c, _lid, _did)
        _lev.score_lever(_c, _lid, _did, lever_alpha=-0.05, baseline_alpha=0.0,
                         min_decisions=5, retire_alpha=-0.02)
    _st = _c.execute("SELECT status, score FROM eval_levers WHERE id=?", (_lid,)).fetchone()
    check("levers: underperforming lever auto-retires", _st[0], "retired")
    check_true("levers: retired score is negative", float(_st[1]) <= -0.02)
    # a kept (positive) lever
    _lid3 = _lev.propose(_c, "good_lever", "analysis_angle", {}, "good")
    _lev.activate(_c, _lid3)
    for _did in (200, 201, 202, 203, 204):
        _lev.record_use(_c, _lid3, _did)
        _lev.score_lever(_c, _lid3, _did, lever_alpha=0.03, baseline_alpha=0.0,
                         min_decisions=5, retire_alpha=-0.02)
    check("levers: positive lever -> kept", _c.execute("SELECT status FROM eval_levers WHERE id=?", (_lid3,)).fetchone()[0], "kept")
    # render_active_block: compact text for past_context; empty when none active
    _blk = _lev.render_active_block(_c)
    check_true("levers: render_active_block names active levers", "good_lever" in _blk)
    _c.execute("UPDATE eval_levers SET status='retired' WHERE id=?", (_lid3,))
    check("levers: render_active_block empty when none active", _lev.render_active_block(_c), "")

# The EVE-markdown -> fields contract: analyze.py:parse_trader_plan + _split_eve_markdown
# + extract_fields on a fixture carrying all 12 labels + 7 sections. Pins the load-bearing
# seam without an LLM. (The live e2e exercises the real EVE brain.)
import analyze as _az  # noqa: E402
_EVE_FIXTURE = """## market_report
Price closed 195.1, volume up 12%, RSI 62.

## sentiment_report
StockTwits (24 msgs): mildly bullish.

## news_report
Earnings beat; guidance raised.

## fundamentals_report
PE 28, P/B 6.1, market cap 3.0e11.

## trader_investment_plan
**Action**: Buy
**Entry Price**: 195.1
**Stop Loss**: 182.5
**Position Sizing**: ~4.5% of capital
**Position Pct**: 4.5
**Strategy Basis**: momentum_breakout
**Catalyst**: none
**Target Price**: 210

## final_trade_decision
**Rating**: Buy
**Next Review Hours**: 24
**Conviction**: 72
**Uncertainty**: 30
Executive summary: momentum intact, beat + raised.

## lever_proposals
- [data_source] options_flow: add odd-lot flow as a confirmation input
"""
_split = _az._split_eve_markdown(_EVE_FIXTURE)
check("eve-split: market_report section", "195.1" in _split.get("market_report", ""), True)
check("eve-split: final_trade_decision section", "Executive summary" in _split.get("final_trade_decision", ""), True)
_plan = _az.parse_trader_plan(_split["trader_investment_plan"])
check("eve-contract: Action parsed", _plan["action"], "Buy")
check("eve-contract: Entry Price parsed", _plan["entry_price"], 195.1)
check("eve-contract: Stop Loss parsed", _plan["stop_loss"], 182.5)
check("eve-contract: Position Pct parsed", _plan["position_pct"], 4.5)
check("eve-contract: Strategy Basis parsed + non-empty", _plan["basis"], "momentum_breakout")
check("eve-contract: Target Price parsed", _plan["target_price"], 210.0)
_fields = _az.extract_fields(_split, _rating.parse_rating(_split["final_trade_decision"]), "AAPL")
check("eve-contract: signal derived from Rating label", _fields["signal"], "Buy")
check("eve-contract: conviction parsed", _fields["conviction"], 72.0)
check("eve-contract: next_review_hours parsed", _fields["next_review_hours"], 24.0)
check("eve-contract: schema=1", _fields["schema"], 1)

# Fail-SAFE: a fixture with missing market_report -> core_available false -> ERROR override.
_BAD = _EVE_FIXTURE.replace("## market_report\nPrice closed 195.1, volume up 12%, RSI 62.\n",
                            "## market_report\nUNAVAILABLE: no price data\n")
_bad_split = _az._split_eve_markdown(_BAD)
_bad_fields = _az.extract_fields(_bad_split, _rating.parse_rating(_bad_split["final_trade_decision"]), "AAPL")
check("eve-contract: missing core data -> ERROR (fail-safe)", _bad_fields["signal"], "ERROR")
check_true("eve-contract: ERROR carries reason", "core data unavailable" in (_bad_fields["error"] or ""))

# Missing-label fixture -> the TS validator exits 1 (asserted by quiver_eve/test); here we
# only assert parse_trader_plan returns None for a missing label (the Python side of the gate).
_NO_BASIS = _EVE_FIXTURE.replace("**Strategy Basis**: momentum_breakout\n", "")
check("eve-contract: missing basis -> None (gate suppresses the reversal)", _az.parse_trader_plan(_NO_BASIS.split("## trader_investment_plan")[1])["basis"], None)


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


# === F3: _split_eve_markdown section tolerance ==============================
def _contract_md(*, with_trend=True, basis="multi-qtr uptrend thesis",
                 rating="Buy", extra_trend_subheading=False):
    p = ["## market_report", "price data here", ""]
    if with_trend:
        p += ["## trend_report", "1y return: +12%"]
        if extra_trend_subheading:
            # SINGLE-word `##` subheading is the real splitter risk (multi-word
            # `## Drawdown Metrics` does NOT match the single-word regex). This
            # is why lib.trend renders with `###` not `##`.
            p += ["## Drawdown", "max DD -18%"]
        p += [""]
    p += ["## sentiment_report", "bullish", "", "## news_report", "news", "",
          "## fundamentals_report", "fundy", "", "## trader_investment_plan",
          "**Action**: " + rating, "**Entry Price**: 100", "**Stop Loss**: 90",
          "**Position Sizing**: ~5%", "**Position Pct**: 5",
          f"**Strategy Basis**: {basis}", "**Catalyst**: none", "**Target Price**: 120", "",
          "## final_trade_decision", f"**Rating**: {rating}",
          "**Next Review Hours**: 24", "**Conviction**: 70", "**Uncertainty**: 30", "",
          "## lever_proposals", "none"]
    return "\n".join(p)

_split_with = _az._split_eve_markdown(_contract_md(with_trend=True))
check_true("F3: trend_report kept when present", "trend_report" in _split_with and "1y return" in (_split_with.get("trend_report") or ""))
_split_without = _az._split_eve_markdown(_contract_md(with_trend=False))
check_true("F3: missing trend_report tolerated", "trend_report" not in _split_without and "final_trade_decision" in _split_without)
_split_bad = _az._split_eve_markdown(_contract_md(with_trend=True, extra_trend_subheading=True))
check_true("F3: stray ## subheading truncates trend body (pins the render rule)", "Drawdown" not in (_split_bad.get("trend_report") or ""))


# === F6: Python contract validator (binding fail-safe gate) =================
def _trader_block(form="outer"):
    """form='outer' -> `**Label**: value` ; 'inner' -> `**Label:** value`.
    Labels are written as `**Label` + SEP + value where SEP closes the bold."""
    sep = "**: " if form == "outer" else ":** "
    return (f"**Action{sep}Buy\n**Entry Price{sep}100\n**Stop Loss{sep}90\n"
            f"**Position Sizing{sep}~5%\n**Position Pct{sep}5\n"
            f"**Strategy Basis{sep}uptrend thesis\n**Catalyst{sep}none\n**Target Price{sep}120")

def _pm_block(form="outer"):
    sep = "**: " if form == "outer" else ":** "
    return f"**Rating{sep}Buy\n**Next Review Hours{sep}24\n**Conviction{sep}70\n**Uncertainty{sep}30"

# canonical `**Label**: value`
_az._validate_contract({"trader_investment_plan": _trader_block("outer"), "final_trade_decision": _pm_block("outer")})
PASS += 1
# colon-inside-bold `**Label:** value` (Gate-B: validate.ts rejects this; grab accepts -> gate MUST too)
_az._validate_contract({"trader_investment_plan": _trader_block("inner"), "final_trade_decision": _pm_block("inner")})
PASS += 1
# colon-inside-bold `**Label:** value` (Gate-B: validate.ts rejects this; grab accepts -> gate MUST too)
_inner_plan = "\n".join(f"**{lbl}:** {v}" for lbl, v in [
    ("Action", "Buy"), ("Entry Price", "100"), ("Stop Loss", "90"),
    ("Position Sizing", "~5%"), ("Position Pct", "5"),
    ("Strategy Basis", "uptrend thesis"), ("Catalyst", "none"), ("Target Price", "120")])
_inner_pm = "\n".join(f"**{lbl}:** {v}" for lbl, v in [
    ("Rating", "Buy"), ("Next Review Hours", "24"), ("Conviction", "70"), ("Uncertainty", "30")])
_az._validate_contract({"trader_investment_plan": _inner_plan, "final_trade_decision": _inner_pm})
PASS += 1

check_raises("F6: empty basis raises", lambda: _az._validate_contract(
    {"trader_investment_plan": _trader_block("outer").replace("uptrend thesis", "none"),
     "final_trade_decision": _pm_block("outer")}), RuntimeError)
check_raises("F6: missing label raises", lambda: _az._validate_contract(
    {"trader_investment_plan": _trader_block("outer"),
     "final_trade_decision": "**Rating**: Buy\n**Next Review Hours**: 24\n**Uncertainty**: 30"}),
    RuntimeError)


# === F2: whole-pipeline fallback retry (analyze.py) ========================
# The brain is non-deterministic; a 2nd run often succeeds. _run_eve_with_fallback
# re-runs ONCE on failure, bounded by a per-ticker deadline. Proves: fail-then-
# succeed recovers; persistent-fail -> ERROR; deadline-skip -> ERROR (no doomed run).
import analyze as _az2

class _FakeCfg:
    brain_engine = "eve"

def _fake_run_eve(scripts):
    """Return a fn that runs a sequence of (result|exception) scripts, one per call."""
    calls = {"n": 0}
    def _run(ticker, date, cfg, pc, pcc):
        i = calls["n"]; calls["n"] += 1
        s = scripts[i] if i < len(scripts) else scripts[-1]
        if isinstance(s, Exception):
            raise s
        return {"final_trade_decision": "**Rating**: Buy", "trader_investment_plan": "**Strategy Basis**: x",
                "_reasoning_tokens": 100, **s}
    return _run, calls

# (a) fail-then-succeed: run-1 raises, run-2 succeeds -> recovered, fallback_triggered=True
_run, calls = _fake_run_eve([RuntimeError("missing sections"), {}])
_az2._run_eve_analysis = _run
import time as _time
out = _az2._run_eve_with_fallback("BOT", "2026-07-07", _FakeCfg(), "", "", deadline_s=_time.monotonic() + 3600)
check("F2: fail-then-succeed recovers", out.get("signal") if "signal" in out else "Buy", "Buy")
check("F2: fallback ran exactly once (2 calls)", calls["n"], 2)
check_true("F2: fallback_triggered flagged on recovery", out.get("fallback_triggered") is True)

# (b) persistent fail (2x): run-1 + run-2 both raise -> ERROR propagated
_run, calls = _fake_run_eve([RuntimeError("missing sections"), RuntimeError("contract violation: missing labels")])
_az2._run_eve_analysis = _run
raised = False
try:
    _az2._run_eve_with_fallback("CEG", "2026-07-07", _FakeCfg(), "", "", deadline_s=_time.monotonic() + 3600)
except RuntimeError as e:
    raised = True
    err = str(e)
check_true("F2: persistent fail -> RuntimeError", raised)
check_true("F2: persistent fail mentions both modes", "run1=" in err and "run2=" in err)

# (c) deadline-skip: < 600s remaining -> NO fallback, ERROR (don't start a doomed run)
_run, calls = _fake_run_eve([RuntimeError("missing sections"), {}])
_az2._run_eve_analysis = _run
raised = False
try:
    _az2._run_eve_with_fallback("URA", "2026-07-07", _FakeCfg(), "", "", deadline_s=_time.monotonic() + 100)  # 100s < 600 floor
except RuntimeError:
    raised = True
check_true("F2: deadline-skip -> ERROR (no fallback)", raised)
check("F2: deadline-skip ran run-1 only (no fallback)", calls["n"], 1)

# (d) no deadline (manual run): fallback always attempted
_run, calls = _fake_run_eve([RuntimeError("turn_threw"), {}])
_az2._run_eve_analysis = _run
out = _az2._run_eve_with_fallback("X", "2026-07-07", _FakeCfg(), "", "", deadline_s=None)
check_true("F2: no-deadline fallback recovers", out.get("fallback_triggered") is True)

# (e) F0: main() MUST forward the ABSOLUTE monotonic deadline UNCHANGED (regression for the
# double-subtraction that made `remaining` always negative -> fallback ALWAYS skipped, the
# real 07-13 BOT/VRT "no fallback (deadline)" error). run_analyses passes monotonic()+timeout.
_dl_abs = _time.monotonic() + 3600
check("F0: _resolve_deadline_s passes the absolute deadline through", _az2._resolve_deadline_s(_dl_abs), _dl_abs)
check_true("F0: None deadline stays None", _az2._resolve_deadline_s(None) is None)
check_true("F0: falsy (0) deadline -> None", _az2._resolve_deadline_s(0) is None)
# Convention proof: (forwarded - monotonic()) is the REAL remaining budget (~3600 >> 600 FLOOR).
# The old bug forwarded a ~3600 RELATIVE value, then downstream subtracted monotonic() again -> < 0.
check_true("F0: forwarded deadline yields POSITIVE remaining (fallback not wrongly skipped)",
           (_az2._resolve_deadline_s(_dl_abs) - _time.monotonic()) > 600.0)
# End-to-end: a first-run failure under the value main() forwards ATTEMPTS the fallback (2 calls);
# under the old double-subtraction this skips at run-1 (calls==1) -> this test goes red.
_run, calls = _fake_run_eve([RuntimeError("turn_threw"), {}])
_az2._run_eve_analysis = _run
out = _az2._run_eve_with_fallback("F0T", "2026-07-07", _FakeCfg(), "", "",
                                  deadline_s=_az2._resolve_deadline_s(_time.monotonic() + 3600))
check("F0: fallback FIRES under a healthy forwarded deadline (2 calls)", calls["n"], 2)
check_true("F0: fallback recovered (not wrongly skipped)", out.get("fallback_triggered") is True)

# F3: error_mode classification
check("F3: missing_sections classified", _az2._classify_failure(RuntimeError("EVE output missing final_trade_decision/trader_investment_plan sections")), "missing_sections")
check("F3: contract_violation classified", _az2._classify_failure(RuntimeError("contract violation: missing labels")), "contract_violation")
check("F3: timeout classified", _az2._classify_failure(RuntimeError("analyze.py timed out after 3600s")), "timeout")
check("F3: turn_threw classified", _az2._classify_failure(RuntimeError("EVE brain (decide.mjs) exited 1")), "turn_threw")


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

# BUG 2 — fundamentals unit normalization (fractions->%, div-yield %, D/E ratio x).
import lib.dataflows.y_finance as _yfm  # noqa: E402
class _FakeInfoTicker:
    def __init__(self, *a, **k): pass
    @property
    def info(self):
        return {"longName": "Test Co", "trailingPE": 20.0, "dividendYield": 2.7,
                "profitMargins": 0.218, "returnOnEquity": 0.057,
                "operatingMargins": 0.172, "returnOnAssets": 0.0125,
                "debtToEquity": 76.592, "marketCap": 1000}
_orig_ticker_fn = _yfm.yf.Ticker
_yfm.yf.Ticker = _FakeInfoTicker
try:
    _fund_md = _yfm.get_fundamentals("TEST")
finally:
    _yfm.yf.Ticker = _orig_ticker_fn
check_true("dh: fund div-yield labeled %", "Dividend Yield: 2.70%" in _fund_md)
check_true("dh: fund D/E as ratio x", "Debt to Equity: 0.77x" in _fund_md)
check_true("dh: fund margin fraction->%", "Profit Margin: 21.80%" in _fund_md)
check_true("dh: fund ROE fraction->%", "Return on Equity: 5.70%" in _fund_md)

# BUGs 4/10/1/8 — the EVE data adapter (loaded by file path; it's not a package).
import importlib.util as _ilu  # noqa: E402
_qd_path = Path(__file__).resolve().parent.parent / "quiver_eve" / "run" / "quill_data.py"
_qd_spec = _ilu.spec_from_file_location("quill_data_uut", _qd_path)
_qd = _ilu.module_from_spec(_qd_spec)
_qd_spec.loader.exec_module(_qd)
import lib.dataflows.stocktwits as _stm  # noqa: E402
# BUG 4 — sentiment passes the pre-formatted StockTwits STRING through (the old code
# sliced it as a list -> 15 one-char lines) + honest core_available.
_orig_st_fn = _stm.fetch_stocktwits_messages
_stm.fetch_stocktwits_messages = lambda t, **k: ("Bullish: 5 (25%) · Bearish: 2 (10%) · "
    "Unlabeled: 13 · Total: 20 most-recent messages\n\n[ts · @u · Bullish] hi there")
try:
    _srep, _score = _qd.fetch_sentiment("TEST")
finally:
    _stm.fetch_stocktwits_messages = _orig_st_fn
check_true("dh: sentiment passes real summary", "Bullish: 5 (25%)" in _srep)
check_true("dh: sentiment not char-exploded", ('"B"' not in _srep) and ('\n"u"' not in _srep))
check_true("dh: sentiment core True on real data", _score is True)
_stm.fetch_stocktwits_messages = lambda t, **k: "<no StockTwits messages found for $TEST>"
try:
    _srep2, _score2 = _qd.fetch_sentiment("TEST")
finally:
    _stm.fetch_stocktwits_messages = _orig_st_fn
check_true("dh: sentiment placeholder -> core False", _score2 is False)
# BUG 10 — content-aware core_available (sentinel/near-empty -> False, real -> True).
check_true("dh: _content_ok rejects sentinel", _qd._content_ok("Long padding text but this report has no news found in it") is False)
check_true("dh: _content_ok accepts real", _qd._content_ok("Bullish: 5 (25%) real message body with enough length here") is True)
# BUGs 1/8 — _tail_csv keeps the NEWEST rows + whole rows (recency), header preserved.
_csv_uut = "# hdr line\nDate,Close\n" + "\n".join(f"2026-01-{i:02d},{100 + i}" for i in range(1, 26))
_tail_uut = _qd._tail_csv(_csv_uut, max_rows=5)
check_true("dh: tail keeps header+cols", _tail_uut.startswith("# hdr line\nDate,Close"))
check_true("dh: tail keeps NEWEST row", "2026-01-25,125" in _tail_uut)
check_true("dh: tail drops OLDEST row", "2026-01-01,101" not in _tail_uut)

# === F2: market-fetch retry (_retry wraps the RAISING core fetch; class-B ERROR fix) ======
# A transient yfinance miss used to become "core data unavailable: missing market" -> skip.
# _retry retries the CORE price fetch (get_YFin_data_online), NOT the non-raising indicators.
_orig_sleep = _qd.time.sleep
_qd.time.sleep = lambda *_a, **_k: None   # no real backoff in tests
try:
    # (1) _retry unit: fail-twice-then-succeed recovers within tries.
    _rn = {"c": 0}
    def _flaky():
        _rn["c"] += 1
        if _rn["c"] < 3:
            raise RuntimeError("transient")
        return "OK"
    check("F2: _retry recovers after transient fails", _qd._retry(_flaky, tries=3), "OK")
    check("F2: _retry used exactly the needed attempts", _rn["c"], 3)
    # always-fail -> re-raises after EXACTLY `tries` (fail-safe preserved, bounded).
    _dn = {"c": 0}
    def _dead():
        _dn["c"] += 1
        raise RuntimeError("down")
    _r1 = False
    try:
        _qd._retry(_dead, tries=3)
    except RuntimeError:
        _r1 = True
    check_true("F2: _retry re-raises after exhausting tries", _r1)
    check("F2: _retry tried exactly `tries` times", _dn["c"], 3)

    # (2) INTEGRATION: the REAL fetch_market wraps the PRICE fetch (:125) in _retry, not the
    #     non-raising _latest_indicators (:127). Transient price miss -> recovers -> core True.
    _CSV = ("Date,Open,High,Low,Close,Volume\n2026-07-10,1,2,0.5,1.5,100\n"
            "2026-07-11,1.5,2,1,1.8,120\n")
    _fm = {"c": 0}
    def _flaky_price(t, s, e):
        _fm["c"] += 1
        if _fm["c"] < 2:
            raise RuntimeError("yfinance transient")
        return _CSV
    _orig_price = _yfm.get_YFin_data_online
    _orig_inds = _qd._latest_indicators
    _yfm.get_YFin_data_online = _flaky_price
    _qd._latest_indicators = lambda *a, **k: {}
    try:
        _rep, _core = _qd.fetch_market("TEST", "2026-07-14")
    finally:
        _yfm.get_YFin_data_online = _orig_price
        _qd._latest_indicators = _orig_inds
    check_true("F2: fetch_market recovers a transient price miss (core True)", _core is True)
    check("F2: fetch_market retried the PRICE fetch", _fm["c"], 2)
    check_true("F2: recovered report carries the real price rows", "2026-07-11" in _rep)
    # persistent price outage -> fetch_market RAISES (so _safe -> UNAVAILABLE; gate still fires).
    def _dead_price(t, s, e):
        raise RuntimeError("yfinance down")
    _yfm.get_YFin_data_online = _dead_price
    _qd._latest_indicators = lambda *a, **k: {}
    _r2 = False
    try:
        _qd.fetch_market("TEST", "2026-07-14")
    except Exception:
        _r2 = True
    finally:
        _yfm.get_YFin_data_online = _orig_price
        _qd._latest_indicators = _orig_inds
    check_true("F2: persistent price outage still raises (fail-safe -> UNAVAILABLE)", _r2)
finally:
    _qd.time.sleep = _orig_sleep

# === Macro news tool (6th channel) — wired 2026-07-13 =============================
# The macro_report section is recognized by the splitter + tracked (non-core).
_macro_md = ("## market_report\n" + "x" * 60 + "\n\n## macro_report\nOil prices surge on "
             "US-Iran attacks; Strait of Hormuz closed once again\n\n## final_trade_decision\n"
             "**Rating**: Hold\n**Next Review Hours**: 24\n**Conviction**: 50\n**Uncertainty**: 40\n")
_macro_split = _az._split_eve_markdown(_macro_md)
check_true("macro: section captured by split", "macro_report" in _macro_split and "Hormuz" in _macro_split["macro_report"])
check_true("macro: in _EVE_SECTIONS", "macro_report" in _az._EVE_SECTIONS)
_macro_dq = _az.assess_data_quality(_macro_split)
check_true("macro: tracked in data_quality", _macro_dq.get("macro") is True)
check("macro: does NOT gate core (market-only)", _macro_dq["core_available"], _macro_dq["market"])
# fetch_macro passes the global-news STRING through + honest core; ignores ticker.
import lib.dataflows.yfinance_news as _ynm  # noqa: E402
_orig_gn = _ynm.get_global_news_yfinance
_ynm.get_global_news_yfinance = lambda d, **k: "## Global Market News:\n\n### Oil prices surge on US-Iran attacks, Strait of Hormuz (source: AP)\n"
try:
    _mrep, _mcore = _qd.fetch_macro("2026-07-13")
finally:
    _ynm.get_global_news_yfinance = _orig_gn
check_true("macro: fetch passes real news", "Oil prices surge" in _mrep)
check_true("macro: fetch core True on real data", _mcore is True)
_ynm.get_global_news_yfinance = lambda d, **k: "No global news found for 2026-07-13"
try:
    _mrep2, _mcore2 = _qd.fetch_macro("2026-07-13")
finally:
    _ynm.get_global_news_yfinance = _orig_gn
check_true("macro: empty -> core False (honest)", _mcore2 is False)


# ===================== LEGISLATIVE CATALYST (lib/legislative, congress, learn) =====================
import lib.legislative as _L  # noqa: E402
import lib.dataflows.congress as _CG  # noqa: E402
import lib.learn as _LEARN  # noqa: E402
import lib.universe as _UNI  # noqa: E402
from lib.dataflows.errors import DataUnavailableError as _DUE  # noqa: E402

# parse_analysis: strict sanitize + fail-safe + NEVER carries a passage field (GA1 wall)
_pa = _L.parse_analysis('```json\n{"passage_probability":0.99,"impacted_tickers":['
                        '{"ticker":"lmt","direction":"benefit","magnitude":1.7,"horizon":"x","rationale":"y"},'
                        '{"ticker":"!!!","direction":"benefit","magnitude":0.5},'
                        '{"ticker":"MSFT","direction":"bogus","magnitude":0.5}],"thesis":"t"}\n```')
check("legislative: parse keeps only valid tickers", [i["ticker"] for i in _pa["impacted_tickers"]], ["LMT"])
check("legislative: parse clamps magnitude to 1.0", _pa["impacted_tickers"][0]["magnitude"], 1.0)
check("legislative: parse defaults bad horizon", _pa["impacted_tickers"][0]["horizon"], "medium")
check_true("legislative: parse output has NO passage key (GA1 wall)",
           "passage_probability" not in str(_pa) and "passage_prob" not in str(_pa))
check("legislative: parse garbage -> empty (fail-safe)", _L.parse_analysis("not json"),
      {"impacted_tickers": [], "thesis": ""})

# passes_thresholds (dual gate, inclusive >=, fail-safe on None)
check_true("legislative: passes both gates", _L.passes_thresholds(0.6, 0.6, prob_min=0.55, impact_min=0.5))
check_true("legislative: fails low passage", not _L.passes_thresholds(0.5, 0.9, prob_min=0.55, impact_min=0.5))
check_true("legislative: fails low impact", not _L.passes_thresholds(0.9, 0.4, prob_min=0.55, impact_min=0.5))
check_true("legislative: None -> False", not _L.passes_thresholds(None, 0.9, prob_min=0.55, impact_min=0.5))

# tally_judge: only >= pass_min PASS advances; a tie never passes
check("legislative: judge 2/3 pass -> pass", _L.tally_judge(["pass", "pass", "fail"], 2), ("pass", 2))
check("legislative: judge 1/3 pass -> fail", _L.tally_judge(["pass", "fail", "fail"], 2), ("fail", 1))
check_true("legislative: judge 1pass/1hold/1fail never passes",
           _L.tally_judge(["pass", "hold", "fail"], 2)[0] != "pass")
check("legislative: judge empty -> fail", _L.tally_judge([], 2), ("fail", 0))

# bill_content_hash: stable + changes when status/action advances
check("legislative: content_hash stable", _L.bill_content_hash("committee", "2026-07-01", "u1"),
      _L.bill_content_hash("committee", "2026-07-01", "u1"))
check_true("legislative: content_hash changes on advance",
           _L.bill_content_hash("committee", "2026-07-01", "u1") != _L.bill_content_hash("passed_one", "2026-07-01", "u1"))

# heuristic_passage_prob: monotonic in progress; failed=0; must-pass bump; clamp
_seq = [_CG.heuristic_passage_prob({"status": s, "title": "x", "cosponsors_n": 0, "latest_action": ""})
        for s in ("introduced", "committee", "reported", "passed_one", "passed_both", "to_president", "became_law")]
check_true("legislative: passage prob monotonic in progress", _seq == sorted(_seq) and _seq[-1] == 1.0)
check("legislative: failed bill prob 0", _CG.heuristic_passage_prob({"status": "failed", "title": "x"}), 0.0)
check_true("legislative: must-pass + cosponsors bump (clamped)",
           _CG.heuristic_passage_prob({"status": "passed_one", "title": "defense appropriations", "cosponsors_n": 100})
           > _CG.heuristic_passage_prob({"status": "passed_one", "title": "x", "cosponsors_n": 0}))
check("legislative: derive_status became_law", _CG.derive_status("Became Public Law No: 119-1."), "became_law")
check("legislative: derive_status committee", _CG.derive_status("Referred to the Committee on Finance."), "committee")
# House suspension-of-rules passage + received-in-chamber (the calibration-backtest fix — these
# were being mis-detected as introduced/committee, so enacted bills scored ~0.03 and got gated out)
check("legislative: suspension-rules pass -> passed_one",
      _CG.derive_status("On motion to suspend the rules and pass the bill Agreed to by voice vote."), "passed_one")
check("legislative: a MOTION to suspend (no 'agreed to') is NOT passage",
      _CG.derive_status("Mr. Smith moved to suspend the rules and pass the bill."), "introduced")
check("legislative: received in the senate -> passed_one",
      _CG.derive_status("Received in the Senate and Read twice and referred to the Committee on Finance."), "passed_one")

# --- passage-calibration backtest (lib/legislative_backtest, pure scorers + leak-free timeline) ---
import lib.legislative_backtest as _BT  # noqa: E402
check("backtest: brier perfect", _BT.brier_score([(1, 1), (0, 0)]), 0.0)
check("backtest: brier coinflip", _BT.brier_score([(0.5, 1), (0.5, 0)]), 0.25)
check("backtest: auc perfect separation", _BT.auc([(0.9, 1), (0.2, 0)]), 1.0)
check("backtest: auc reversed", _BT.auc([(0.1, 1), (0.9, 0)]), 0.0)
check_true("backtest: auc one-class -> None", _BT.auc([(0.9, 1), (0.9, 1)]) is None)
# LEAK GUARD: the terminal became_law action must be excluded from the scored peak status
_bt_acts = [{"text": "Introduced in House"}, {"text": "Passed House"}, {"text": "Became Public Law No: 118-1."}]
check("backtest: peak excludes terminal became_law", _BT.peak_nonterminal_status(_bt_acts), "passed_one")
check("backtest: peak defaults to introduced", _BT.peak_nonterminal_status([]), "introduced")
# records_from_fixture re-derives peak from RAW actions (so a derive_status change re-scores offline)
_bt_recs = _BT.records_from_fixture([
    {"bill_id": "x", "enacted": 1, "title": "t", "actions": _bt_acts},
    {"bill_id": "y", "enacted": 0, "title": "t", "actions": [{"text": "Referred to the Committee."}]}])
check("backtest: fixture re-derives peak status", _bt_recs[0]["peak_status"], "passed_one")
check_true("backtest: calibrate emits brier+auc+reliability+status",
           all(k in _BT.calibrate(_bt_recs) for k in ("brier", "auc", "reliability", "status_calibration")))

# --- judge-vs-human-label agreement scorer (lib/legislative_judge_eval, pure) ---
import lib.legislative_judge_eval as _JE  # noqa: E402
_jm = _JE.agreement_metrics([("pass", "pass"), ("fail", "fail"), ("pass", "fail")])
check("judge-eval: precision (1 TP, 1 FP)", _jm["precision"], 0.5)
check("judge-eval: recall (1 TP, 0 FN)", _jm["recall"], 1.0)
check("judge-eval: 'hold' is a negative prediction", _JE.agreement_metrics([("hold", "pass")])["confusion"]["fn"], 1)
_jsw = _JE.sweep_pass_min([(["pass", "pass", "fail"], "pass"), (["pass", "fail", "fail"], "fail"),
                          (["fail", "fail", "fail"], "fail")])
check("judge-eval: sweep spans pass_min 1..3", [s["pass_min"] for s in _jsw], [1, 2, 3])
check_true("judge-eval: stricter pass_min raises precision (fewer false PASSes)",
           _JE.best_operating_point(_jsw, "precision")["pass_min"] >= _JE.best_operating_point(_jsw, "recall")["pass_min"]
           if _JE.best_operating_point(_jsw, "recall") else True)

# poll_changed_bills: dedup + fail-safe (fake opener, no network)
def _fake_opener(url, timeout):
    import json as _j
    return _j.dumps({"bills": [
        {"congress": 119, "type": "HR", "number": 10, "title": "Bill A",
         "latestAction": {"text": "Passed House", "actionDate": "2026-07-10"}, "updateDate": "2026-07-10T00:00:00Z"},
        {"congress": 119, "type": "HR", "number": 10, "title": "Bill A dup",
         "latestAction": {"text": "Passed House", "actionDate": "2026-07-10"}, "updateDate": "2026-07-10T00:00:00Z"}]}).encode()
_polled = _CG.poll_changed_bills("a", "b", "KEY", max_pages=1, opener=_fake_opener)
check("legislative: poll dedups (bill_id,update)", len(_polled), 1)
check_raises("legislative: poll no key raises (fail-safe)",
             lambda: _CG.poll_changed_bills("a", "b", "", opener=_fake_opener), _DUE)

# apply_add: funds only the DELTA; a re-add of an already-active name is a no-op (GB2 cash-drift fix)
_rows0 = [{"sleeve": "Cash", "ticker": "SGOV", "target_weight": 40.0, "status": "active"},
          {"sleeve": "US", "ticker": "QQQ", "target_weight": 60.0, "band": 5, "status": "active"}]
_r1, _f1, _ = _UNI.apply_add(_rows0, "LMT", "Legislative Catalyst", 4.0, 1.0, cash_ticker="SGOV")
check("legislative: apply_add funds full weight for a NEW name", _f1, 4.0)
check("legislative: apply_add debits cash by the funded weight",
      round(next(r["target_weight"] for r in _r1 if r["ticker"] == "SGOV"), 2), 36.0)
_r2, _f2, _ = _UNI.apply_add(_r1, "LMT", "Legislative Catalyst", 4.0, 1.0, cash_ticker="SGOV")
check("legislative: re-add already-active name is a NO-OP (no re-debit)", _f2, 0.0)
check("legislative: re-add leaves cash unchanged", _r2, _r1)
_r3, _f3, _ = _UNI.apply_add(_r1, "LMT", "Legislative Catalyst", 6.0, 1.0, cash_ticker="SGOV")
check("legislative: apply_add funds only the incremental delta on an increase", _f3, 2.0)

# build_catalyst_proposals gating (benefit ADD allow-list; suffer REMOVE held+allow+policy-map)
_act = [{"bill_id": "119-hr-2", "ticker": "FSLR", "direction": "benefit", "magnitude": 0.8, "rationale": "r", "policy_codes": ["Energy"]},
        {"bill_id": "119-hr-3", "ticker": "XOM", "direction": "suffer", "magnitude": 0.7, "rationale": "r2", "policy_codes": ["Energy"]},
        {"bill_id": "119-hr-4", "ticker": "ZZZZ", "direction": "benefit", "magnitude": 0.9, "rationale": "r3", "policy_codes": ["x"]}]
_allow = frozenset({"FSLR", "XOM", "QQQ"})
_held = {"XOM", "QQQ"}
_p_add = _LEARN.build_catalyst_proposals(_act, _held, add_weight=4.0, allow=_allow, policy_area_map={}, remove_enabled=False)
_addkeys = {(p.kind, p.ticker) for p in _p_add}
check_true("legislative: benefit on allow-list -> ADD", ("PROPOSE_ADD", "FSLR") in _addkeys)
check_true("legislative: benefit OFF allow-list -> no ADD", ("PROPOSE_ADD", "ZZZZ") not in _addkeys)
check_true("legislative: ADD tier is 'legislative'", all(p.tier == "legislative" for p in _p_add))
check_true("legislative: no REMOVE when remove_enabled=False", not any(p.kind == "PROPOSE_REMOVE" for p in _p_add))
check_true("legislative: no REMOVE without a policy-area map",
           not any(p.kind == "PROPOSE_REMOVE" for p in _LEARN.build_catalyst_proposals(
               _act, _held, add_weight=4.0, allow=_allow, policy_area_map={}, remove_enabled=True)))
_p_rm = _LEARN.build_catalyst_proposals(_act, _held, add_weight=4.0, allow=_allow,
                                        policy_area_map={"XOM": ["Energy"]}, remove_enabled=True)
check_true("legislative: REMOVE when held+allow+policy-code overlap",
           any(p.kind == "PROPOSE_REMOVE" and p.ticker == "XOM" for p in _p_rm))
check_true("legislative: no REMOVE when policy codes don't overlap",
           not any(p.kind == "PROPOSE_REMOVE" for p in _LEARN.build_catalyst_proposals(
               _act, _held, add_weight=4.0, allow=_allow, policy_area_map={"XOM": ["Taxation"]}, remove_enabled=True)))
check_true("legislative: no REMOVE for an UNHELD suffer name",
           not any(p.kind == "PROPOSE_REMOVE" for p in _LEARN.build_catalyst_proposals(
               [{"bill_id": "b", "ticker": "FSLR", "direction": "suffer", "magnitude": 0.7, "rationale": "r", "policy_codes": ["Energy"]}],
               _held, add_weight=4.0, allow=_allow, policy_area_map={"FSLR": ["Energy"]}, remove_enabled=True)))

# actionable_impacts filter (judged=pass AND applied=0 AND passage>=min AND magnitude>=min) via temp ledger
import tempfile as _tf2  # noqa: E402
from lib.ledger import Ledger as _Led2  # noqa: E402
_ldb = str(Path(_tf2.mkdtemp()) / "leg.db")
_lg = _Led2(_ldb)
_lg.ensure_schema()
with _lg.legislative_conn() as _lc:
    check_true("legislative: upsert_bill returns True for a NEW bill",
               _L.upsert_bill(_lc, bill_id="119-hr-2", congress=119, chamber="house", bill_type="hr", number=2,
                              title="T", status="passed_one", latest_action="Passed House",
                              latest_action_date="2026-07-10", policy_codes=["Energy"], content_hash="c1",
                              first_seen="2026-07-13", now="2026-07-13"))
    check_true("legislative: upsert_bill True again (analyzed_at still NULL -> retry)",
               _L.upsert_bill(_lc, bill_id="119-hr-2", congress=119, chamber="house", bill_type="hr", number=2,
                              title="T", status="passed_one", latest_action="Passed House",
                              latest_action_date="2026-07-10", policy_codes=["Energy"], content_hash="c1",
                              first_seen="2026-07-13", now="2026-07-13"))
    _L.record_analysis(_lc, "119-hr-2", passage_prob=0.7, passage_source="heuristic", thesis="t", model="claude", now="2026-07-13")
    check_true("legislative: upsert_bill False once unchanged AND analyzed",
               not _L.upsert_bill(_lc, bill_id="119-hr-2", congress=119, chamber="house", bill_type="hr", number=2,
                                  title="T", status="passed_one", latest_action="Passed House",
                                  latest_action_date="2026-07-10", policy_codes=["Energy"], content_hash="c1",
                                  first_seen="2026-07-13", now="2026-07-13"))
    _L.replace_bill_impacts(_lc, "119-hr-2", [{"ticker": "FSLR", "direction": "benefit", "magnitude": 0.8,
                                               "horizon": "medium", "rationale": "r", "sector": "energy"}], "2026-07-13")
    check("legislative: unjudged bill is NOT actionable",
          _L.actionable_impacts(_lc, min_passage_prob=0.55, min_magnitude=0.5), [])
    _L.mark_bill_judged(_lc, "119-hr-2", "fail", "no", "[]", "2026-07-13")
    check("legislative: judge-FAIL bill is NOT actionable",
          _L.actionable_impacts(_lc, min_passage_prob=0.55, min_magnitude=0.5), [])
    _L.mark_bill_judged(_lc, "119-hr-2", "pass", "ok", "[]", "2026-07-13")
    check("legislative: judged-PASS bill is actionable",
          len(_L.actionable_impacts(_lc, min_passage_prob=0.55, min_magnitude=0.5)), 1)
    check("legislative: actionable excludes below-magnitude",
          _L.actionable_impacts(_lc, min_passage_prob=0.55, min_magnitude=0.9), [])
    check("legislative: actionable excludes below-passage",
          _L.actionable_impacts(_lc, min_passage_prob=0.75, min_magnitude=0.5), [])
    _L.mark_bill_applied(_lc, "119-hr-2")
    check("legislative: applied bill is NOT actionable",
          _L.actionable_impacts(_lc, min_passage_prob=0.55, min_magnitude=0.5), [])

# --- congress_cache: memoize GETs so repeated historical pulls never re-hit the API quota ---
_cache_calls = {"n": 0}


def _cache_fake(_u, _t):
    _cache_calls["n"] += 1
    return b'{"x":1}'


_cstats = {}
_copener = _L.caching_opener(_lg, now="2026-07-13T12:00:00", stats=_cstats, fetch=_cache_fake)
_cu = "https://api.congress.gov/v3/bill/118/hr/1/actions?api_key=SECRET&format=json"
check("legislative: cache 1st fetch hits network", _copener(_cu, 30), b'{"x":1}')
check("legislative: cache 2nd fetch is a hit", _copener(_cu, 30), b'{"x":1}')
check("legislative: cache hit survives api_key rotation", _copener(_cu.replace("SECRET", "ROT"), 30), b'{"x":1}')
check("legislative: ONE network call for 3 cached fetches", _cache_calls["n"], 1)
check("legislative: cache stats (1 miss, 2 hits)", _cstats, {"misses": 1, "hits": 2})


# ============================ STRATEGY INTELLIGENCE (lib/intel) =============
# Section splitter + the cost-gate prefilter. PURE — no network, no LLM, no clock.
from lib.intel import sections as _isec  # noqa: E402
from lib.intel import prefilter as _ipf  # noqa: E402

# A tiny USLM bill: one boilerplate section, one that mimics H.R.1 §20308 (the SUBJECT
# "uranium" lives ONLY in the header — the operative text names it by omission), and one
# unrelated section. Proves splitting, fact extraction, boilerplate, and the header-recall
# property the whole prefilter design turns on.
_BILL_XML = b"""<?xml version="1.0"?>
<bill><legis-body>
  <section><enum>1.</enum><header>Short title</header><text>This Act may be cited as the Test Act.</text></section>
  <section><enum>20308.</enum><header>Ensuring consideration of uranium as a critical mineral</header>
    <text>Section 7002(a)(3)(B)(i) of the Energy Act of 2020 (30 U.S.C. 1606(a)(3)(B)(i)) is amended
    to read as follows: oil, oil shale, coal, or natural gas. Not later than 60 days after enactment
    the Secretary shall publish an update, and $50,000,000 is authorized.</text></section>
  <section><enum>412.</enum><header>Fish hatchery maintenance schedule</header>
    <text>The Secretary of the Interior shall maintain the regional fish hatchery on a quarterly basis.</text></section>
</legis-body></bill>"""

_secs = _isec.parse_sections(_BILL_XML)
check("intel: parse_sections finds all 3 <section> elements", len(_secs), 3)
_by = {s.enum: s for s in _secs}
check("intel: section enum stripped of trailing dot", "20308" in _by, True)
check("intel: short-title flagged boilerplate", _by["1"].is_boilerplate, True)
check("intel: substantive section not boilerplate", _by["20308"].is_boilerplate, False)
check("intel: US Code cite extracted", "30 U.S.C. 1606" in _by["20308"].usc_cites, True)
check("intel: amended Act extracted", any("Energy Act of 2020" in a for a in _by["20308"].acts_amended), True)
check("intel: deadline extracted", _by["20308"].deadline.startswith("Not later than 60 days"), True)
check("intel: dollar amount extracted", any("50,000,000" in d for d in _by["20308"].dollar_amounts), True)
check("intel: amended-Act regex rejects 'this Act' boilerplate", _by["1"].acts_amended, [])
check_raises("intel: unparseable XML fails SAFE (raises, never fabricates)",
             lambda: _isec.parse_sections(b"<not xml"), _isec.DataUnavailableError)

# PREFILTER recall: §20308 survives ONLY because its header carries "uranium"/"critical mineral"
# (the operative text has neither) — the exact property that forced header+body matching.
check("intel: prefilter KEEPS the uranium section (header-recall)", _ipf.matches(_by["20308"].text), True)
check("intel: prefilter DROPS the unrelated fish-hatchery section", _ipf.matches(_by["412"].text), False)
check("intel: prefilter DROPS boilerplate short-title", _ipf.matches(_by["1"].text), False)
check_true("intel: matched_terms records why a section was kept",
           any("uranium" in t or "critical mineral" in t for t in _ipf.matched_terms(_by["20308"].text)))
_kept, _dropped = _ipf.keep(_secs)
check("intel: keep() partitions to the 1 book-relevant section", len(_kept), 1)
check("intel: keep() reports the dropped count (over-filter visibility)", _dropped, 2)
# Specificity: a bare energy word must NOT match (broad tokens defeat the gate).
check("intel: 'natural gas' alone does not trip the gate (specificity)", _ipf.matches("natural gas pipeline permitting"), False)
check("intel: 'covered sector' (FAST-41, §20304) trips the gate", _ipf.matches("designated as a covered sector"), True)

# REGULATION arm: FR-rule splitter + the CFR-by-agency relevance crosswalk.
_FR_XML = b"""<?xml version="1.0"?><RULE>
  <PREAMB><AGENCY>Nuclear Regulatory Commission</AGENCY>
    <HD SOURCE="HED">SUMMARY:</HD>
    <P>The NRC is amending its regulations to approve a new spent fuel storage cask design for
    commercial nuclear power reactors under 10 CFR Part 72.</P>
    <HD SOURCE="HED">DATES:</HD><P>Effective August 1.</P></PREAMB>
  <REGTEXT><SECTION><SECTNO>&#167; 72.214</SECTNO><SUBJECT>List of approved spent fuel storage casks.</SUBJECT>
    <P>Certificate Number: 1032. The cask model is added to the list of approved storage casks for
    spent nuclear fuel, effective under 10 CFR 72.214.</P></SECTION></REGTEXT></RULE>"""
_frsecs = _isec.parse_fr_sections(_FR_XML, min_chars=20)
_frby = {s.enum: s for s in _frsecs}
check_true("intel: FR splitter emits a SUMMARY section", "SUMMARY" in _frby)
check_true("intel: FR splitter emits the operative CFR section (SECTNO)", "72.214" in _frby)
check_true("intel: FR SUMMARY carries the agency's statement of the rule",
           "spent fuel storage cask" in _frby["SUMMARY"].text.lower())
check("intel: FR section CFR cite extracted", any("72" in c for c in _frby["72.214"].cfr_cites), True)
check_raises("intel: FR unparseable fails SAFE", lambda: _isec.parse_fr_sections(b"<not"), _isec.DataUnavailableError)
# agency relevance (CFR-by-agency crosswalk): a book regulator is kept regardless of vocab
check("intel: NRC is a book-relevant regulator", _ipf.agency_is_relevant("Nuclear Regulatory Commission"), True)
check("intel: FERC is a book-relevant regulator", _ipf.agency_is_relevant("Federal Energy Regulatory Commission"), True)
check("intel: an off-book agency is not relevant", _ipf.agency_is_relevant("United States Postal Service"), False)

# --- aggregate: section impacts -> per-key-player posture (deterministic weighting) ---
from lib.intel import aggregate as _iagg  # noqa: E402
check("intel: impact_weight helps/high/UPHELD = +1.0", _iagg.impact_weight("helps", "high", "UPHELD"), 1.0)
check("intel: impact_weight hurts/medium/UPHELD = -0.6", round(_iagg.impact_weight("hurts", "medium", "UPHELD"), 4), -0.6)
check("intel: impact_weight REJECTED contributes ZERO (verifier veto is absolute)",
      _iagg.impact_weight("helps", "high", "REJECTED"), 0.0)
check("intel: impact_weight ambiguous = 0", _iagg.impact_weight("ambiguous", "high", "UPHELD"), 0.0)
check("intel: unverified impact discounted not zeroed", round(_iagg.impact_weight("helps", "high", ""), 4), 0.4)
_imp_ura = [
    {"ticker": "URA", "direction": "helps", "confidence": "low", "verify_verdict": "UPHELD",
     "doc_id": "118-hr-1", "sec": "20304", "step1_span": "inserting mineral production", "title": "LEC Act"},
    {"ticker": "URA", "direction": "helps", "confidence": "medium", "verify_verdict": "DOWNGRADED",
     "doc_id": "118-hr-1", "sec": "20308", "step1_span": "oil, oil shale, coal", "title": "LEC Act"},
]
_pos = _iagg.posture_for_ticker(_imp_ura)
check("intel: posture nets to helps (0.3*1.0 + 0.6*0.5 = 0.6)", _pos["score"], 0.6)
check("intel: posture label helps", _pos["posture"], "helps")
check("intel: posture keeps evidence trail", len(_pos["evidence"]), 2)
# all-REJECTED nets to zero -> neutral, no evidence contributes
_pos_rej = _iagg.posture_for_ticker([{"ticker": "X", "direction": "helps", "confidence": "high", "verify_verdict": "REJECTED"}])
check("intel: all-REJECTED ticker -> neutral score 0", _pos_rej["score"], 0.0)
check("intel: all-REJECTED ticker -> no evidence rows", len(_pos_rej["evidence"]), 0)
# helps + hurts that cancel -> 'mixed'
_pos_mix = _iagg.posture_for_ticker([
    {"ticker": "Y", "direction": "helps", "confidence": "high", "verify_verdict": "UPHELD"},
    {"ticker": "Y", "direction": "hurts", "confidence": "high", "verify_verdict": "UPHELD"}])
check("intel: opposing evidence that cancels -> mixed", _pos_mix["posture"], "mixed")
_prof = _iagg.build_profile(bioguide="S001176", congress=118,
    power=[{"role": "leadership", "committee": "LEAD", "chamber": "house", "name": "Scalise"}], impacts=_imp_ura)
check("intel: build_profile groups by ticker", list(_prof["tickers"]), ["URA"])
check("intel: build_profile marks key player", _prof["is_key_player"], True)
check("intel: build_profile name from power", _prof["name"], "Scalise")

# --- profiles: pure dict -> markdown VIEW, diff-stable ---
from lib.intel import profiles as _iprof  # noqa: E402
_r1 = _iprof.render(_prof, as_of="2026-07-18")
_r2 = _iprof.render(_prof, as_of="2099-12-31")  # different as_of, SAME body
check("intel: profile content_hash excludes as_of (stable diffs)", _r1["content_hash"], _r2["content_hash"])
check_true("intel: profile md carries the DO-NOT-EDIT view header", "A VIEW over intel.db" in _r1["md"])
check_true("intel: profile md traces a claim to a section", "118-hr-1 §20304" in _r1["md"])
check_true("intel: profile md shows the verbatim span", "inserting mineral production" in _r1["md"])
check_true("intel: profile md has NO generated_at timestamp in body", "generated_at" not in _r1["md"])
# an empty profile (key player, no book-relevant sections) still renders honestly
_empty = _iagg.build_profile(bioguide="R000575", congress=118,
    power=[{"role": "chair", "committee": "HSAS", "chamber": "house", "name": "Rogers"}], impacts=[])
check_true("intel: empty profile says nothing-to-report (not a fabrication)",
           "No sponsored section reached" in _iprof.render(_empty)["md"])

# --- propose: additive-only, cash-capped, CONSERVATION is structural ---
from lib.intel import propose as _iprop  # noqa: E402
_book = _iprop.BookSnapshot(
    held={"URA", "SMH", "SGOV"}, tradable={"URA", "SMH", "NLR", "SMR", "SGOV"},
    sleeves={"Power": {"weight": 14.0, "protected": True, "tickers": ["URA"]},
             "Cash": {"weight": 18.0, "protected": False, "tickers": ["SGOV"]}},
    cash_pct=18.0, ticker_sleeve={"URA": "Power", "SMH": "Compute", "SGOV": "Cash"})
# a tailwind on a NON-held tradable name -> one ADD; a tailwind on a HELD name -> skipped
_props = _iprop.propose(
    postures={"NLR": {"posture": "helps", "score": 0.9, "evidence": [{"span": "x"}]},
              "URA": {"posture": "helps", "score": 0.9, "evidence": [{"span": "y"}]}},
    book=_book, cfg=_iprop.ProposeConfig(min_score=0.5, add_weight=4.0, intel_max_total_pct=20.0))
check("intel: propose ADDs the non-held tailwind name only", [p["ticker"] for p in _props], ["NLR"])
check("intel: propose tier is 'intel' (always human-approve)", _props[0]["tier"], "intel")
check("intel: propose funds from cash at add_weight", _props[0]["target_weight"], 4.0)
check_true("intel: tailwind ADDs never trigger a protected reduction", not _iprop.protected_reduction_below_bar(_props, _book))
# budget cap: many tailwind names, tiny cash -> total proposed weight <= min(cash, cap)
_many = {f"T{i}": {"posture": "helps", "score": 0.9, "evidence": []} for i in range(10)}
_smallcash = _iprop.BookSnapshot(held=set(), tradable={f"T{i}" for i in range(10)}, sleeves={},
                                 cash_pct=6.0, ticker_sleeve={})
_capped = _iprop.propose(postures=_many, book=_smallcash,
                         cfg=_iprop.ProposeConfig(min_score=0.5, add_weight=4.0, intel_max_total_pct=20.0))
check_true("intel: propose respects the cash budget (sum <= 6%)", sum(p["target_weight"] for p in _capped) <= 6.0 + 1e-9)
# THREAT -> REDUCE: the service CAN reduce a held position to cash on a strong threat (the user's
# "reduce our positions to cash"). Conservation is a HIGHER BAR, not a wall.
_cfg_t = _iprop.ProposeConfig(threat_score=0.6, protected_threat_score=1.5)
# a WEAK threat on a protected held name -> below the protected bar -> NO proposal (noise filtered)
_weak = _iprop.propose(postures={"URA": {"posture": "hurts", "score": -0.9, "evidence": [{"span": "x"}]}},
                       book=_book, cfg=_cfg_t)
check("intel: weak threat on a PROTECTED name is below the bar -> no reduce", len(_weak), 0)
# a STRONG threat on a protected held name -> clears the protected bar -> PROPOSE_REMOVE (to cash)
_strong = _iprop.propose(postures={"URA": {"posture": "hurts", "score": -2.0, "evidence": [{"span": "regulation guts uranium"}]}},
                         book=_book, cfg=_cfg_t)
check("intel: STRONG threat on a protected name -> PROPOSE_REMOVE (reduce to cash)",
      [(p["kind"], p["ticker"]) for p in _strong], [("PROPOSE_REMOVE", "URA")])
check("intel: reduce proposal is tier 'intel' (human --approve winds it to cash)", _strong[0]["tier"], "intel")
check("intel: reduce proposal carries no target_weight (full exit)", _strong[0]["target_weight"], None)
check_true("intel: protected reduction cleared the higher bar (belt-and-suspenders empty)",
           not _iprop.protected_reduction_below_bar(_strong, _book, _cfg_t))
# a moderate threat on an UNPROTECTED held name clears the lower bar
_book_unp = _iprop.BookSnapshot(held={"NLR"}, tradable={"NLR"},
    sleeves={"Nuclear": {"weight": 5.0, "protected": False, "tickers": ["NLR"]}},
    cash_pct=5.0, ticker_sleeve={"NLR": "Nuclear"})
_unp = _iprop.propose(postures={"NLR": {"posture": "hurts", "score": -0.7, "evidence": []}},
                      book=_book_unp, cfg=_cfg_t)
check("intel: moderate threat on an UNPROTECTED held name -> reduce (lower bar)",
      [p["kind"] for p in _unp], ["PROPOSE_REMOVE"])
# a threat on a name we DON'T hold -> nothing to reduce
_noh = _iprop.propose(postures={"SMR": {"posture": "hurts", "score": -5.0, "evidence": []}},
                      book=_book, cfg=_cfg_t)
check("intel: threat on a non-held name -> no reduce (nothing to sell)", len(_noh), 0)
# INTEL-APPROVAL-01: a held name with an EMPTY/unknown sleeve fails SAFE to PROTECTED (higher bar),
# so a moderate threat below the protected bar does NOT propose reducing it.
_book_empty = _iprop.BookSnapshot(held={"ZZZ"}, tradable={"ZZZ"}, sleeves={},
                                  cash_pct=5.0, ticker_sleeve={"ZZZ": ""})
_emptythreat = _iprop.propose(postures={"ZZZ": {"posture": "hurts", "score": -0.9, "evidence": []}},
                              book=_book_empty, cfg=_iprop.ProposeConfig(threat_score=0.6, protected_threat_score=1.5))
check("intel: empty-sleeve held name is treated PROTECTED (no reduce below protected bar)", len(_emptythreat), 0)

# --- fedreg: the regulation fetcher (offline, fake opener; stdlib urllib in prod) ---
from lib.dataflows import fedreg as _fr  # noqa: E402
def _fr_fake(url, timeout):
    import json as _j
    return _j.dumps({"results": [
        {"document_number": "2026-1", "title": "Nuclear reactor licensing rule", "type": "Rule",
         "publication_date": "2026-03-01", "agency_names": ["Nuclear Regulatory Commission"],
         "cfr_references": [{"title": 10, "part": 50}], "significant": True, "html_url": "http://x"},
        {"document_number": "2026-1", "title": "dup — same doc number", "type": "Rule",
         "publication_date": "2026-03-01", "agency_names": ["NRC"], "cfr_references": []},
    ]}).encode()
_rules = _fr.poll_rules("2026-03-01", "2026-03-02", opener=_fr_fake, max_pages=1)
check("fedreg: poll dedups by document_number", len(_rules), 1)
check("fedreg: doc_id prefixed FR-", _rules[0]["doc_id"], "FR-2026-1")
check("fedreg: agency captured (the regulatory key player)", _rules[0]["agency"], "Nuclear Regulatory Commission")
check("fedreg: CFR reference normalized (industry map)", _rules[0]["cfr_references"], ["10 CFR 50"])
check("fedreg: published date is the as-of anchor", _rules[0]["published"], "2026-03-01")
check_raises("fedreg: non-JSON fails SAFE (raises, never fabricates)",
             lambda: _fr.poll_rules("a", "b", opener=lambda u, t: b"<html>error", max_pages=1), Exception)

# --- config: intel section fails SAFE + validates only when enabled ---
def _cfg_with_intel(intel_block):
    dd = {"account_number": "12345678", "dry_run": True, "kill_switch_file": "/tmp/kintel",
          "risk": {"max_dollars_per_trade": 25, "daily_loss_halt_pct": 5.0,
                   "daily_capital_deploy_cap": 75, "min_buying_power_buffer": 5},
          "glm": {"chat_model": "glm-5.2", "reasoner_model": "glm-5.2"},
          "intel": intel_block}
    return load_config(_write_config(dd))
check("config: intel absent -> disabled (fail-safe)", _cfg_with_intel({}).intel.enabled, False)
check("config: intel enabled reads knobs", _cfg_with_intel({"enabled": True, "add_weight": 3.0}).intel.add_weight, 3.0)
check_raises("config: intel enabled + add_weight<=0 raises",
             lambda: _cfg_with_intel({"enabled": True, "add_weight": 0}), ConfigError)
check_raises("config: intel enabled + max_total>100 raises",
             lambda: _cfg_with_intel({"enabled": True, "intel_max_total_pct": 150}), ConfigError)

# --- chain: section -> book-hits, injectable LLM, deterministic span-verbatim veto ---
from lib.intel import chain as _ichain  # noqa: E402
class _Sec:  # a minimal Section-like object
    def __init__(s, enum, header, text): s.enum, s.header, s.text = enum, header, text
_sec_ura = _Sec("20308", "uranium as a critical mineral",
                "Section 7002(a)(3)(B)(i) is amended to read: oil, oil shale, coal, or natural gas.")
# a GOOD response: span is verbatim, ticker in allow-list -> hit survives
_good = '```json\n{"sec":"20308","step1_span":"oil, oil shale, coal, or natural gas",' \
        '"step1_what_changes":"strikes fuel-minerals exclusion","step2_mechanism":"uranium eligible",' \
        '"book_hits":[{"ticker":"URA","direction":"helps","confidence":"low","reasoning":"uranium miners"}],' \
        '"no_impact":false}\n```'
_r = _ichain.analyze_section(_sec_ura, allow=["URA", "SMH"], book="AI+energy", llm=lambda p, t: _good)
check("chain: verbatim span + allow-list ticker -> hit survives", [h["ticker"] for h in _r["book_hits"]], ["URA"])
check("chain: span_verbatim True when quote is in the text", _r["span_verbatim"], True)
# FABRICATED span (not in text) -> the deterministic veto drops ALL hits
_fab = '```json\n{"sec":"20308","step1_span":"this text is nowhere in the section",' \
       '"book_hits":[{"ticker":"URA","direction":"helps","confidence":"high","reasoning":"x"}],"no_impact":false}\n```'
_rf = _ichain.analyze_section(_sec_ura, allow=["URA"], book="x", llm=lambda p, t: _fab)
check("chain: fabricated span VETOES all hits (deterministic honesty gate)", _rf["book_hits"], [])
check("chain: fabricated span -> span_verbatim False", _rf["span_verbatim"], False)
# a ticker OUTSIDE the allow-list is dropped even with a good span
_off = '```json\n{"sec":"20308","step1_span":"oil, oil shale, coal, or natural gas",' \
       '"book_hits":[{"ticker":"XOM","direction":"helps","confidence":"high","reasoning":"x"}],"no_impact":false}\n```'
_ro = _ichain.analyze_section(_sec_ura, allow=["URA"], book="x", llm=lambda p, t: _off)
check("chain: off-allow-list ticker dropped", _ro["book_hits"], [])
# unparseable model output -> fail SAFE to no_impact
_ru = _ichain.analyze_section(_sec_ura, allow=["URA"], book="x", llm=lambda p, t: "sorry, I cannot")
check("chain: unparseable output fails SAFE to no_impact", _ru["no_impact"], True)
# F1: a TRIVIAL span (a single ubiquitous word that is in the text) must NOT launder a fabricated
# hit past the veto — require a substantive multi-token quote.
_sec_sec = _Sec("101", "appropriation", "Amends the appropriation for the Secretary of Energy for fiscal year 2026.")
_trivial = '{"sec":"101","step1_span":"Secretary","book_hits":[{"ticker":"URA","direction":"helps","confidence":"high","reasoning":"x"}],"no_impact":false}'
_rt = _ichain.analyze_section(_sec_sec, allow=["URA"], book="x", llm=lambda p, t: _trivial)
check("chain: a one-word span is too trivial to admit a hit (F1)", _rt["book_hits"], [])
check("chain: trivial span -> span_verbatim False", _rt["span_verbatim"], False)

# --- refresh: the whole scheduled pass (fetch -> split -> prefilter -> chain -> ingest), faked ---
from lib.intel import refresh as _iref  # noqa: E402
_BILL2 = b"""<?xml version="1.0"?><bill><legis-body>
  <section><enum>1.</enum><header>Short title</header><text>This Act may be cited as the X Act.</text></section>
  <section><enum>10.</enum><header>Nuclear reactor licensing acceleration</header>
    <text>The Nuclear Regulatory Commission shall issue a combined license for a small modular reactor
    within 180 days. This section concerns nuclear reactor deployment and uranium fuel supply for
    commercial power generation across the electricity grid.</text></section>
  <section><enum>11.</enum><header>Post office naming</header>
    <text>The facility at 100 Main Street shall be known as the John Doe Post Office Building.</text></section>
</legis-body></bill>"""
_refdb = Ledger(tempfile.mktemp(suffix=".db"))
def _fake_llm(prompt, timeout):
    # emit a hit ONLY when the prompt carries the nuclear section (span must be verbatim in it)
    if "small modular reactor" in prompt:
        return ('{"sec":"10","step1_span":"issue a combined license for a small modular reactor",'
                '"step1_what_changes":"NRC licensing deadline","step2_mechanism":"180-day deadline",'
                '"book_hits":[{"ticker":"SMR","direction":"helps","confidence":"medium","reasoning":"reactors"}],'
                '"no_impact":false}')
    return '{"sec":"x","step1_span":"","book_hits":[],"no_impact":true}'
# run_refresh takes a connection FACTORY (led.intel_conn) and commits per document — the slow
# fetch + LLM I/O holds no lock, and a doc completed before a timeout persists.
_refout = _iref.run_refresh(
    _refdb.intel_conn,
    documents=[{"doc_id": "118-hr-99", "kind": "bill", "title": "X Act", "published": "2023-05-01",
                "sponsor": "R000575", "congress": 118}],
    allow=["SMR", "URA"], book_desc="nuclear",
    fetch_text=lambda doc: _BILL2, llm=_fake_llm, now="2026-07-18T00:00:00Z",
    min_section_chars=50)
check("refresh: one document processed", _refout["documents"], 1)
check("refresh: prefilter kept the nuclear section, dropped post-office", _refout["kept"], 1)
check("refresh: chain recorded the SMR impact", _refout["impacts"], 1)
from lib.intel import store as _istore  # noqa: E402
with _refdb.intel_conn() as _rc:
    _rimp = _istore.impacts_for_sponsor(_rc, "R000575")
check("refresh: impact persisted + joinable to sponsor (per-doc commit)", [i["ticker"] for i in _rimp], ["SMR"])
check_true("refresh: persisted impact is span-verbatim", bool(_rimp[0]["span_verbatim"]))
# fetch failure on a doc is best-effort (counted, never raises)
def _boom(doc): raise RuntimeError("network down")
_referr = _iref.run_refresh(_refdb.intel_conn, documents=[{"doc_id": "d2", "published": "2023-01-01"}],
                            allow=["SMR"], book_desc="x", fetch_text=_boom, now="t")
check("refresh: a failed fetch is best-effort (counted, pass continues)", _referr["errors"], 1)
# COST BUDGET (INTEL-COST-1): max_chained_sections caps the LLM fan-out; excess sections are kept
# but not chained (a later run picks them up). A 3-section bill with a budget of 1 -> 1 chained.
_budhit = {"n": 0}
def _count_llm(prompt, timeout):
    _budhit["n"] += 1
    return '{"sec":"x","step1_span":"","book_hits":[],"no_impact":true}'
_BILL3 = b"""<?xml version="1.0"?><bill><legis-body>
  <section><enum>1.</enum><header>Nuclear reactor A</header><text>This section concerns a nuclear reactor and uranium enrichment for commercial power generation across the grid one.</text></section>
  <section><enum>2.</enum><header>Nuclear reactor B</header><text>This section concerns a nuclear reactor and uranium enrichment for commercial power generation across the grid two.</text></section>
  <section><enum>3.</enum><header>Nuclear reactor C</header><text>This section concerns a nuclear reactor and uranium enrichment for commercial power generation across the grid three.</text></section>
</legis-body></bill>"""
_budout = _iref.run_refresh(Ledger(tempfile.mktemp(suffix=".db")).intel_conn,
    documents=[{"doc_id": "b3", "kind": "bill", "published": "2023-01-01", "sponsor": "X", "congress": 118}],
    allow=["URA"], book_desc="nuclear", fetch_text=lambda d: _BILL3, llm=_count_llm, now="t",
    min_section_chars=20, max_chained_sections=1)
check("refresh: max_chained_sections caps LLM calls", _budhit["n"], 1)
check("refresh: budget_hit flag set when the cap bites", _budout["budget_hit"], True)
check("refresh: over-budget sections still stored kept (chained later)", _budout["kept"], 3)
# REGULATION through refresh: a rule from a book regulator dispatches to the FR splitter and its
# operative sections are kept via the agency crosswalk (even though "nuclear reactor" isn't in the
# operative cask text) -> the chain fires and records the impact.
_frdb = Ledger(tempfile.mktemp(suffix=".db"))
def _fr_llm(prompt, timeout):
    if "72.214" in prompt or "spent fuel" in prompt.lower():
        return ('{"sec":"72.214","step1_span":"added to the list of approved storage casks for",'
                '"step1_what_changes":"approves a cask design","step2_mechanism":"cask certified",'
                '"book_hits":[{"ticker":"CEG","direction":"helps","confidence":"low","reasoning":"nuclear utility"}],'
                '"no_impact":false}')
    return '{"sec":"x","step1_span":"","book_hits":[],"no_impact":true}'
_frout = _iref.run_refresh(_frdb.intel_conn,
    documents=[{"doc_id": "FR-9", "kind": "rule", "title": "Cask rule", "published": "2026-07-01",
                "sponsor": "NRC", "congress": 0, "agency": "Nuclear Regulatory Commission"}],
    allow=["CEG", "URA"], book_desc="nuclear",
    fetch_text=lambda d: _FR_XML, llm=_fr_llm, now="t", min_section_chars=20)
check("refresh: rule from a book agency keeps sections via the CFR-agency crosswalk", _frout["kept"] >= 1, True)
check("refresh: rule chain recorded the impact", _frout["impacts"], 1)
# INTEL-1: a key player on MULTIPLE committees has multiple intel_power rows; all_key_player_sponsors
# must return exactly ONE row for them, or cmd_intel_propose would double-count their conviction.
_kpdb = Ledger(tempfile.mktemp(suffix=".db"))
with _kpdb.intel_conn() as _rc:
    _istore.upsert_document(_rc, doc_id="118-hr-7", kind="bill", title="t", published="2023-01-01",
                            sponsor="R000575", congress=118)
    _istore.upsert_power(_rc, bioguide="R000575", congress=118, role="chair", committee="HSAS", name="Rogers")
    _istore.upsert_power(_rc, bioguide="R000575", congress=118, role="member", committee="HSHA", name="Rogers")
    _kps = _istore.all_key_player_sponsors(_rc)
check("intel: a multi-committee sponsor returns ONE row (no double-count)",
      [(k["bioguide"], k["congress"]) for k in _kps], [("R000575", 118)])


print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
