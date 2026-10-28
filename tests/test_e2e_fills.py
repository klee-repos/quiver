"""E2E: capture the REAL fill from the broker (``tick.py fills``, plan §11.2 and §14.5 ``fills`` row).

The ledger once never recorded what the bot paid: the orchestrator assembled
``orders.result_json`` at place time, and it was wrong as a price (BOT order 6a79ec24 stored
27.59 against an actual ``average_price`` of 27.949900). Python now parses the broker answer,
keyed on ``broker_order_id``.

The input is the §11.2 file shape ``{"get_equity_orders": <raw result>}`` (``data.orders[]``).
Python takes the window (``created_at_gte``) from the order rows, never from the model's file.
``snapshot-prepare`` gives the broker model the same window as ``fills_created_at_gte``.

The load-bearing case is the PARTIAL fill. A ``partially_filled`` order carries a REAL partial
``average_price``, so a retire on price would freeze the share count. Live proof on this account:
BSOL 6a75fa00 has two executions summing 1.697570; a capture after the first stores 1.000000.

No network, no broker, no LLM. Every broker order comes from the ``rh_orders_live`` capture with
values changed (plan §18.1 rule 4).
"""
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

from desk_fakes import Results, fx, temp_root, write_tmp  # noqa: E402
import tick                                               # noqa: E402
from lib import market                                    # noqa: E402
from lib.desk import execution                            # noqa: E402

PY = str(REPO / ".venv" / "bin" / "python")
r = Results("e2e-fills")


def _order(led, ref, ticker, bid, *, submitted_at="2026-08-12T10:00:00-04:00",
           kind="rebalance_buy", side="buy", state="placed"):
    """A pre-desk order row through the real producers: reserved, then finalized with a
    broker id. A state other than ``proposed`` stores ``submitted_at``."""
    led.reserve_order(ref, submitted_at[:10], ticker, side=side, type="market",
                      dollar_amount=26.9, quantity=None, now_iso=submitted_at,
                      order_kind=kind, state=state)
    led.finalize_order(ref, bid, "{}")


def _broker_orders(*changes):
    """The ``rh_orders_live`` capture, one captured order per ``changes`` entry with those
    VALUES set. Entries past ``len(changes)`` are dropped. No key is added."""
    data = fx("rh_orders_live")
    orders = data["get_equity_orders"]["data"]["orders"]
    if len(changes) > len(orders):
        raise ValueError("more orders than captured entries")
    keep = []
    for entry, ch in zip(orders, changes):
        for k, v in ch.items():
            if k not in entry:
                raise KeyError(f"the capture has no key {k!r}")
            entry[k] = v
        keep.append(entry)
    data["get_equity_orders"]["data"]["orders"] = keep
    return data


def _cli(root, *args):
    env = {**os.environ, **root.env}
    p = subprocess.run([PY, "tick.py", *args], cwd=str(REPO), env=env, capture_output=True,
                       text=True, timeout=120)
    lines = [ln for ln in p.stdout.splitlines() if ln.strip().startswith("{")]
    return p, (json.loads(lines[-1]) if lines else None)


# --- the parser, on the captured broker order shape ----------------------------------
_cap = fx("rh_orders_live")["get_equity_orders"]["data"]["orders"][0]
FILLED = dict(copy.deepcopy(_cap), id="6a79ec24", symbol="BOT", state="filled",
              average_price="27.949900", cumulative_quantity="0.962436", quantity="0.962436",
              fees="0.000000")
PARTIAL = dict(copy.deepcopy(_cap), id="6a75fa00", symbol="BSOL", state="partially_filled",
               average_price="10.445000", cumulative_quantity="1.000000", quantity="1.697570",
               fees="0.000000")
ZEROED = dict(copy.deepcopy(_cap), id="Z", symbol="X", state="unconfirmed",
              average_price="0.000000", cumulative_quantity="0.000000")
for d in (FILLED, PARTIAL, ZEROED):
    assert set(d) == set(_cap), "a parser case must keep the captured keys"

f = execution.parse_fill(FILLED)
r.ok("parses the real average_price, not the submission quote", f["avg_price"] == 27.9499, f)
r.ok("parses the share count a dollar-based buy never stored", f["quantity"] == 0.962436, f)
r.ok("keeps fees as a real 0.0", f["fees"] == 0.0, f)
r.ok("carries the broker state", f["fill_state"] == "filled", f)
z = execution.parse_fill(ZEROED)
r.ok("a zero price is NOT a fill (None, never 0.0)", z["avg_price"] is None, z)
r.ok("a zero quantity is NOT a fill", z["quantity"] is None, z)
p = execution.parse_fill(PARTIAL)
r.ok("a partial reports cumulative_quantity, not the ordered quantity", p["quantity"] == 1.0, p)
if hasattr(tick, "_parse_broker_fill"):
    r.ok("tick._parse_broker_fill and execution.parse_fill give the same answer",
         all(tick._parse_broker_fill(o) == execution.parse_fill(o) for o in (FILLED, PARTIAL, ZEROED)))

# --- retire on STATE, never on price ----------------------------------------------------
with temp_root() as root:
    led = root.led()
    _order(led, "r_part", "BSOL", "6a75fa00")
    r.ok("the order starts pending", len(led.fills_pending()) == 1)
    led.record_fill("r_part", avg_price=p["avg_price"], quantity=p["quantity"],
                    fees=p["fees"], fill_state=p["fill_state"], now_iso="t1")
    r.ok("a PARTIAL fill stays pending (a price key would freeze 59% of the truth)",
         len(led.fills_pending()) == 1, led.fills_pending())
    d2 = execution.parse_fill(dict(PARTIAL, state="filled", cumulative_quantity="1.697570"))
    led.record_fill("r_part", avg_price=d2["avg_price"], quantity=d2["quantity"],
                    fees=d2["fees"], fill_state=d2["fill_state"], now_iso="t2")
    r.ok("it retires once the broker says filled", len(led.fills_pending()) == 0)
    r.ok("and the FULL share count lands, not the partial",
         abs(float(led.get_order("r_part")["fill_quantity"]) - 1.697570) < 1e-9,
         led.get_order("r_part")["fill_quantity"])

# --- every terminal state retires; a non-terminal one holds ----------------------------
for state, should_retire in (("filled", True), ("cancelled", True), ("rejected", True),
                             ("failed", True), ("voided", True), ("not_found", True),
                             ("unconfirmed", False), ("partially_filled", False),
                             ("queued", False), ("confirmed", False)):
    with temp_root() as root:
        L = root.led()
        _order(L, "r", "AAA", "B1")
        L.record_fill("r", avg_price=None, quantity=None, fees=None, fill_state=state, now_iso="t")
        retired = len(L.fills_pending()) == 0
        r.ok("state %-17s -> %s" % (state, "retires" if should_retire else "stays pending"),
             retired == should_retire, (state, retired))

# --- the broker state never overwrites the lifecycle state of a pre-desk order ----------
with temp_root() as root:
    led = root.led()
    _order(led, "r_stop", "AAA", "S1", kind="protective_stop", side="sell", state="stop_placed")
    led.record_fill("r_stop", avg_price=None, quantity=None, fees=None,
                    fill_state="cancelled", now_iso="t")
    row = led.get_order("r_stop")
    r.ok("fill_state does not touch orders.state", row["state"] == "stop_placed", row["state"])
    r.ok("fill_state is stored separately", row["fill_state"] == "cancelled", row["fill_state"])
    _order(led, "r_aud", "AAA", "A1")
    led.record_fill("r_aud", avg_price=1.0, quantity=2.0, fees=0.0, fill_state="filled", now_iso="t")
    r.ok("result_json survives the capture", led.get_order("r_aud")["result_json"] == "{}")

# --- the real CLI on the §11.2 file shape ------------------------------------------------
with temp_root() as root:
    led = root.led()
    _order(led, "r_fill", "VRT", "6ada272c-2bd5-4f01-9769-513b80d2445a",
           submitted_at="2026-08-10T10:00:00-04:00")
    _order(led, "r_gone", "ANET", "GONE1", submitted_at="2026-08-11T10:00:00-04:00")
    _order(led, "r_open", "BOT", "6ada272a-b956-4a58-856f-0c8cd6858f76",
           submitted_at="2026-08-11T11:00:00-04:00")
    answer = _broker_orders({"id": "6ada272c-2bd5-4f01-9769-513b80d2445a"},
                            {"id": "6ada272a-b956-4a58-856f-0c8cd6858f76", "state": "partially_filled",
                             "cumulative_quantity": "0.300000"})
    rel = write_tmp(root, "fills.json", answer)
    proc, out = _cli(root, "fills", "--input", rel)
    r.ok("fills exits 0 on the §11.2 shape", proc.returncode == 0, proc.stderr[-400:])
    r.ok("fills reports ok", bool(out and out.get("ok")), out)
    vrt = led.get_order("r_fill")
    r.ok("a present order gets its real average price", vrt["fill_avg_price"] == 194.465, vrt)
    r.ok("a present order gets its real share count", vrt["fill_quantity"] == 0.064124, vrt)
    r.ok("a present order is retired as filled", vrt["fill_state"] == "filled", vrt["fill_state"])
    r.ok("a partially_filled order stays pending",
         [x["ref_id"] for x in led.fills_pending()] == ["r_open"], led.fills_pending())
    r.ok("an order inside the window and absent from the answer is retired as not_found",
         led.get_order("r_gone")["fill_state"] == "not_found", led.get_order("r_gone")["fill_state"])
    r.ok("the not_found order is named in the output", (out or {}).get("not_found") == ["ANET"], out)
    r.ok("the window comes from the oldest pending order minus one day",
         (out or {}).get("window") == "2026-08-09", (out or {}).get("window"))
    # The not_found row is terminal: the next run never asks for it again (no loop).
    proc2, out2 = _cli(root, "fills", "--input", write_tmp(root, "fills2.json", _broker_orders()))
    r.ok("a second run asks only for the open order; absent now, it is retired as not_found",
         (out2 or {}).get("pending_in") == 1 and (out2 or {}).get("not_found") == ["BOT"], out2)

with temp_root() as root:
    led = root.led()
    _order(led, "r_x", "AAA", "X1")
    proc, out = _cli(root, "fills", "--input", write_tmp(root, "old.json", {"orders": []}))
    r.ok("the old {orders: [...]} shape is refused with exit 1", proc.returncode == 1, proc.returncode)
    r.ok("the refusal names the §11.2 shape", "get_equity_orders" in str((out or {}).get("error")), out)
    r.ok("a refused file retires nothing", led.get_order("r_x")["fill_state"] is None)

# --- REGRESSION (live run, 2026-08-13): a narrow window must not retire old orders -------
# The first live run passed a window starting 2026-08-05 against a pending set reaching
# back to 2026-06-16. Every older order was absent from the answer and was marked
# `not_found`, which is TERMINAL: 139 real orders were retired for good.
with temp_root() as root:
    led = root.led()
    _order(led, "r_old", "PLTR", "OLD1", submitted_at="2026-06-16T10:00:00-04:00")
    _order(led, "r_new", "ANET", "NEW1", submitted_at="2026-08-10T10:00:00-04:00")
    # A model that writes its own narrow window into the file: Python ignores it.
    narrow = {**_broker_orders(), "created_at_gte": "2026-08-05"}
    narrow["get_equity_orders"]["data"]["orders"] = []
    proc, out = _cli(root, "fills", "--input", write_tmp(root, "narrow.json", narrow))
    r.ok("a model-written created_at_gte is ignored: the window covers the oldest order",
         (out or {}).get("window") == "2026-06-15", out)
    r.ok("so the old order is asked for (it is inside the window), never skipped as outside",
         (out or {}).get("outside_window") == 0, out)

with temp_root() as root:
    led = root.led()
    _order(led, "r_old", "PLTR", "OLD1", submitted_at="2026-06-16T10:00:00-04:00")
    _order(led, "r_new", "ANET", "NEW1", submitted_at="2026-08-10T10:00:00-04:00")
    # The guard inside apply_fills: an order older than the window it was given stays pending.
    with led.transaction() as conn:
        res = execution.apply_fills(led, conn, [], window="2026-08-05", now="2026-08-13T14:00:00Z")
    r.ok("an order OLDER than the window is not retired", led.get_order("r_old")["fill_state"] is None,
         led.get_order("r_old")["fill_state"])
    r.ok("it is reported as outside_window", res.get("outside_window") == 1, res)
    r.ok("an order INSIDE the window and truly absent IS retired",
         led.get_order("r_new")["fill_state"] == "not_found", led.get_order("r_new")["fill_state"])
    r.ok("the old order stays pending for a later, wider fetch",
         any(x["ref_id"] == "r_old" for x in led.fills_pending()), led.fills_pending())

# --- the window covers the OLDEST pending order -----------------------------------------
with temp_root() as root:
    led = root.led()
    _order(led, "r_a", "PLTR", "A", submitted_at="2026-06-16T10:04:00-04:00")
    _order(led, "r_b", "ANET", "B", submitted_at="2026-08-10T10:00:00-04:00")
    for fn_name, fn in (("tick._fills_window", tick._fills_window), ("execution.fills_window", execution.fills_window)):
        w = fn(led)
        r.ok(f"{fn_name}: the window covers the OLDEST pending order", w <= "2026-06-16", w)
        r.ok(f"{fn_name}: the window carries a day of margin", w == "2026-06-15", w)
    # A late-evening ET stamp is the next UTC day. The broker reads the window as UTC, so it
    # must still reach one day before the order's UTC date.
    _order(led, "r_late", "CEG", "L", submitted_at="2026-06-01T22:30:00-04:00")
    for fn_name, fn in (("tick._fills_window", tick._fills_window), ("execution.fills_window", execution.fills_window)):
        w = fn(led)
        r.ok(f"{fn_name}: a late-ET order (2026-06-02 UTC) is covered with a day of margin",
             w <= "2026-06-01", w)

with temp_root() as root:
    today = market.trading_day_et()
    r.ok("no pending orders -> today, not a crash", execution.fills_window(root.led()) == today)
    r.ok("no pending orders -> tick._fills_window gives today too", tick._fills_window(root.led()) == today)

# --- snapshot-prepare EMITS the window the broker model passes to get_equity_orders ------
# The broker runbooks pass `fills_created_at_gte` from the prompt. If snapshot-prepare does
# not emit it, the model invents one: the exact mistake that retired 139 real orders.
with temp_root() as root:
    led = root.led()
    _order(led, "r_a", "PLTR", "A", submitted_at="2026-06-16T10:04:00-04:00")
    _order(led, "r_b", "ANET", "B", submitted_at="2026-08-10T10:00:00-04:00")
    proc, sp = _cli(root, "snapshot-prepare")
    r.ok("snapshot-prepare exits 0", proc.returncode == 0, proc.stderr[-400:])
    r.ok("snapshot-prepare EMITS fills_created_at_gte", "fills_created_at_gte" in (sp or {}), sorted(sp or {}))
    r.ok("the emitted window covers the oldest order",
         str((sp or {}).get("fills_created_at_gte")) <= "2026-06-16", sp)
    r.ok("the emitted window is the same one `fills` uses",
         (sp or {}).get("fills_created_at_gte") == execution.fills_window(led), sp)
    ip = execution.snapshot_prepare(led, root.cfg())
    r.ok("in process: snapshot_prepare gives the same window",
         ip.get("fills_created_at_gte") == (sp or {}).get("fills_created_at_gte"), ip)

for doc in ("SNAPSHOT.md", "EXECUTE.md"):
    text = (REPO / doc).read_text(encoding="utf-8")
    r.ok(f"{doc} passes fills_created_at_gte as created_at_gte of get_equity_orders",
         "created_at_gte" in text and "fills_created_at_gte" in text)

raise SystemExit(r.done())
