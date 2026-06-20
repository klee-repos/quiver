#!/usr/bin/env python3
"""Deterministic e2e for pipeline-decided sizing (Q1) + dynamic universe (Q3).

Exercises the TICK-level wiring against an isolated ledger (no network, no broker,
no LLM): the conviction branch in `tick._run_construct` (flag OFF byte-identical to
the static book; flag ON conviction-differentiated, caps + cash floor binding; D2
holds prior) and the screener ADD path through `tick._run_universe_apply`
(propose -> human-approve -> apply_add conserves the book -> the name enters the
analysis universe; the AUTO path refuses on insufficient recurrence; a
non-allow-listed candidate is blocked).

Plain asserts (no pytest); exits non-zero on any failure.
"""

import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
os.environ.setdefault("RH_ACCOUNT_NUMBER", "TEST1234")

PASS = 0
FAIL = 0


def ok(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ FAIL: {name}{(' — ' + str(detail)) if detail else ''}")


def _mk(tmp: Path, *, conviction: bool, per_name=80, sleeve_max=100, cash_floor=10,
        auto_propose=False, auto_apply=False, rh="[AAA, BBB, NVDA, SGOV]",
        sleeves="", holdings=None):
    """Write a strategy + config and return a loaded Config + a freshly-seeded Ledger."""
    from lib.config import load_config
    from lib.strategy import load_strategy
    from lib.ledger import Ledger
    holdings = holdings or [
        "      - {sleeve: Tech, ticker: AAA, weight: 30, band: 5}",
        "      - {sleeve: Tech, ticker: BBB, weight: 30, band: 5}",
        "      - {sleeve: Cash, ticker: SGOV, weight: 40}"]
    strat = tmp / "strategy.yaml"
    strat.write_text(
        "schema: 1\n"
        "goal: {target_return_pct: 15, horizon_months: 12, benchmark: SGOV, benchmark_annual_pct: 3.6}\n"
        "macro_thesis: {deploy_trigger_pce_pct: 2.5, standdown_trigger_pce_pct: 3.5}\n"
        f"rh_tradable_confirmed: {rh}\n"
        f"risk_policy: {{per_name_max_pct: {per_name}, sleeve_max_pct: {sleeve_max}, "
        f"cash_floor_pct: {cash_floor}, smoothing_alpha: 1.0}}\n"
        + (sleeves or "")
        + f"learning: {{auto_propose_adds: {str(auto_propose).lower()}, "
        f"auto_apply_universe_changes: {str(auto_apply).lower()}}}\n"
        "books:\n  core:\n    default: true\n    holdings:\n" + "\n".join(holdings) + "\n",
        encoding="utf-8")
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text(
        "schema: 1\ndry_run: true\nkill_switch_file: KILL\naccount_number: TEST1234\n"
        f"strategy_path: {strat}\n"
        "glm: {chat_model: glm-5.2, reasoner_model: glm-5.2}\n"
        "risk: {max_dollars_per_trade: 100, daily_loss_halt_pct: 20, daily_capital_deploy_cap: 1000, "
        "min_buying_power_buffer: 5, max_actions_per_ticker_per_day: 1, max_analyses_per_ticker_per_day: 1, "
        f"rebalance_enabled: true, conviction_weights_enabled: {str(conviction).lower()}, "
        "cash_sleeve_ticker: SGOV}\nnotify: {enabled: false}\n",
        encoding="utf-8")
    cfg = load_config(str(cfg_path))
    sc = load_strategy(str(strat))
    led = Ledger(tmp / f"l_{os.urandom(4).hex()}.db")
    holds = [{"sleeve": h.sleeve, "ticker": h.ticker, "target_weight": h.weight, "band": h.band,
              "status": "active", "quotable": h.quotable, "book": "core", "updated_at": "t"}
             for h in sc.book("core").holdings]
    led.set_strategy_goal_with_holdings(
        goal={"created_at": "t", "target_return_pct": 15, "horizon_months": 12, "benchmark": "SGOV",
              "benchmark_annual_pct": 3.6, "constraint_note": "", "macro_thesis_version": "v",
              "macro_thesis_json": "{}", "active_book": "core", "as_of": "x",
              "start_date": "2026-01-01", "start_equity": 1000.0},
        holdings=holds)
    return cfg, led


def main() -> int:
    import tick
    print("=" * 64)
    print("E2E — pipeline sizing (Q1) + dynamic universe (Q3)")
    print("=" * 64)
    base = {"equity": 1000.0, "buying_power": 1000.0, "positions": {},
            "now_iso": "2026-06-20T11:00:00-04:00"}

    # --- Q1: flag OFF -> byte-identical static book (the iron regression guard) ---
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d), conviction=False)
        out = tick._run_construct(cfg, led, dict(base))
        w = {t: out["target_weights"][t]["target_weight"] for t in out["target_weights"]}
        ok("Q1 flag OFF -> static book weights (byte-identical)",
           w == {"AAA": 30.0, "BBB": 30.0, "SGOV": 40.0} and out["conviction_weights"] is False, w)

    # --- Q1: flag ON -> conviction differentiates same-sleeve names ---
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d), conviction=True, per_name=80)
        an = [{"ticker": "AAA", "signal": "Buy", "conviction": 90, "uncertainty": 10},
              {"ticker": "BBB", "signal": "Buy", "conviction": 30, "uncertainty": 10}]
        out = tick._run_construct(cfg, led, {**base, "analyses": an})
        w = {t: out["target_weights"][t]["target_weight"] for t in out["target_weights"]}
        ok("Q1 flag ON -> higher conviction => higher weight", w["AAA"] > w["BBB"], w)
        ok("Q1 cash floor respected (>= 10%)", w["SGOV"] >= 10.0, w)
        ok("Q1 book conserves to ~100%", abs(sum(w.values()) - 100.0) < 0.5, sum(w.values()))

    # --- Q1: per-name cap binds (both pinned at the cap) ---
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d), conviction=True, per_name=40)
        an = [{"ticker": "AAA", "signal": "Buy", "conviction": 90, "uncertainty": 10},
              {"ticker": "BBB", "signal": "Buy", "conviction": 30, "uncertainty": 10}]
        out = tick._run_construct(cfg, led, {**base, "analyses": an})
        w = {t: out["target_weights"][t]["target_weight"] for t in out["target_weights"]}
        ok("Q1 per-name cap binds (both <= 40)", w["AAA"] <= 40.01 and w["BBB"] <= 40.01, w)

    # --- Q1: flag ON, no analyses -> D2 holds prior book weights (never sells to cash) ---
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d), conviction=True)
        out = tick._run_construct(cfg, led, dict(base))
        w = {t: out["target_weights"][t]["target_weight"] for t in out["target_weights"]}
        ok("Q1 D2: no analyses -> holds prior weights",
           abs(w["AAA"] - 30.0) < 1.0 and abs(w["BBB"] - 30.0) < 1.0, w)

    # --- Q3: screener ADD -> human-approve -> apply_add conserves -> in universe ---
    _sleeves = ('sleeves:\n  "US large cap": {screen: {sector: Technology, '
                'market_cap_min: 50000000000, pe_max: 60, add_weight: 8}}\n')
    _holds = ["      - {sleeve: \"US large cap\", ticker: AAA, weight: 30, band: 5}",
              "      - {sleeve: Cash, ticker: SGOV, weight: 70}"]

    def provider(screen):
        return [{"ticker": "NVDA", "sector": "Technology", "market_cap": 2e12, "pe": 45, "momentum": 0.4},
                {"ticker": "AMZN", "sector": "Technology", "market_cap": 2e12, "pe": 55, "momentum": 0.9}]

    with tempfile.TemporaryDirectory() as d:
        import lib.learn as learn
        import lib.universe as universe
        cfg, led = _mk(Path(d), conviction=False, auto_propose=True, rh="[AAA, NVDA, SGOV]",
                       sleeves=_sleeves, holdings=_holds)
        gid = led.get_active_goal()["id"]
        ps = learn.build_proposals(led, gid, cfg.strategy.learning, "HOLD", None,
                                   strategy_cfg=cfg.strategy, candidate_provider=provider)
        adds = [p for p in ps["all"] if p.kind == "PROPOSE_ADD"]
        ok("Q3 screener proposes 1 allow-listed ADD (NVDA; AMZN excluded)",
           len(adds) == 1 and adds[0].ticker == "NVDA", [(p.ticker) for p in adds])
        ok("Q3 ADD needs approval by default", adds and adds[0] in ps["needs_approval"])
        p = adds[0]
        rid = led.record_universe_proposal(
            goal_id=gid, proposed_at="2026-06-20T10:00:00", kind=p.kind, ticker=p.ticker,
            sleeve=p.sleeve, from_book=None, to_book=None, target_weight=p.target_weight,
            tier=p.tier, content_hash=p.content_hash(), reason=p.reason, goal_gap_pct=None)
        res = tick._run_universe_apply(cfg, led, change_id=rid, approve=True)
        ok("Q3 human --approve applies the ADD", res.get("applied") is True, res)
        rows = led.active_target_portfolio(gid, statuses=("active", "exiting", "removed"))
        wmap = {r["ticker"]: r["target_weight"] for r in rows}
        ok("Q3 apply_add conserves book to ~100",
           abs(sum(r["target_weight"] for r in rows if r["status"] != "removed") - 100) < 0.5, wmap)
        ok("Q3 NVDA funded from cash (70 -> 62)", wmap.get("NVDA") == 8.0 and abs(wmap.get("SGOV", 0) - 62.0) < 0.01, wmap)
        ok("Q3 NVDA now in the analysis universe", "NVDA" in tick._analysis_universe(cfg, led))
        ok("Q3 the add is logged to strategy_change_log",
           bool(led.strategy_change_history(gid, change_type="status")))

    # --- Q3: AUTO path refuses on insufficient recurrence ---
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d), conviction=False, auto_propose=True, auto_apply=True,
                       rh="[AAA, NVDA, SGOV]", sleeves=_sleeves, holdings=_holds)
        gid = led.get_active_goal()["id"]
        rid = led.record_universe_proposal(
            goal_id=gid, proposed_at="2026-06-20T10:00:00", kind="PROPOSE_ADD", ticker="NVDA",
            sleeve="US large cap", from_book=None, to_book=None, target_weight=8, tier="universe",
            content_hash="h_auto", reason="t", goal_gap_pct=None)
        res = tick._run_universe_apply(cfg, led, change_id=rid, approve=False)  # auto, recurrence 1 < 2
        ok("Q3 AUTO ADD refused until confirmed over N days",
           res.get("applied") is False and "confirmed" in (res.get("reason") or ""), res)

    # --- Q3: a non-allow-listed candidate is blocked at apply by validate_add ---
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d), conviction=False, rh="[AAA, SGOV]", sleeves=_sleeves, holdings=_holds)
        gid = led.get_active_goal()["id"]
        rid = led.record_universe_proposal(
            goal_id=gid, proposed_at="2026-06-20T10:00:00", kind="PROPOSE_ADD", ticker="TSLA",
            sleeve="US large cap", from_book=None, to_book=None, target_weight=8, tier="universe",
            content_hash="h_block", reason="t", goal_gap_pct=None)
        res = tick._run_universe_apply(cfg, led, change_id=rid, approve=True)
        ok("Q3 non-allow-listed ADD blocked by validate_add",
           res.get("applied") is False and "refusing add" in (res.get("reason") or ""), res)

    print(f"\nE2E pipeline: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
