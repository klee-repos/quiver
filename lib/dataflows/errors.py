"""Typed dataflow errors so the vendor router can fall through on a real failure.

A core fetcher that returns an *error string* instead of raising is the fail-OPEN
bug: ``route_to_vendor`` can't tell success from failure, so the fallback vendor
never fires and "No data found"/"Error retrieving…" text feeds a confident,
tradable Buy/Sell on real money. Raising ``DataUnavailableError`` instead lets the
router advance to the next vendor, and lets ``analyze.py`` fail SAFE (emit
``signal:ERROR`` -> the allocator holds the prior weight) rather than trade on nothing.
"""

from __future__ import annotations


class DataUnavailableError(RuntimeError):
    """A core data fetch returned nothing usable (empty/missing). Recoverable: the
    router tries the next vendor; if all fail, the analysis fails SAFE to ERROR."""
