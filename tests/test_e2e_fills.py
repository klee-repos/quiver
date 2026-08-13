"""E2E: capture the REAL fill from the broker.

The ledger never recorded what the bot paid. `orders.result_json` is assembled by
the ORCHESTRATOR at PLACE time, so it drifted into 15 distinct key shapes over 159
orders and is wrong as a price: BOT order 6a79ec24 stored 27.59 against an actual
`average_price` of 27.949900.

Python now parses the broker answer, keyed on `broker_order_id`.

The load-bearing case is the PARTIAL fill. A `partially_filled` order carries a
REAL partial `average_price`, so retiring the row on a price would freeze the
share count. Live proof on this account: BSOL 6a75fa00 has two executions summing
1.697570; a capture after the first stores 1.000000, which is 59% of the truth.

No network, no broker, no LLM. Payload shapes are copied from real MCP responses.
"""
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tick                                      # noqa: E402
from lib.ledger import Ledger                     # noqa: E402

PASS = 0
FAILED = []


def ok(name, cond, extra=""):
    global PASS
    if cond:
        PASS += 1
    else:
        FAILED.append(name)
        print("  FAIL %s   -> %r" % (name, extra))


def _led():
    return Ledger(str(Path(tempfile.mkdtemp()) / "l.db"))


def _order(led, ref, ticker, bid, kind="rebalance_buy", side="buy"):
    led.reserve_order(ref, "2026-08-12", ticker, side=side, type="market",
                      dollar_amount=26.9, quantity=None,
                      now_iso="2026-08-12T10:00:00-04:00", order_kind=kind)
    led.finalize_order(ref, bid, "{}")


# --- the parser, against REAL broker payload shapes -------------------------
# copied verbatim from get_equity_orders on account 455737171
FILLED = {"id": "6a79ec24", "symbol": "BOT", "state": "filled",
          "average_price": "27.949900", "cumulative_quantity": "0.962436",
          "quantity": "0.962436", "fees": "0.000000"}
PARTIAL = {"id": "6a75fa00", "symbol": "BSOL", "state": "partially_filled",
           "average_price": "10.445000", "cumulative_quantity": "1.000000",
           "quantity": "1.697570", "fees": "0.000000"}
ZEROED = {"id": "Z", "symbol": "X", "state": "unconfirmed",
          "average_price": "0.0000", "cumulative_quantity": "0.0000"}

f = tick._parse_broker_fill(FILLED)
ok("parses the real average_price, not the submission quote", f["avg_price"] == 27.9499, f)
ok("parses the share count a dollar-based buy never stored", f["quantity"] == 0.962436, f)
ok("keeps fees as a real 0.0", f["fees"] == 0.0, f)
ok("carries the broker state", f["fill_state"] == "filled", f)

z = tick._parse_broker_fill(ZEROED)
ok("a zero price is NOT a fill (None, never 0.0)", z["avg_price"] is None, z)
ok("a zero quantity is NOT a fill", z["quantity"] is None, z)

p = tick._parse_broker_fill(PARTIAL)
ok("a partial reports cumulative_quantity, not the ordered quantity",
   p["quantity"] == 1.0, p)

# --- retire on STATE, never on price ----------------------------------------
led = _led()
_order(led, "r_part", "BSOL", "6a75fa00")
ok("the order starts pending", len(led.fills_pending()) == 1)
led.record_fill("r_part", avg_price=p["avg_price"], quantity=p["quantity"],
                fees=p["fees"], fill_state=p["fill_state"], now_iso="t1")
ok("a PARTIAL fill stays pending (a price key would freeze 59% of the truth)",
   len(led.fills_pending()) == 1, led.fills_pending())
# next tick: the same order completes
done = dict(PARTIAL, state="filled", cumulative_quantity="1.697570")
d2 = tick._parse_broker_fill(done)
led.record_fill("r_part", avg_price=d2["avg_price"], quantity=d2["quantity"],
                fees=d2["fees"], fill_state=d2["fill_state"], now_iso="t2")
ok("it retires once the broker says filled", len(led.fills_pending()) == 0)
row = led.get_order("r_part")
ok("and the FULL share count lands, not the partial",
   abs(float(row["fill_quantity"]) - 1.697570) < 1e-9, row["fill_quantity"])

# --- every terminal state retires; non-terminal holds ------------------------
for state, should_retire in (("filled", True), ("cancelled", True), ("rejected", True),
                             ("failed", True), ("voided", True), ("not_found", True),
                             ("unconfirmed", False), ("partially_filled", False),
                             ("queued", False), ("confirmed", False)):
    L = _led()
    _order(L, "r", "AAA", "B1")
    L.record_fill("r", avg_price=None, quantity=None, fees=None,
                  fill_state=state, now_iso="t")
    retired = len(L.fills_pending()) == 0
    ok("state %-17s -> %s" % (state, "retires" if should_retire else "stays pending"),
       retired == should_retire, (state, retired))

# --- an order the broker no longer returns must not loop forever ------------
led = _led()
_order(led, "r_gone", "AAA", "GONE")
Path(led.db_path)  # touch, keeps linters quiet
inp = Path(tempfile.mkdtemp()) / "fills.json"
inp.write_text('{"orders": []}', encoding="utf-8")
ok("a missing order is pending before the run", len(led.fills_pending()) == 1)

# --- the broker state must NEVER overwrite our lifecycle state --------------
# open_protective_stops keys on state='stop_placed'. A broker state written there
# would hide a resting stop from the cancel-before-sell step.
led = _led()
_order(led, "r_stop", "AAA", "S1", kind="protective_stop", side="sell")
led.set_order_state("r_stop", "stop_placed")
led.record_fill("r_stop", avg_price=None, quantity=None, fees=None,
                fill_state="cancelled", now_iso="t")
row = led.get_order("r_stop")
ok("fill_state does not touch orders.state", row["state"] == "stop_placed", row["state"])
ok("fill_state is stored separately", row["fill_state"] == "cancelled", row["fill_state"])

# --- result_json is preserved as the audit trail ----------------------------
led = _led()
_order(led, "r_aud", "AAA", "A1")
led.record_fill("r_aud", avg_price=1.0, quantity=2.0, fees=0.0,
                fill_state="filled", now_iso="t")
ok("result_json survives the capture", led.get_order("r_aud")["result_json"] == "{}")

# --- REGRESSION (live run, 2026-08-13): a narrow window must not retire old orders
# The first live run passed a window starting 2026-08-05 against a pending set
# reaching back to 2026-06-16. Every older order was absent from the response and
# was marked `not_found`, which is TERMINAL — 139 real orders were retired forever.
import json as _json
led = _led()
_order(led, "r_old", "PLTR", "OLD1")
_order(led, "r_new", "ANET", "NEW1")
with led._conn() as _c:
    _c.execute("UPDATE orders SET submitted_at=? WHERE ref_id=?",
               ("2026-06-16T10:00:00-04:00", "r_old"))
    _c.execute("UPDATE orders SET submitted_at=? WHERE ref_id=?",
               ("2026-08-10T10:00:00-04:00", "r_new"))

_inp = Path(tempfile.mkdtemp()) / "fills.json"
_inp.write_text(_json.dumps({"created_at_gte": "2026-08-05", "orders": []}), encoding="utf-8")


class _A:
    input = str(_inp)


import lib.config as _cfgmod                                    # noqa: E402
_orig = tick._cfg_and_ledger
tick._cfg_and_ledger = lambda: (_orig()[0], led)
try:
    out = tick.cmd_fills(_A())
finally:
    tick._cfg_and_ledger = _orig

ok("an order OLDER than the window is not retired",
   led.get_order("r_old")["fill_state"] is None, led.get_order("r_old")["fill_state"])
ok("it is reported as outside_window", out.get("outside_window") == 1, out)
ok("an order INSIDE the window and truly absent IS retired",
   led.get_order("r_new")["fill_state"] == "not_found", led.get_order("r_new")["fill_state"])
ok("the old order stays pending for a later, wider fetch",
   any(r["ref_id"] == "r_old" for r in led.fills_pending()), led.fills_pending())

# --- the window preflight computes must COVER the oldest pending order ---------
# TICK.md tells the orchestrator to pass `fills_created_at_gte from STEP 1`. If
# preflight does not emit it, the orchestrator invents one — the exact mistake that
# retired 139 real orders.
led = _led()
_order(led, "r_a", "PLTR", "A")
_order(led, "r_b", "ANET", "B")
with led._conn() as _c:
    _c.execute("UPDATE orders SET submitted_at=? WHERE ref_id=?",
               ("2026-06-16T10:04:00-04:00", "r_a"))
    _c.execute("UPDATE orders SET submitted_at=? WHERE ref_id=?",
               ("2026-08-10T10:00:00-04:00", "r_b"))
w = tick._fills_window(led)
ok("the window covers the OLDEST pending order", w <= "2026-06-16", w)
ok("the window carries a day of margin", w == "2026-06-15", w)
ok("no pending orders -> today, not a crash", tick._fills_window(_led()) is not None)

from lib.config import load_config as _lc                       # noqa: E402
_cfg = _lc(str(Path(__file__).resolve().parent.parent / "config.yaml"))
_pf = tick._run_preflight(_cfg, led)
ok("preflight EMITS fills_created_at_gte", "fills_created_at_gte" in _pf, sorted(_pf))
ok("preflight emits the pending count", _pf.get("fills_pending") == 2, _pf.get("fills_pending"))
ok("the emitted window covers the oldest order",
   _pf["fills_created_at_gte"] <= "2026-06-16", _pf["fills_created_at_gte"])

print("%d passed, %d failed" % (PASS, len(FAILED)))
sys.exit(1 if FAILED else 0)
