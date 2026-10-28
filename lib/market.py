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
from contextlib import contextmanager
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")

# --- injectable clock (TEST / BACKTEST ONLY) --------------------------------
# Live trading NEVER touches this. With the override unset (the only state a live
# tick is ever in), now_et() is byte-identical to datetime.now(ET) — the sole added
# cost is one `is not None` check. A historical replay pins the clock so that the
# ledger day-key (market.trading_day_et(), read from the OS clock in tick.py, NOT
# from now_iso) and every XNYS session check resolve to the SIMULATED instant.
_now_override: "datetime | None" = None


def set_clock(dt: "datetime | None") -> None:
    """TEST/BACKTEST ONLY. Pin now_et() to a fixed ET instant (None restores the real
    wall clock). Prefer the frozen_clock() context manager so the pin is always
    restored. Naive datetimes are treated as ET; aware ones are converted to ET."""
    global _now_override
    if dt is not None and dt.tzinfo is None:
        dt = dt.replace(tzinfo=ET)
    _now_override = dt.astimezone(ET) if dt is not None else None


@contextmanager
def frozen_clock(dt: "datetime | None"):
    """TEST/BACKTEST ONLY. Pin the clock to `dt` for the duration of the block and
    ALWAYS restore the prior value on exit (even on exception) so a replay can never
    leak a pinned clock into later code."""
    prev = _now_override
    set_clock(dt)
    try:
        yield
    finally:
        set_clock(prev)


def now_et() -> datetime:
    if _now_override is not None:
        return _now_override
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


_CAL = None


def _calendar():
    """The XNYS calendar object, built once per process (the build is slow)."""
    global _CAL
    if _CAL is None:
        import pandas_market_calendars as mcal
        _CAL = mcal.get_calendar("XNYS")
    return _CAL


def sessions_between(start: datetime, end: datetime) -> list:
    """XNYS sessions from the date of ``start`` to the date of ``end``, in order.

    Each item is ``(session_date "YYYY-MM-DD", open_dt, close_dt)`` with ET datetimes.
    Half-days carry their early close. Holidays have no row."""
    s = start.astimezone(ET).strftime("%Y-%m-%d")
    e = end.astimezone(ET).strftime("%Y-%m-%d")
    sched = _calendar().schedule(start_date=s, end_date=e)
    out = []
    for idx, row in sched.iterrows():
        out.append((idx.strftime("%Y-%m-%d"), row["market_open"].tz_convert(ET).to_pydatetime(),
                    row["market_close"].tz_convert(ET).to_pydatetime()))
    return out


def session_close_et(now: datetime | None = None) -> tuple:
    """``(session_date, close_dt)`` of the session that is open at ``now``, else of the
    next session. A proposal expires at this close (plan §10.1 rule 2)."""
    now = (now or now_et()).astimezone(ET)
    for day, _open, close in sessions_between(now, now + timedelta(days=12)):
        if close > now:
            return day, close
    raise RuntimeError("no XNYS session in the next 12 days")


def session_open_et(now: datetime | None = None) -> tuple:
    """``(session_date, open_dt, close_dt)`` of the session that is open at ``now``, else
    of the next session. The tap refusal texts name these times."""
    now = (now or now_et()).astimezone(ET)
    for day, open_t, close in sessions_between(now, now + timedelta(days=12)):
        if close > now:
            return day, open_t, close
    raise RuntimeError("no XNYS session in the next 12 days")


def last_close_et(now: datetime | None = None):
    """The close of the latest session that closed at or before ``now`` (ET datetime)."""
    now = (now or now_et()).astimezone(ET)
    last = None
    for _day, _open, close in sessions_between(now - timedelta(days=12), now):
        if close <= now:
            last = close
    if last is None:
        raise RuntimeError("no XNYS close in the last 12 days")
    return last


def is_open_at(now: datetime) -> bool:
    """True when the regular session is open at ``now`` (uses ``now``, not the clock)."""
    now = now.astimezone(ET)
    for _day, open_t, close in sessions_between(now, now):
        if open_t <= now < close:
            return True
    return False


def trading_days_after(day: str, n: int) -> list:
    """The ``n`` XNYS session dates after ``day`` ("YYYY-MM-DD"), in order."""
    start = datetime.fromisoformat(day).replace(tzinfo=ET) + timedelta(days=1)
    rows = sessions_between(start, start + timedelta(days=n * 2 + 10))
    return [d for d, _o, _c in rows][:n]


def fmt_et(dt: datetime) -> str:
    """'4:00 PM' style clock time in ET, the form every desk message uses."""
    t = dt.astimezone(ET)
    return t.strftime("%I:%M %p").lstrip("0")


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
