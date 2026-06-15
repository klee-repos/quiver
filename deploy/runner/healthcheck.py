#!/usr/bin/env python3
"""Offline build-gate self-test: config + market calendar + ledger schema + imports.

Runs with no network and no broker — a green healthcheck proves the box can load
config, resolve the XNYS calendar, apply the full ledger schema (incl. the strategy
tables), and import every decision module before a real tick is ever scheduled.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))


def run_healthcheck() -> dict:
    checks: dict = {}

    def _try(name, fn):
        try:
            fn()
            checks[name] = "ok"
        except Exception as e:  # noqa: BLE001
            checks[name] = f"FAIL: {e}"

    def _config():
        from lib.config import load_config
        load_config(_REPO / "config.yaml")

    def _market():
        import lib.market as market
        market.trading_day_et()
        # Exercise the XNYS calendar (pandas_market_calendars) — preflight calls this on every
        # tick. trading_day_et() alone does NOT trigger the lazy import, so a missing
        # pandas-market-calendars would pass the check yet break the first real tick.
        market.is_regular_session_open()

    def _ledger():
        from lib.ledger import Ledger
        Ledger(Path(tempfile.mkdtemp()) / "hc.db")  # applies the full schema

    def _imports():
        import importlib
        for m in ("lib.strategy", "lib.portfolio", "lib.goal", "lib.signals",
                  "lib.learn", "lib.universe", "lib.runlock", "lib.strategy_context"):
            importlib.import_module(m)

    _try("config", _config)
    _try("market", _market)
    _try("ledger", _ledger)
    _try("imports", _imports)
    return {"ok": all(v == "ok" for v in checks.values()), "checks": checks}


if __name__ == "__main__":
    import json
    result = run_healthcheck()
    print(json.dumps(result, indent=2))
    raise SystemExit(0 if result["ok"] else 1)
