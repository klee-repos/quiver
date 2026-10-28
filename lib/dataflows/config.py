"""Dataflow config: the cache folder and the news limit that the fetchers read.

Self-contained: it imports nothing from the desk. ``stockstats_utils.py`` reads
``data_cache_dir``. ``yfinance_news.py`` reads ``news_article_limit``.
"""
from copy import deepcopy
from typing import Dict, Optional

_DEFAULT_CONFIG = {
    "data_cache_dir": "state/cache",
    # The number of headlines one news fetch asks for, per ticker.
    "news_article_limit": 20,
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
