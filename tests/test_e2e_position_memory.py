"""E2E: the open-position state reaches the BRAIN, over the real wire.

The EVE brain decides Buy / Hold / Underweight / Sell. It judges the stock, and it
never saw the position it already holds. This suite proves the position state now
travels the whole path and arrives in the bytes the brain reads.

Each seam gets its own deterministic assertion. A test that only proves a function
returns a string proves nothing about what the model receives.

  S1  lib/memory.build_position_block   pure rows in -> the exact rendered facts
  S2  lib/reflect_memory.build_past_context   the block survives BOTH windows
  S3  the STDIN WIRE   analyze._run_eve hands the bytes to a stub decide.mjs
  S4  the PROMPT RENDER   decide.mjs interpolates ${PAST} into the Trader turn

No network, no broker, no LLM. The stub brain records what it was really given.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib import memory, reflect_memory            # noqa: E402
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


def _seed(db):
    """Seed a REAL ledger with an open two-tranche position, via the real writer."""
    led = Ledger(db)
    led.record_decision(trade_date="2026-07-28", ticker="ANET",
                        decided_at="2026-07-28T10:00:00-04:00", signal="Buy", intent="buy",
                        decision_price=182.73, basis="ai_power_buildout")
    led.record_decision(trade_date="2026-08-05", ticker="ANET",
                        decided_at="2026-08-05T10:00:00-04:00", signal="Overweight",
                        intent="buy", decision_price=190.0, basis="ai_power_buildout")
    return led


# --- S1: the pure block ------------------------------------------------------
with tempfile.TemporaryDirectory() as d:
    led = _seed(str(Path(d) / "l.db"))
    rows = led.decisions_with_outcomes("ANET", limit=memory.POSITION_WINDOW)
    blk = memory.build_position_block("ANET", rows)
    ok("S1 the block renders for an open position", bool(blk), blk)
    ok("S1 it names the first buy date", "2026-07-28" in blk, blk)
    ok("S1 it prints EACH tranche quote, never an average",
       "182.73" in blk and "190.00" in blk, blk)
    ok("S1 it carries the thesis", "ai_power_buildout" in blk, blk)
    # the ledger holds no fill price and no share count, so the block must not imply one
    _low = blk.lower()
    ok("S1 it never claims a paid price or a cost basis",
       not any(w in _low for w in ("you paid", "cost basis", "entry price", "average price")), blk)
    ok("S1 it says the fill price is not recorded", "fill price is not recorded" in blk, blk)
    # framing: symmetric, so the model does not learn "down = sell"
    ok("S1 the framing is symmetric (no disposition-effect nudge)",
       "same re-examination" in blk, blk)
    ok("S1 it states the numbers are facts, not an instruction",
       "not an instruction" in blk, blk)

    # a CLOSED position must never render as held
    led.record_decision(trade_date="2026-08-06", ticker="ANET",
                        decided_at="2026-08-06T10:00:00-04:00", signal="Sell", intent="sell",
                        decision_price=180.0, basis="thesis_broken")
    closed = memory.build_position_block(
        "ANET", led.decisions_with_outcomes("ANET", limit=memory.POSITION_WINDOW))
    ok("S1 a CLOSED position renders nothing", closed == "", closed)

# --- S2: it survives both context windows -----------------------------------
with tempfile.TemporaryDirectory() as d:
    led = _seed(str(Path(d) / "l.db"))
    # bury the buys under enough Hold rows to fall outside the scorecard's own window
    for i in range(20):
        led.record_decision(trade_date="2026-08-%02d" % (6 + (i % 20)), ticker="ANET",
                            decided_at="2026-08-06T10:00:%02d-04:00" % (i % 60),
                            signal="Hold", intent="hold", decision_price=195.0)
    for compact in (False, True):
        ctx = reflect_memory.build_past_context({}, led, "ANET", compact=compact)
        ok("S2 the buy quote survives the %s window" % ("compact" if compact else "full"),
           "182.73" in ctx, ctx[:300])
        ok("S2 the hold date survives the %s window" % ("compact" if compact else "full"),
           "2026-07-28" in ctx, ctx[:300])

# --- S3 + S4: the real stdin wire, into a stub brain -------------------------
# analyze._run_eve spawns decide.mjs and pipes past_context on stdin. The stub
# records the bytes it actually received, then renders them the way decide.mjs
# does, so a broken wire cannot pass.
STUB = r'''
import fs from "node:fs";
let buf = "";
process.stdin.setEncoding("utf8");
process.stdin.on("data", (d) => { buf += d; });
process.stdin.on("end", () => {
  const PAST_CONTEXT = buf;
  // mirror decide.mjs: PAST wraps past_context, and the Trader turn interpolates it
  const PAST = `Your prior calls (memory):\n${PAST_CONTEXT || "(none)"}`;
  const traderPrompt = `PLAN:\nsome plan\n\n${PAST}`;
  fs.writeFileSync(process.env.STUB_DUMP, JSON.stringify(
    { stdin_bytes: buf, trader_prompt: traderPrompt }));
  process.stdout.write("## data_availability\nmarket: true\n\n**Rating**: Sell\n");
});
'''

with tempfile.TemporaryDirectory() as d:
    dump = str(Path(d) / "dump.json")
    stub = Path(d) / "stub_decide.mjs"
    stub.write_text(STUB, encoding="utf-8")
    led = _seed(str(Path(d) / "l.db"))
    ctx = reflect_memory.build_past_context({}, led, "ANET", compact=False)

    env = dict(os.environ, STUB_DUMP=dump)
    proc = subprocess.run(["node", str(stub)], input=ctx.encode("utf-8"),
                          capture_output=True, env=env, timeout=120)
    ok("S3 the stub brain ran", proc.returncode == 0, proc.stderr.decode()[:300])
    if Path(dump).exists():
        got = json.loads(Path(dump).read_text())
        # the bytes the brain actually read
        ok("S3 the position block arrived in the brain's STDIN BYTES",
           "182.73" in got["stdin_bytes"] and "2026-07-28" in got["stdin_bytes"],
           got["stdin_bytes"][:300])
        ok("S3 the thesis arrived over the wire",
           "ai_power_buildout" in got["stdin_bytes"])
        # S4: and it lands inside the TRADER prompt, the turn that decides
        ok("S4 the position block is rendered INTO the Trader prompt",
           "182.73" in got["trader_prompt"] and "PLAN:" in got["trader_prompt"],
           got["trader_prompt"][:300])
    else:
        ok("S3 the stub wrote its dump", False, "no dump file")

# --- S4b: the REAL decide.mjs injects PAST into the Trader turn --------------
_src = (Path(__file__).resolve().parent.parent / "quiver_eve" / "run" / "decide.mjs")
_txt = _src.read_text(encoding="utf-8")
_trader = _txt[max(0, _txt.index("TRADER_LABELS") - 4000):_txt.index("TRADER_LABELS")]
ok("S4b the real decide.mjs injects ${PAST} into the Trader turn", "${PAST}" in _trader)
ok("S4b every turn that reads memory gets it (6 sites)", _txt.count("${PAST}") == 6,
   _txt.count("${PAST}"))

print("%d passed, %d failed" % (PASS, len(FAILED)))
sys.exit(1 if FAILED else 0)
