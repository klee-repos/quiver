#!/usr/bin/env python3
"""End-to-end test of the legislative-catalyst pipeline — OFFLINE, deterministic.

Drives the REAL `tick.py:_run_bills_review` + `_run_universe_apply` in-process, injecting
FAKE callables for poll/detail/text/analyze/judge (the dependency-injection seam), so the
FULL chain — ingest -> heuristic passage gate -> LLM analysis -> adversarial judge ->
actionable filter -> proposal mint -> human-gated apply -> reconcile — runs with ZERO
network/subprocess/LLM/broker. Assertions are against REAL ledger rows (universe_change_log,
target_portfolio, bills.applied), not return dicts alone.

Run: .venv/bin/python tests/test_e2e_catalyst.py
"""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml  # noqa: E402

import tick as T  # noqa: E402
import lib.legislative as L  # noqa: E402
from lib import market  # noqa: E402
from lib.config import load_config  # noqa: E402
from lib.ledger import Ledger  # noqa: E402

_REPO = Path(__file__).resolve().parent.parent
FIXTURE = _REPO / "tests" / "fixtures" / "strategy_fixture.yaml"
PASS = 0
FAIL = 0


def ok(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ FAIL: {name}")


def mk(*, enabled=True, remove_enabled=False, tpa=None, auto_universe=False):
    """A fresh isolated environment: temp ledger + config (legislative enabled) + seeded goal.
    ``auto_universe`` flips learning.auto_apply_universe_changes ON in the strategy so we can
    prove the legislative tier STILL refuses auto-apply even with the universe flag set."""
    d = Path(tempfile.mkdtemp(prefix="quiver_catalyst_"))
    strat = d / "strategy.yaml"
    _sdict = yaml.safe_load(FIXTURE.read_text(encoding="utf-8"))
    if auto_universe:
        _sdict.setdefault("learning", {})["auto_apply_universe_changes"] = True
    strat.write_text(yaml.safe_dump(_sdict), encoding="utf-8")
    cfgd = {
        "account_number": "12345678", "dry_run": True, "kill_switch_file": str(d / "KILL"),
        "strategy_path": str(strat),
        "risk": {"max_dollars_per_trade": 100, "daily_loss_halt_pct": 50.0,
                 "daily_capital_deploy_cap": 1000, "min_buying_power_buffer": 5, "rebalance_enabled": True},
        "glm": {"chat_model": "glm-5.2", "reasoner_model": "glm-5.2"},
        "legislative": {
            "enabled": enabled, "passage_prob_min": 0.55, "impact_min": 0.5,
            "judge_enabled": True, "judge_votes": 3, "judge_pass_min": 2,
            "max_proposals_per_review": 3, "max_bills_analyzed_per_review": 15,
            "add_weight": 4.0, "remove_enabled": remove_enabled,
            "ticker_policy_areas": (tpa or {}),
        },
    }
    p = d / "config.yaml"
    p.write_text(yaml.safe_dump(cfgd), encoding="utf-8")
    os.environ["CONGRESS_API_KEY"] = "TESTKEY"  # api_key resolved at load
    cfg = load_config(str(p))
    led = Ledger(d / "ledger.db")
    now = market.now_et().isoformat()
    T._run_strategy_set(cfg, led, {"equity": 100.0, "now_iso": now})
    goal = led.get_active_goal()
    return cfg, led, goal


# --- fake callables (the DI seam; no network/LLM) --------------------------------------
def _bill(bid="119-hr-1", n=1, la="Passed House"):
    return {"bill_id": bid, "congress": 119, "bill_type": "hr", "number": n, "title": "Test Bill",
            "latest_action": la, "latest_action_date": "2026-07-10",
            "update_date": "2026-07-10T00:00:00Z", "update_date_including_text": f"2026-07-10T01:00:00Z-{bid}"}


def poll_of(bills):
    return lambda since, until, key: [dict(b) for b in bills]


def detail_of(status="passed_both", policy=("Energy",)):
    return lambda c, t, n, k: {"status": status, "policy_codes": list(policy), "title": "Test Bill",
                               "latest_action": "x", "text_versions_url": None}


def text_of(txt="bill text body"):
    return lambda detail: txt


def analyze_of(impacts, thesis="t"):
    return lambda meta, txt: {"impacted_tickers": list(impacts), "thesis": thesis}


def judge_of(verdict):
    return lambda meta, analysis, *, votes, pass_min: {"verdict": verdict, "reason": f"{verdict}", "votes_json": "[]"}


def _imp(ticker, direction="benefit", mag=0.8):
    return {"ticker": ticker, "direction": direction, "magnitude": mag, "horizon": "medium",
            "rationale": f"{ticker} rationale", "sector": "x"}


def run(cfg, led, bills, *, detail=None, analyze=None, judge=None, text=None):
    return T._run_bills_review(
        cfg, led, {}, poll=poll_of(bills),
        detail_fetch=detail or detail_of("passed_both"),
        text_fetch=text or text_of(),
        analyze=analyze or analyze_of([_imp("COIN")]),
        judge=judge or judge_of("pass"))


print("=" * 70)
print("LEGISLATIVE-CATALYST E2E (offline, injected fakes, real ledger assertions)")
print("=" * 70)

# --- A) happy path: high-passage + high-impact + judge-PASS -> proposal -> human apply ---
cfg, led, goal = mk()
r = run(cfg, led, [_bill()])
ok("A: reviewed", r.get("reviewed") is True)
ok("A: analyzed 1", r.get("analyzed") == 1)
ok("A: minted 1", r.get("minted") == 1)
props = led.pending_universe_changes(goal["id"])
ok("A: exactly one proposed row", len(props) == 1)
ok("A: proposal is PROPOSE_ADD/COIN/tier=legislative",
   props and props[0]["kind"] == "PROPOSE_ADD" and props[0]["ticker"] == "COIN"
   and props[0]["tier"] == "legislative")
rid = props[0]["id"]
# daily dedup: a second same-day review no-ops
r_again = run(cfg, led, [_bill()])
ok("A: daily-dedup blocks a second same-day ingest", r_again.get("reason") == "already ran today")
# human gate: approve=False refuses with the tell-tale reason; approve=True applies
deny = T._run_universe_apply(cfg, led, change_id=rid, approve=False)
ok("A: approve=False -> applied False", deny.get("applied") is False)
ok("A: refusal reason names --approve", "requires --approve" in str(deny.get("reason")))
appl = T._run_universe_apply(cfg, led, change_id=rid, approve=True)
ok("A: approve=True -> applied True", appl.get("applied") is True)
book = led.active_target_portfolio(goal["id"], statuses=("active",))
ok("A: COIN added to book at add_weight 4%",
   any(h["ticker"] == "COIN" and abs(h["target_weight"] - 4.0) < 0.01 for h in book))
ok("A: book conserves to ~100%", abs(sum(h["target_weight"] for h in book) - 100.0) < 0.6)
ok("A: COIN funded from cash (SGOV 45 -> ~41)",
   any(h["ticker"] == "SGOV" and abs(h["target_weight"] - 41.0) < 0.01 for h in book))

# reconcile (simulate next trading day by rewinding the cursor) marks the bill applied;
# re-polling the SAME bill mints NOTHING (no orphan re-mint / cash re-debit).
with led.legislative_conn() as c:
    L.set_cursor(c, "2026-07-01", "2026-07-01", "2026-07-01T00:00:00")
r2 = run(cfg, led, [_bill()])
ok("A: reconcile marked the applied bill", r2.get("reconciled", 0) >= 1)
ok("A: no re-mint after apply", len(led.pending_universe_changes(goal["id"])) == 0)
ok("A: cash not re-debited (SGOV still ~41)",
   any(h["ticker"] == "SGOV" and abs(h["target_weight"] - 41.0) < 0.01
       for h in led.active_target_portfolio(goal["id"], statuses=("active",))))

# --- B) benefit on a NON-allow-listed ticker -> no proposal -----------------------------
cfg, led, goal = mk()
rB = run(cfg, led, [_bill()], analyze=analyze_of([_imp("PLTR")]))  # PLTR not in fixture allow-list
ok("B: off-allow-list benefit mints nothing", rB.get("minted") == 0)
ok("B: no proposed rows", len(led.pending_universe_changes(goal["id"])) == 0)

# --- C) below the passage gate -> never spends an LLM call, no proposal ------------------
cfg, led, goal = mk()
rC = run(cfg, led, [_bill()], detail=detail_of("committee"))  # prob ~0.05 < 0.55
ok("C: below-gate bill counted, not analyzed", rC.get("below_gate", 0) >= 1 and rC.get("analyzed") == 0)
ok("C: no proposal below passage gate", len(led.pending_universe_changes(goal["id"])) == 0)

# --- C2) already-enacted bill -> skipped (no front-run edge), no LLM, no proposal ---------
cfg, led, goal = mk()
rC2 = run(cfg, led, [_bill()], detail=detail_of("became_law"))
ok("C2: became_law skipped (not front-runnable, prob honestly 1.0)",
   rC2.get("skipped_enacted", 0) >= 1 and rC2.get("analyzed") == 0)
ok("C2: no proposal for an already-enacted bill", len(led.pending_universe_changes(goal["id"])) == 0)

# --- D) judge FAIL -> not actionable -> no proposal -------------------------------------
cfg, led, goal = mk()
rD = run(cfg, led, [_bill()], judge=judge_of("fail"))
ok("D: judged at least one bill", rD.get("judged", 0) >= 1)
ok("D: judge-FAIL mints nothing", rD.get("minted") == 0 and len(led.pending_universe_changes(goal["id"])) == 0)

# --- E) malformed/empty analysis -> fail-safe, no proposal, no trade ---------------------
cfg, led, goal = mk()
rE = run(cfg, led, [_bill()], analyze=analyze_of([]))  # LLM returned nothing usable
ok("E: fail-safe empty analysis mints nothing", rE.get("minted") == 0)
ok("E: no proposed rows on empty analysis", len(led.pending_universe_changes(goal["id"])) == 0)

# --- F) feature disabled -> hard no-op --------------------------------------------------
cfg, led, goal = mk(enabled=False)
rF = run(cfg, led, [_bill()])
ok("F: disabled -> reviewed False", rF.get("reviewed") is False and rF.get("reason") == "disabled")

# --- G) two bills -> same ticker -> ONE proposal; apply funds once; reconcile marks BOTH --
cfg, led, goal = mk()
rG = run(cfg, led, [_bill("119-hr-1", 1), _bill("119-hr-2", 2)],
         analyze=analyze_of([_imp("COIN")]))
ok("G: two bills collapse to one COIN proposal", rG.get("minted") == 1)
propsG = led.pending_universe_changes(goal["id"])
ok("G: exactly one proposed row", len(propsG) == 1)
T._run_universe_apply(cfg, led, change_id=propsG[0]["id"], approve=True)
bookG = led.active_target_portfolio(goal["id"], statuses=("active",))
ok("G: COIN funded ONCE (SGOV 45 -> ~41, not 37)",
   any(h["ticker"] == "SGOV" and abs(h["target_weight"] - 41.0) < 0.01 for h in bookG))
with led.legislative_conn() as c:
    L.set_cursor(c, "2026-07-01", "2026-07-01", "2026-07-01T00:00:00")
rG2 = run(cfg, led, [_bill("119-hr-1", 1), _bill("119-hr-2", 2)])
ok("G: reconcile marks BOTH contributing bills applied", rG2.get("reconciled", 0) == 2)
ok("G: no orphan re-mint", len(led.pending_universe_changes(goal["id"])) == 0)

# --- H) legislative tier can NEVER auto-apply (even with the universe flag ON) -----------
cfg, led, goal = mk(auto_universe=True)
ok("H: fixture really has auto_apply_universe_changes ON",
   getattr(cfg.strategy.learning, "auto_apply_universe_changes", False) is True)
rH = run(cfg, led, [_bill()])
ridH = led.pending_universe_changes(goal["id"])[0]["id"]
autoH = T._run_universe_apply(cfg, led, change_id=ridH, approve=False)
ok("H: legislative tier is never auto-eligible (still requires --approve)",
   autoH.get("applied") is False and "requires --approve" in str(autoH.get("reason")))

# --- I) suffer->REMOVE requires held + allow + policy-area map (GB1) ---------------------
# held SMH (in fixture book + allow-list); a suffer bill tagged 'Semiconductors' with a map hit.
cfg, led, goal = mk(remove_enabled=True, tpa={"SMH": ["Semiconductors"]})
rI = run(cfg, led, [_bill()], detail=detail_of("passed_both", ["Semiconductors"]),
         analyze=analyze_of([_imp("SMH", direction="suffer")]))
propsI = led.pending_universe_changes(goal["id"])
ok("I: REMOVE minted for held+allow+policy-map suffer", any(p["kind"] == "PROPOSE_REMOVE" and p["ticker"] == "SMH" for p in propsI))
# same bill but NO policy-area map -> no REMOVE
cfg2, led2, goal2 = mk(remove_enabled=True, tpa={"SMH": ["Health"]})  # code mismatch
rI2 = run(cfg2, led2, [_bill()], detail=detail_of("passed_both", ["Semiconductors"]),
          analyze=analyze_of([_imp("SMH", direction="suffer")]))
ok("I: no REMOVE when policy codes don't overlap",
   not any(p["kind"] == "PROPOSE_REMOVE" for p in led2.pending_universe_changes(goal2["id"])))

# --- J) judge eval: re-run the judge over human labels -> agreement + pass_min sweep (offline) ---
import json  # noqa: E402
import types as _types  # noqa: E402
cfg, led, goal = mk()
_lf = str(Path(tempfile.mkdtemp()) / "labels.jsonl")
with open(_lf, "w", encoding="utf-8") as _f:
    _f.write(json.dumps({"bill_id": "b1", "title": "t", "status": "passed_both",
                         "impacted_tickers": [_imp("COIN")], "thesis": "x", "human_verdict": "pass"}) + "\n")
    _f.write(json.dumps({"bill_id": "b2", "title": "t", "status": "passed_both",
                         "impacted_tickers": [_imp("XYZ")], "thesis": "x", "human_verdict": "fail"}) + "\n")


def _fake_judge(meta, analysis, *, votes, pass_min):
    cast = ["pass", "pass", "pass"] if meta["bill_id"] == "b1" else ["pass", "fail", "fail"]
    return {"verdict": "pass", "reason": "", "votes_json": json.dumps({"votes": cast})}


_jargs = _types.SimpleNamespace(labels=_lf, judge_votes="3")
rJ = T._run_judge_eval(cfg, led, _jargs, judge=_fake_judge)
ok("J: judge eval scored both labeled rows", rJ.get("n") == 2)
ok("J: pass_min sweep spans 1..3", [s["pass_min"] for s in rJ.get("sweep", [])] == [1, 2, 3])
_pm3 = next((s for s in rJ.get("sweep", []) if s["pass_min"] == 3), None)
ok("J: pass_min=3 gives perfect agreement on this set (b1 pass, b2 fail)",
   _pm3 is not None and _pm3["accuracy"] == 1.0)
ok("J: best-precision operating point returned", rJ.get("best_precision") is not None)

# --- K) labeling corpus round-trips (catalyst-seed writes / catalyst-label reads the JSONL) ---
_cp = str(Path(tempfile.mkdtemp()) / "corpus.jsonl")
T._save_corpus(_cp, [{"bill_id": "b1", "human_verdict": None, "impacted_tickers": [_imp("COIN")]},
                     {"bill_id": "b2", "human_verdict": "pass"}])
_back = T._load_corpus(_cp)
ok("K: corpus round-trips", len(_back) == 2 and _back[0]["bill_id"] == "b1")
ok("K: un-labeled rows are distinguishable from labeled", sum(1 for r in _back if not r.get("human_verdict")) == 1)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
