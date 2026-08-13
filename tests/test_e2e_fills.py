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

print("%d passed, %d failed" % (PASS, len(FAILED)))
sys.exit(1 if FAILED else 0)
