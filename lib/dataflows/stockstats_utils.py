import re
import time
import logging

import pandas as pd
import yfinance as yf
from yfinance.exceptions import YFRateLimitError
from stockstats import wrap
from typing import Annotated
import os
from .config import get_config
from .utils import safe_ticker_component

logger = logging.getLogger(__name__)


def yf_retry(func, max_retries=3, base_delay=2.0):
    """Execute a yfinance call with exponential backoff on rate limits.

    yfinance raises YFRateLimitError on HTTP 429 responses but does not
    retry them internally. This wrapper adds retry logic specifically
    for rate limits. Other exceptions propagate immediately.
    """
    for attempt in range(max_retries + 1):
        try:
            return func()
        except YFRateLimitError:
            if attempt < max_retries:
                delay = base_delay * (2 ** attempt)
                logger.warning(f"Yahoo Finance rate limited, retrying in {delay:.0f}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(delay)
            else:
                raise


def _clean_dataframe(data: pd.DataFrame) -> pd.DataFrame:
    """Normalize a stock DataFrame for stockstats: parse dates, drop invalid rows, fill price gaps."""
    data["Date"] = pd.to_datetime(data["Date"], errors="coerce")
    data = data.dropna(subset=["Date"])

    price_cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in data.columns]
    data[price_cols] = data[price_cols].apply(pd.to_numeric, errors="coerce")
    data = data.dropna(subset=["Close"])
    data[price_cols] = data[price_cols].ffill().bfill()

    return data


# Bars each indicator actually NEEDS before its value means what its name says.
# stockstats rolls with min_periods=1, so a 147-bar frame happily returns a 147-bar mean
# labelled ``close_200_sma`` — a short-window number wearing a long-window label. The live
# consumer set is CLOSED (quiver_eve/run/quill_data.py enumerates exactly these 11), so this
# is a table rather than a name-parsing heuristic: a heuristic keyed on digits in the name
# would mask only the three ``close_*`` entries and leave RSI/MACD/BOLL/ATR fabricated.
# Windows are stockstats' own defaults, read from the library (``stockstats.dft_windows``):
# rsi 14, atr 14, boll 20, macd (12, 26, 9). ``macds``/``macdh``/``boll_ub``/``boll_lb`` are
# derived columns and report no default of their own, so they are named explicitly here.
_MIN_PERIODS = {
    "close_200_sma": 200, "close_50_sma": 50, "close_10_ema": 10,
    "rsi": 14, "atr": 14,
    "boll": 20, "boll_ub": 20, "boll_lb": 20,
    "macd": 26,                    # longest constituent EMA span
    "macds": 34, "macdh": 34,      # a 9-period EMA *of* the 26-span macd line: 26 + 9 - 1
}

_WINDOW_IN_NAME = re.compile(r"_(\d+)_")


def min_periods_for(indicator: str) -> "int | None":
    """Bars ``indicator`` needs before its value is meaningful, or ``None`` when unknown.

    Table first (the closed live set), then a numeric-suffix fallback for stockstats names
    that carry their window explicitly (``rsi_14``, ``close_20_sma``). ``None`` means "no
    idea" and is treated as "do not mask" — we never suppress a value on a guess.
    """
    name = str(indicator or "").strip().lower()
    if name in _MIN_PERIODS:
        return _MIN_PERIODS[name]
    m = _WINDOW_IN_NAME.search(f"_{name}_")
    return int(m.group(1)) if m else None


def indicator_at(df, indicator: str, row: int = -1):
    """Value of ``indicator`` at ``row``, or NaN when the frame is SHORTER than the window
    that indicator needs.

    Without this, a short frame yields a confident number that silently answers a different
    question than the one asked — e.g. on real AAPL at 2022-03-01 the 147-bar window reported
    ``close_200_sma = 154.72``, and on 2021-09-15 a 32-bar window inverted the price-vs-200d
    read outright (reported 145.85 -> "below"; the true 200-day level was 130.21 -> above).
    """
    need = min_periods_for(indicator)
    if need is not None and len(df) < need:
        return float("nan")
    return df[indicator].iloc[row]


def load_ohlcv(symbol: str, curr_date: str) -> pd.DataFrame:
    """Fetch 5 years of OHLCV ending at ``curr_date``, cached, look-ahead filtered.

    The window is anchored to ``curr_date``, NOT to today. Anchoring it to today meant a
    dated run kept only the slice of a today-anchored window that happened to fall before
    ``curr_date`` — e.g. AAPL at 2022-03-01 yielded 147 bars — and since stockstats rolls
    with ``min_periods=1``, that short frame came back as a confident ``close_200_sma``.
    Because the cache filename is built from this window, it now varies with ``curr_date``
    too; previously one today-keyed file served every ``curr_date``, so the same
    (symbol, curr_date) gave different answers depending on the day you asked.
    """
    # Reject ticker values that would escape the cache directory when
    # interpolated into the cache filename (e.g. ``../../tmp/x``).
    safe_symbol = safe_ticker_component(symbol)

    if not curr_date:
        # Previously fell through as NaT and silently returned an EMPTY frame (every row
        # fails ``<= NaT``). Fail loudly instead of handing back a look-ahead-safe-looking void.
        raise ValueError("load_ohlcv requires a curr_date (YYYY-MM-DD)")

    config = get_config()
    curr_date_dt = pd.to_datetime(curr_date)
    if pd.isna(curr_date_dt):
        raise ValueError(f"load_ohlcv got an unparseable curr_date: {curr_date!r}")

    # Window + cache key both anchor to curr_date. yfinance's ``end`` is EXCLUSIVE, so
    # curr_date's own bar needs ``+1 day`` — without it the exact-date lookup in
    # StockstatsUtils.get_stock_stats could never match. The ``min(..., today)`` clamp is
    # what keeps the LIVE case byte-identical to the previous today-anchored behavior:
    # when curr_date is today, ``end`` collapses back to today at any clock time.
    curr_day = curr_date_dt.normalize()
    today_date = pd.Timestamp.today().normalize()
    start_str = (curr_day - pd.DateOffset(years=5)).strftime("%Y-%m-%d")
    end_str = min(curr_day + pd.Timedelta(days=1), today_date).strftime("%Y-%m-%d")

    os.makedirs(config["data_cache_dir"], exist_ok=True)
    data_file = os.path.join(
        config["data_cache_dir"],
        f"{safe_symbol}-YFin-data-{start_str}-{end_str}.csv",
    )

    if os.path.exists(data_file):
        data = pd.read_csv(data_file, on_bad_lines="skip", encoding="utf-8")
    else:
        data = yf_retry(lambda: yf.download(
            symbol,
            start=start_str,
            end=end_str,
            multi_level_index=False,
            progress=False,
            auto_adjust=True,
        ))
        data = data.reset_index()
        # VALIDATE BEFORE PUBLISHING. yfinance turns ANY per-ticker failure — timeout, reset,
        # DNS — into an EMPTY frame rather than an exception, and yf_retry above only covers
        # YFRateLimitError, so a network blip arrives here looking like a successful fetch of
        # nothing. Caching that used to be self-limiting because the key moved with the wall
        # clock and healed the next day; now the key is pinned to curr_date, so for a past
        # date it would never roll and one blip would poison that (symbol, curr_date) forever.
        if data.empty or "Date" not in data.columns:
            from .errors import DataUnavailableError
            raise DataUnavailableError(
                f"Empty OHLCV download for '{symbol}' between {start_str} and {end_str}")
        # Publish atomically so an interrupted write can't leave a truncated CSV behind that
        # every later run would happily read.
        tmp_file = f"{data_file}.{os.getpid()}.tmp"
        data.to_csv(tmp_file, index=False, encoding="utf-8")
        os.replace(tmp_file, data_file)

    data = _clean_dataframe(data)

    # Filter to curr_date to prevent look-ahead bias in backtesting
    data = data[data["Date"] <= curr_date_dt]

    return data


def filter_financials_by_date(data: pd.DataFrame, curr_date: str) -> pd.DataFrame:
    """Drop financial statement columns (fiscal period timestamps) after curr_date.

    yfinance financial statements use fiscal period end dates as columns.
    Columns after curr_date represent future data and are removed to
    prevent look-ahead bias.
    """
    if not curr_date or data.empty:
        return data
    cutoff = pd.Timestamp(curr_date)
    mask = pd.to_datetime(data.columns, errors="coerce") <= cutoff
    return data.loc[:, mask]

