"""`bench/` — the quiver benchmark harness.

DELIBERATELY AT REPO ROOT, NOT UNDER `lib/`. `tests/test_units.py` enumerates `lib/**/*.py`
into the decision-wall roster, so a module placed under `lib/` may not import
`lib.risk`/`lib.signals`/`lib.config`/`lib.levers` nor name a broker/limit symbol. The benchmark
must read exactly those things, so it lives outside the wall's enumeration instead of being
granted an exemption inside it.

Nothing here is on the trading path. No module in this package is imported by `analyze.py`,
`tick.py`, or `deploy/runner/run_tick.py`.
"""
