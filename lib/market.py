"""US equity market-hours logic.

All time math is done in America/New_York via zoneinfo (never trusting the
OS timezone), and session validity comes from the NYSE (XNYS) calendar in
pandas_market_calendars, which correctly handles holidays and half-day early
closes. A bare weekday check would happily trade on Thanksgiving.

CLI: ``python -m lib.market`` prints a one-line JSON status the orchestrator
can parse.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


def now_et() -> datetime:
    return datetime.now(ET)


def trading_day_et() -> str:
    """Today's date in ET as YYYY-MM-DD (the bot's notion of 'today')."""
    return now_et().strftime("%Y-%m-%d")


def _today_schedule():
    """Return today's XNYS schedule row, or None if not a trading day."""
    import pandas_market_calendars as mcal

    cal = mcal.get_calendar("XNYS")
    day = trading_day_et()
    sched = cal.schedule(start_date=day, end_date=day)
    if sched.empty:
        return None
    return sched.iloc[0]


def _open_close():
    row = _today_schedule()
    if row is None:
        return None, None
    open_t = row["market_open"].tz_convert(ET)
    close_t = row["market_close"].tz_convert(ET)
    return open_t, close_t


def is_regular_session_open(now: datetime | None = None) -> bool:
    """True only during a real regular-hours session (holiday/half-day aware)."""
    now = now or now_et()
    open_t, close_t = _open_close()
    if open_t is None or close_t is None:
        return False
    return open_t <= now < close_t


def minutes_since_open(now: datetime | None = None):
    """Minutes since today's open, or None if closed / before open / non-session."""
    now = now or now_et()
    open_t, close_t = _open_close()
    if open_t is None or close_t is None or now < open_t or now >= close_t:
        return None
    return (now - open_t).total_seconds() / 60.0


def next_market_time_et(after: datetime, lookahead_days: int = 10):
    """Earliest instant >= ``after`` at which the regular session is open.

    If ``after`` already falls inside a session, returns ``after`` unchanged;
    otherwise returns the next session's open (holiday/half-day aware via XNYS).
    Used to snap a model-proposed re-check time forward out of closed periods so
    the bot never schedules a wake into a market gap. Returns ``after`` as a safe
    fallback if no session is found within ``lookahead_days`` (shouldn't happen).
    """
    import pandas_market_calendars as mcal

    after = after.astimezone(ET)
    cal = mcal.get_calendar("XNYS")
    start = after.strftime("%Y-%m-%d")
    end = (after + timedelta(days=lookahead_days)).strftime("%Y-%m-%d")
    sched = cal.schedule(start_date=start, end_date=end)
    for _, row in sched.iterrows():
        open_t = row["market_open"].tz_convert(ET)
        close_t = row["market_close"].tz_convert(ET)
        if after < open_t:
            return open_t
        if open_t <= after < close_t:
            return after
    return after


def status() -> dict:
    now = now_et()
    return {
        "open": is_regular_session_open(now),
        "minutes_since_open": minutes_since_open(now),
        "now_et": now.isoformat(),
        "trading_day": trading_day_et(),
    }


if __name__ == "__main__":
    try:
        print(json.dumps(status()))
    except Exception as e:  # never crash the orchestrator's open-check
        print(json.dumps({"open": False, "error": str(e)}))
        sys.exit(1)
