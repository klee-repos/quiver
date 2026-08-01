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
import re
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


def _mk(tmp: Path, *, per_name=80, sleeve_max=100, cash_floor=10,
        auto_apply=False, rh="[AAA, BBB, NVDA, SGOV]", sleeves="", holdings=None,
        smoothing_alpha=1.0, dep_min_factor=None):
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
        f"cash_floor_pct: {cash_floor}, smoothing_alpha: {smoothing_alpha}"
        + (f", conviction_deploy_min_factor: {dep_min_factor}" if dep_min_factor is not None else "")
        + "}\n"
        + (sleeves or "")
        + f"learning: {{auto_apply_universe_changes: {str(auto_apply).lower()}}}\n"
        "books:\n  core:\n    default: true\n    holdings:\n" + "\n".join(holdings) + "\n",
        encoding="utf-8")
    cfg_path = tmp / "config.yaml"
    cfg_path.write_text(
        "schema: 1\ndry_run: true\nkill_switch_file: KILL\naccount_number: TEST1234\n"
        f"strategy_path: {strat}\n"
        "glm: {chat_model: glm-5.2, reasoner_model: glm-5.2}\n"
        "risk: {max_dollars_per_trade: 100, daily_loss_halt_pct: 20, daily_capital_deploy_cap: 1000, "
        "min_buying_power_buffer: 5, max_actions_per_ticker_per_day: 1, max_analyses_per_ticker_per_day: 1, "
        "rebalance_enabled: true, cash_sleeve_ticker: SGOV}\nnotify: {enabled: false}\n",
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

    # --- Q1 (always on): no analyses -> static book, byte-identical (the iron regression guard) ---
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d))
        out = tick._run_construct(cfg, led, dict(base))
        w = {t: out["target_weights"][t]["target_weight"] for t in out["target_weights"]}
        ok("Q1 no-conviction tick -> static book (byte-identical)",
           w == {"AAA": 30.0, "BBB": 30.0, "SGOV": 40.0} and out["conviction_weights"] is False, w)

    # --- Q1: an all-Hold tick -> allocation skipped -> static book (the common production case) ---
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d))
        an = [{"ticker": "AAA", "signal": "Hold", "conviction": 60, "uncertainty": 50},
              {"ticker": "BBB", "signal": "Hold", "conviction": 55, "uncertainty": 50}]
        out = tick._run_construct(cfg, led, {**base, "analyses": an})
        w = {t: out["target_weights"][t]["target_weight"] for t in out["target_weights"]}
        ok("Q1 all-Hold analyses -> static book (skip alloc; never sells to cash)",
           w == {"AAA": 30.0, "BBB": 30.0, "SGOV": 40.0} and out["conviction_weights"] is False, w)

    # --- Q1: conviction differentiates same-sleeve names ---
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d), per_name=80)
        an = [{"ticker": "AAA", "signal": "Buy", "conviction": 90, "uncertainty": 10},
              {"ticker": "BBB", "signal": "Buy", "conviction": 30, "uncertainty": 10}]
        out = tick._run_construct(cfg, led, {**base, "analyses": an})
        w = {t: out["target_weights"][t]["target_weight"] for t in out["target_weights"]}
        ok("Q1 higher conviction => higher weight", w["AAA"] > w["BBB"], w)
        ok("Q1 conviction allocation engaged", out["conviction_weights"] is True, out.get("conviction_weights"))
        ok("Q1 cash floor respected (>= 10%)", w["SGOV"] >= 10.0, w)
        ok("Q1 book conserves to ~100%", abs(sum(w.values()) - 100.0) < 0.5, sum(w.values()))

    # --- Q1: per-name cap binds (both pinned at the cap) ---
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d), per_name=40)
        an = [{"ticker": "AAA", "signal": "Buy", "conviction": 90, "uncertainty": 10},
              {"ticker": "BBB", "signal": "Buy", "conviction": 30, "uncertainty": 10}]
        out = tick._run_construct(cfg, led, {**base, "analyses": an})
        w = {t: out["target_weights"][t]["target_weight"] for t in out["target_weights"]}
        ok("Q1 per-name cap binds (both <= 40)", w["AAA"] <= 40.01 and w["BBB"] <= 40.01, w)

    # --- F3: conviction-driven deployment (reallocation teeth) at PRODUCTION config ------------
    # An all-Underweight book must DEPLOY LESS -> RAISE CASH. Proven by an A/B on the SAME book at
    # SHIPPED rails (smoothing_alpha=0.4, conviction_rebalance_min_delta_pct=2.0 default): F3-ON
    # (default floor 0.70) vs F3-OFF (min_factor=1.0). The A/B cancels book-size/normalization
    # artifacts and — critically — GUARDS the v1 no-op the adversarial review caught: with F3 OFF
    # the 2pt per-name hysteresis REVERTS the Underweight to the static book (0 cash raised); F3's
    # post-hysteresis carve-out is what actually raises cash.
    _bear_holds = [
        "      - {sleeve: Tech, ticker: AAA, weight: 20, band: 3}",
        "      - {sleeve: Tech, ticker: BBB, weight: 20, band: 3}",
        "      - {sleeve: Pwr, ticker: CCC, weight: 20, band: 3}",
        "      - {sleeve: Pwr, ticker: DDD, weight: 20, band: 3}",
        "      - {sleeve: Cash, ticker: SGOV, weight: 20}"]
    _bear_rh = "[AAA, BBB, CCC, DDD, SGOV]"
    _engines = ("AAA", "BBB", "CCC", "DDD")
    _an_uw = [{"ticker": t, "signal": "Underweight", "conviction": 40, "uncertainty": 30}
              for t in _engines]
    with tempfile.TemporaryDirectory() as d1:
        cfg_on, led_on = _mk(Path(d1), rh=_bear_rh, cash_floor=5, smoothing_alpha=0.4,
                             holdings=_bear_holds)
        out_on = tick._run_construct(cfg_on, led_on, {**base, "analyses": _an_uw})
        tw_on = out_on["target_weights"]
        cash_on = tw_on["SGOV"]["target_weight"]
        eng_on = sum(tw_on[t]["target_weight"] for t in _engines)
    with tempfile.TemporaryDirectory() as d2:
        cfg_off, led_off = _mk(Path(d2), rh=_bear_rh, cash_floor=5, smoothing_alpha=0.4,
                               holdings=_bear_holds, dep_min_factor=1.0)  # F3 disabled
        out_off = tick._run_construct(cfg_off, led_off, {**base, "analyses": _an_uw})
        tw_off = out_off["target_weights"]
        cash_off = tw_off["SGOV"]["target_weight"]
        eng_off = sum(tw_off[t]["target_weight"] for t in _engines)
    ok("F3 e2e: bearish book RAISES CASH vs F3-off", cash_on > cash_off + 2.0, (cash_on, cash_off))
    ok("F3 e2e: bearish book DEPLOYS LESS vs F3-off", eng_on < eng_off - 2.0, (eng_on, eng_off))
    ok("F3 e2e: F3-off reverts Underweight to ~static (the v1 no-op it guards)",
       abs(cash_off - 20.0) < 3.0, cash_off)
    ok("F3 e2e: book still conserves to ~100%", abs(cash_on + eng_on - 100.0) < 0.5, cash_on + eng_on)
    # Drive construct->plan: positions held ABOVE F3's lowered targets by more than the band+min
    # -> F3's de-risk emits trim orders that actually raise cash (reaches the broker path, not just
    # the target map). NB item 1 (trim-to-edge + $5 min) damps a sub-band F3 de-risk to no trade;
    # a materially overweight book is what makes F3's cash-raise reach the broker.
    with tempfile.TemporaryDirectory() as d3:
        cfg_p, led_p = _mk(Path(d3), rh=_bear_rh, cash_floor=5, smoothing_alpha=0.4,
                           holdings=_bear_holds)
        snap = {"equity": 1000.0, "buying_power": 200.0,
                "positions": {t: {"quantity": 3.0, "market_value": 300.0} for t in _engines},
                "quotes": {"AAA": 100.0, "BBB": 100.0, "CCC": 100.0, "DDD": 100.0, "SGOV": 1.0},
                "now_iso": "2026-06-20T11:00:00-04:00"}
        con = tick._run_construct(cfg_p, led_p, {**snap, "analyses": _an_uw})
        plan = tick._run_plan(cfg_p, led_p, {**snap, "run_id": "E2E-F3", "analyses": _an_uw,
                                             "target_weights": con["target_weights"]})
        _trims = [o for o in plan["orders"] if o.get("order_kind") == "rebalance_trim"]
        ok("F3 e2e: bearish book emits rebalance_trim orders (raises cash to the broker)",
           len(_trims) > 0, [o.get("order_kind") for o in plan["orders"]])

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
        cfg, led = _mk(Path(d), rh="[AAA, NVDA, SGOV]",
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
        cfg, led = _mk(Path(d), auto_apply=True,
                       rh="[AAA, NVDA, SGOV]", sleeves=_sleeves, holdings=_holds)
        gid = led.get_active_goal()["id"]
        rid = led.record_universe_proposal(
            goal_id=gid, proposed_at="2026-06-20T10:00:00", kind="PROPOSE_ADD", ticker="NVDA",
            sleeve="US large cap", from_book=None, to_book=None, target_weight=8, tier="universe",
            content_hash="h_auto", reason="t", goal_gap_pct=None)
        res = tick._run_universe_apply(cfg, led, change_id=rid, approve=False)  # auto, recurrence 1 < 2
        ok("Q3 AUTO ADD refused until confirmed over N days",
           res.get("applied") is False and "confirmed" in (res.get("reason") or ""), res)

    # --- F4 (churn-fix guard): auto_apply_universe_changes=false -> an AUTO REMOVE is NOT applied
    # without a human --approve. The learning layer currently proposes whole-book removals on
    # "sustained underperformance" (a SYMPTOM of the bearish brain, not bad names); this pins the
    # flag gate at tick.py:_run_universe_apply so a refactor can't silently start evicting the book.
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d))  # auto_apply=False (the default, matches production)
        gid = led.get_active_goal()["id"]
        before = {r["ticker"]: r["status"]
                  for r in led.active_target_portfolio(gid, statuses=("active", "exiting", "removed"))}
        rid = led.record_universe_proposal(
            goal_id=gid, proposed_at="2026-06-20T10:00:00", kind="PROPOSE_REMOVE", ticker="AAA",
            sleeve="Tech", from_book=None, to_book=None, target_weight=0, tier="universe",
            content_hash="h_f4_remove", reason="sustained underperformance", goal_gap_pct=None)
        res = tick._run_universe_apply(cfg, led, change_id=rid, approve=False)
        ok("F4: AUTO REMOVE refused (auto_apply_universe_changes off -> needs --approve)",
           res.get("applied") is False and "auto_apply_universe_changes" in (res.get("reason") or ""), res)
        after = {r["ticker"]: r["status"]
                 for r in led.active_target_portfolio(gid, statuses=("active", "exiting", "removed"))}
        ok("F4: the book is UNCHANGED after the refused AUTO remove (AAA still active)",
           after.get("AAA") == "active" and after == before, (before, after))
        # A human --approve IS the deliberate gate -> it applies (AAA -> exiting).
        res2 = tick._run_universe_apply(cfg, led, change_id=rid, approve=True)
        ok("F4: human --approve applies the REMOVE (the guard is a gate, not a hard block)",
           res2.get("applied") is True
           and bool(led.active_target_portfolio(gid, statuses=("exiting",))), res2)

    # --- Q3: a non-allow-listed candidate is blocked at apply by validate_add ---
    with tempfile.TemporaryDirectory() as d:
        cfg, led = _mk(Path(d), rh="[AAA, SGOV]", sleeves=_sleeves, holdings=_holds)
        gid = led.get_active_goal()["id"]
        rid = led.record_universe_proposal(
            goal_id=gid, proposed_at="2026-06-20T10:00:00", kind="PROPOSE_ADD", ticker="TSLA",
            sleeve="US large cap", from_book=None, to_book=None, target_weight=8, tier="universe",
            content_hash="h_block", reason="t", goal_gap_pct=None)
        res = tick._run_universe_apply(cfg, led, change_id=rid, approve=True)
        ok("Q3 non-allow-listed ADD blocked by validate_add",
           res.get("applied") is False and "refusing add" in (res.get("reason") or ""), res)

    # === F4 multi-agent assembly seam (deterministic; no tokens) ============
    # Proves decide.mjs's assembly glue (sanitize ## -> ###, prepend canonical
    # headers once, extract the 12 labels) yields a contract that analyze.py's
    # _split_eve_markdown + _validate_contract + extract_fields accept — incl.
    # the adversarial case where a turn body contains a stray `## Rationale`.
    import analyze as _az
    import lib.rating as _rating

    # Simulated turn outputs (what decide.mjs's deep/quick turns would emit).
    # The gather turn emits ### sub-reports (NOT ##).
    gather_out = """### market_report
Price 195, RSI 58, volume up.
### trend_report
ADX 31, regime UPTREND, Sharpe 1.2, max DD -14%.
### fundamentals_report
PE 24, market cap 3T.
### news_report
earnings beat.
### sentiment_report
bullish 0.7."""
    # The trader turn — with a STRAY `## Rationale` mid-block (the Gate-B
    # adversarial case): decide.mjs must sanitize it so the splitter keeps all
    # 8 labels under trader_investment_plan.
    trader_turn = """**Action**: Buy
**Entry Price**: 195
**Stop Loss**: 180
**Position Sizing**: ~5% of capital
**Position Pct**: 5
**Strategy Basis**: multi-quarter AI-capex uptrend
## Rationale
because ADX 31 and 200d slope positive
**Catalyst**: none
**Target Price**: 240"""
    pm_turn = """**Rating**: Buy
**Next Review Hours**: 48
**Conviction**: 72
**Uncertainty**: 28
The trend is persistent; ride it."""

    # Replicate decide.mjs's sanitize + extract + prepend-headers-once.
    #
    # NB this is a SIMULATION of the producer, not the producer: the real one is
    # quiver_eve/run/contract.mjs:sanitize. Keep it in step or this suite silently keeps modelling
    # an old brain. It is deliberately NOT the proof of sanitize's behaviour — that lives in
    # quiver_eve/test/contract_helpers.test.mjs (which imports the REAL function) and in the
    # producer-derived DA rows in tests/test_units.py (which drive it through `node`).
    #
    # Mirrors the hardened rule: downgrade any line starting `##` that is not `###`, splitting on
    # EVERY boundary str.splitlines() recognizes. Both are broader than analyze.py's `^##\s+`
    # splitter on purpose — matching the classes by hand left measured gaps that let a model-authored
    # body overwrite the gated market_report.
    def _sanitize(body):
        return "\n".join(("###" + l[2:]) if re.match(r"^##(?!#)", l) else l
                         for l in str(body or "").splitlines()).strip()

    def _extract(text, labels):
        lines = (text or "").split("\n")
        out = []
        for lab in labels:
            import re
            re_lbl = re.compile(r"\*\*" + lab.replace(" ", r"\s+") + r"(?::\*\*|\*\*:)\s*(.+)", re.I)
            hit = next((l for l in lines if re_lbl.search(l)), None)
            if hit:
                out.append(hit.strip())
        return "\n".join(out) if out else text.strip()

    TRADER = ["Action","Entry Price","Stop Loss","Position Sizing","Position Pct","Strategy Basis","Catalyst","Target Price"]
    PM = ["Rating","Next Review Hours","Conviction","Uncertainty"]
    def grab_sub(body, name):
        import re
        m = re.search(r"###\s+" + name + r"\s*\n([\s\S]*?)(?=###\s+|$)", body, re.I)
        return m.group(1).strip() if m else "UNAVAILABLE"

    final = "\n\n".join([
        "## market_report", _sanitize(grab_sub(gather_out, "market_report")),
        "## trend_report", _sanitize(grab_sub(gather_out, "trend_report")),
        "## sentiment_report", _sanitize(grab_sub(gather_out, "sentiment_report")),
        "## news_report", _sanitize(grab_sub(gather_out, "news_report")),
        "## fundamentals_report", _sanitize(grab_sub(gather_out, "fundamentals_report")),
        "## trader_investment_plan", _sanitize(_extract(trader_turn, TRADER)),
        "## final_trade_decision", _sanitize(_extract(pm_turn, PM)),
        "## lever_proposals", "none",
    ])

    # 1. the splitter yields exactly the 8 canonical sections, each non-empty.
    pseudo = _az._split_eve_markdown(final)
    ok("F4 seam: 8 canonical sections present",
       all(k in pseudo for k in ("market_report","trend_report","sentiment_report","news_report",
          "fundamentals_report","trader_investment_plan","final_trade_decision","lever_proposals")), list(pseudo.keys()))
    # 2. the stray `## Rationale` did NOT truncate the trader labels (the
    #    sanitizer downgraded it to `### Rationale` so the splitter kept the
    #    Target Price line after it).
    ok("F4 seam: stray ## Rationale sanitized (all 8 trader labels kept)",
       "**Target Price**" in (pseudo.get("trader_investment_plan") or ""),
       pseudo.get("trader_investment_plan"))
    # 3. the F6 contract gate PASSES on the assembled output (12 labels + basis).
    try:
        _az._validate_contract(pseudo)
        ok("F4 seam: F6 validator passes assembled contract", True)
    except RuntimeError as e:
        ok("F4 seam: F6 validator passes assembled contract", False, str(e))
    # 4. extract_fields + parse_rating yield a valid 5-tier signal (the live seam).
    fields = _az.extract_fields(pseudo, _rating.parse_rating(pseudo.get("final_trade_decision", "")), "AAPL")
    ok("F4 seam: signal derived as Buy", fields["signal"], "Buy")
    ok("F4 seam: basis carried through", fields["basis"], "multi-quarter AI-capex uptrend")
    ok("F4 seam: trend_report captured in audit dump keys", "trend_report" in _az._EVE_SECTIONS)

    print(f"\nE2E pipeline: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
