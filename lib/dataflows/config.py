"""Dataflow config (F8: extracted from tradingagents/dataflows/config.py).

Self-contained — no tradingagents import. Holds the data-vendor routing + news
limits the fetchers read. The EVE brain's data tools (quiver_eve/run/quill_data.py)
call the fetchers in lib/dataflows/ directly.
"""
from copy import deepcopy
from typing import Dict, Optional

_DEFAULT_CONFIG = {
    "project_dir": ".",
    "results_dir": "state/results",
    "data_cache_dir": "state/cache",
    # News / data fetching parameters
    "news_article_limit": 20,
    "global_news_article_limit": 10,
    "global_news_lookback_days": 7,
    "global_news_queries": [
        "Federal Reserve interest rates inflation",
        "S&P 500 earnings GDP economic outlook",
        "geopolitical risk trade war sanctions",
        "ECB Bank of England BOJ central bank policy",
        "oil commodities supply chain energy",
    ],
    # Data vendor configuration (yfinance default; alpha_vantage optional fallback)
    "data_vendors": {
        "core_stock_apis": "yfinance",
        "technical_indicators": "yfinance",
        "fundamental_data": "yfinance",
        "news_data": "yfinance",
    },
    "tool_vendors": {},
    "benchmark_ticker": None,
    "benchmark_map": {
        ".NS": "^NSEI", ".BO": "^BSESN", ".T": "^N225", ".HK": "^HSI",
        ".L": "^FTSE", ".TO": "^GSPTSE", ".AX": "^AXJO", "": "SPY",
    },
}

_config: Optional[Dict] = None


def initialize_config():
    global _config
    if _config is None:
        _config = deepcopy(_DEFAULT_CONFIG)


def set_config(config: Dict):
    global _config
    initialize_config()
    incoming = deepcopy(config)
    for key, value in incoming.items():
        if isinstance(value, dict) and isinstance(_config.get(key), dict):
            _config[key].update(value)
        else:
            _config[key] = value


def get_config() -> Dict:
    if _config is None:
        initialize_config()
    return deepcopy(_config)


initialize_config()
