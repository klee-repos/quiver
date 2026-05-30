"""Load and validate config.yaml into a typed Config object.

Fails SAFE: a missing/garbled ``dry_run`` is treated as ``True`` (paper mode),
never as live trading. Invalid required fields raise immediately so the
orchestrator stops instead of trading on a bad config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import List

import yaml

_PLACEHOLDER_ACCOUNT = "XXXXXXXX"


@dataclass(frozen=True)
class RiskConfig:
    max_dollars_per_trade: float
    daily_loss_halt_pct: float
    daily_capital_deploy_cap: float
    max_open_position_per_ticker: float
    min_buying_power_buffer: float
    # Intraday-only caps (default 1 == classic once-a-day behavior). max_actions
    # bounds repeat TRADES per ticker/day; max_analyses is the LLM-cost circuit
    # breaker bounding repeat ANALYSES per ticker/day.
    max_actions_per_ticker_per_day: int
    max_analyses_per_ticker_per_day: int


@dataclass(frozen=True)
class NotifyConfig:
    """Email-digest delivery settings. Observability only — never trading.

    Fails SAFE: stays disabled unless ``notify.enabled`` is exactly ``true``.
    No secrets here — the RESEND_API_KEY and sender live in the operator's Resend
    MCP registration (``claude mcp add``), same as the Robinhood MCP, NOT in this
    repo. ``from_addr`` is optional; blank means the MCP's own sender is used. A
    bad/missing key degrades to a logged send failure, never a config crash.
    """
    enabled: bool
    to: List[str]
    from_addr: str
    subject_prefix: str


@dataclass(frozen=True)
class StorageConfig:
    """Retention + optional offload for bulky run artifacts (observability only).

    ``retention_days <= 0`` disables pruning (keep everything). The S3 archive
    backend is deferred; ``archive_enabled`` defaults False and, even if set,
    degrades safely to local-only pruning until a backend exists.
    """
    retention_days: int
    archive_enabled: bool
    archive_backend: str
    archive_bucket: str
    archive_prefix: str


@dataclass(frozen=True)
class Config:
    account_number: str
    dry_run: bool
    kill_switch_file: str
    watchlist: List[str]
    risk: RiskConfig
    chat_model: str
    reasoner_model: str
    buy_type: str
    sell_mode: str
    time_in_force: str
    market_hours: str
    # Phase 5: limit entries + protective GTC stops (Python-owned prices).
    limit_slippage_pct: float
    protective_stop_enabled: bool
    protective_stop_pct: float
    protective_stop_tif: str
    act_after_open_minutes: int
    analyze_timeout_sec: int
    # Multi-run / cadence (intraday mode only; the master switch gates the path).
    intraday_enabled: bool
    per_ticker_cooldown_min: int
    review_floor_min: int
    review_ceiling_open_min: int
    review_ceiling_min: int
    notify: NotifyConfig
    storage: StorageConfig
    raw: dict


class ConfigError(ValueError):
    """Raised when config.yaml is missing required fields or has bad values."""


def _require(d: dict, key: str):
    if key not in d or d[key] in (None, ""):
        raise ConfigError(f"config.yaml: missing required key '{key}'")
    return d[key]


def _pos_num(d: dict, key: str) -> float:
    v = _require(d, key)
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise ConfigError(f"config.yaml: '{key}' must be a number, got {v!r}")
    if v <= 0:
        raise ConfigError(f"config.yaml: '{key}' must be > 0, got {v}")
    return v


def _pos_int(d: dict, key: str, default: int) -> int:
    """Positive integer with a default (used for loop/cadence + intraday caps)."""
    v = d.get(key, default)
    try:
        v = int(v)
    except (TypeError, ValueError):
        raise ConfigError(f"config.yaml: '{key}' must be an integer, got {v!r}")
    if v <= 0:
        raise ConfigError(f"config.yaml: '{key}' must be > 0, got {v}")
    return v


def load_config(path) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config.yaml not found at {p}")
    d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    # Per-user/secret values are NOT committed: they come from .env (gitignored),
    # alongside DEEPSEEK_API_KEY. Load it here so every entrypoint (tick.py,
    # analyze.py) sees the same values via load_config. Missing python-dotenv or
    # a missing .env is fine — the vars may already be set in the real environment.
    try:
        from dotenv import load_dotenv

        load_dotenv(p.resolve().parent / ".env")
    except Exception:
        pass

    # FAIL SAFE: anything other than an explicit False keeps us in paper mode.
    dry_run = d.get("dry_run", True) is not False

    # account_number lives in .env (RH_ACCOUNT_NUMBER); config.yaml may still carry
    # it as a fallback for local use, but the committed file ships without it.
    account = (os.environ.get("RH_ACCOUNT_NUMBER") or str(d.get("account_number", "") or "")).strip()
    if not account or account == _PLACEHOLDER_ACCOUNT:
        raise ConfigError(
            "account_number is not set. Put RH_ACCOUNT_NUMBER=<your agentic_allowed "
            "Robinhood account number> in .env (it must NOT be committed)."
        )

    watchlist = _require(d, "watchlist")
    if not isinstance(watchlist, list) or not watchlist:
        raise ConfigError("config.yaml: 'watchlist' must be a non-empty list")
    watchlist = [str(t).strip().upper() for t in watchlist]

    risk_d = d.get("risk", {}) or {}
    risk = RiskConfig(
        max_dollars_per_trade=_pos_num(risk_d, "max_dollars_per_trade"),
        daily_loss_halt_pct=_pos_num(risk_d, "daily_loss_halt_pct"),
        daily_capital_deploy_cap=_pos_num(risk_d, "daily_capital_deploy_cap"),
        max_open_position_per_ticker=_pos_num(risk_d, "max_open_position_per_ticker"),
        min_buying_power_buffer=float(risk_d.get("min_buying_power_buffer", 0) or 0),
        max_actions_per_ticker_per_day=_pos_int(risk_d, "max_actions_per_ticker_per_day", 1),
        max_analyses_per_ticker_per_day=_pos_int(risk_d, "max_analyses_per_ticker_per_day", 1),
    )

    ds = d.get("deepseek", {}) or {}
    chat_model = str(_require(ds, "chat_model")).strip()
    reasoner_model = str(_require(ds, "reasoner_model")).strip()
    if "verify" in chat_model.lower() or "verify" in reasoner_model.lower():
        raise ConfigError(
            "config.yaml: deepseek model IDs are still placeholders — verify the "
            "current IDs via the DeepSeek /v1/models endpoint and set them."
        )

    order = d.get("order", {}) or {}
    loop = d.get("loop", {}) or {}

    # Order types (Phase 5). buy_type 'limit' uses a marketable limit at the live
    # quote + slippage and WHOLE shares (limit orders can't be fractional). The
    # protective stop price is always Python-clamped; the model only seeds it.
    buy_type = str(order.get("buy_type", "market")).strip().lower()
    if buy_type not in ("market", "limit"):
        raise ConfigError("config.yaml: order.buy_type must be 'market' or 'limit'")
    limit_slippage_pct = float(order.get("limit_slippage_pct", 0.3) or 0.0)
    if limit_slippage_pct < 0:
        raise ConfigError("config.yaml: order.limit_slippage_pct must be >= 0")
    pstop = order.get("protective_stop", {}) or {}
    protective_stop_enabled = pstop.get("enabled", False) is True
    protective_stop_pct = float(pstop.get("stop_pct", 8.0) or 0.0)
    if protective_stop_enabled and not (0 < protective_stop_pct < 100):
        raise ConfigError(
            "config.yaml: order.protective_stop.stop_pct must be in (0, 100) when enabled"
        )
    protective_stop_tif = str(pstop.get("time_in_force", "gtc")).strip().lower()

    # Multi-run / cadence. Master switch fails SAFE OFF (only an explicit True
    # enables intraday). Cadence bounds must be ordered floor <= open-ceiling <=
    # ceiling so the Python clamp can never invert.
    intraday_enabled = loop.get("intraday_enabled", False) is True
    per_ticker_cooldown_min = _pos_int(loop, "per_ticker_cooldown_min", 60)
    review_floor_min = _pos_int(loop, "review_floor_min", 30)
    review_ceiling_open_min = _pos_int(loop, "review_ceiling_open_min", 120)
    review_ceiling_min = _pos_int(loop, "review_ceiling_min", 1440)
    if not (review_floor_min <= review_ceiling_open_min <= review_ceiling_min):
        raise ConfigError(
            "config.yaml: loop cadence bounds must satisfy review_floor_min <= "
            "review_ceiling_open_min <= review_ceiling_min "
            f"(got {review_floor_min}, {review_ceiling_open_min}, {review_ceiling_min})"
        )

    # Notifications: fail SAFE — absent block or anything but an explicit True
    # keeps email OFF. Only validate addresses when the user opts in, so a
    # disabled block never blocks a trading tick.
    notify_d = d.get("notify", {}) or {}
    notify_enabled = notify_d.get("enabled", False) is True
    # Recipients live in .env (NOTIFY_TO, comma-separated) so the committed config
    # carries no personal address; fall back to config.yaml's notify.to if unset.
    to_env = os.environ.get("NOTIFY_TO", "").strip()
    if to_env:
        to_raw = to_env.split(",")
    else:
        to_raw = notify_d.get("to", []) or []
        if isinstance(to_raw, str):
            to_raw = [to_raw]
    to = [str(x).strip() for x in to_raw if str(x).strip()]
    from_addr = str(notify_d.get("from", "") or "").strip()
    subject_prefix = str(notify_d.get("subject_prefix", "[Quiver]") or "[Quiver]").strip()
    if notify_enabled:
        if not to or any("@" not in addr for addr in to):
            raise ConfigError(
                "config.yaml: notify.to must be a non-empty list of email "
                "addresses when notify.enabled is true"
            )
        # `from` is optional: blank means the Resend MCP's own SENDER_EMAIL_ADDRESS
        # is used (the sender lives in the MCP registration, not this repo). Only
        # validate it when the user explicitly overrides it here.
        if from_addr and "@" not in from_addr:
            raise ConfigError(
                "config.yaml: notify.from, if set, must be a sender email address "
                "(or leave it blank to use the Resend MCP's configured sender)"
            )
    notify = NotifyConfig(
        enabled=notify_enabled, to=to, from_addr=from_addr, subject_prefix=subject_prefix,
    )

    # Storage retention + optional archival. Observability housekeeping only;
    # fails SAFE — bad values raise here rather than letting a runaway log tree
    # or a misconfigured offload surprise us later. retention_days<=0 disables.
    storage_d = d.get("storage", {}) or {}
    try:
        retention_days = int(storage_d.get("retention_days", 30))
    except (TypeError, ValueError):
        raise ConfigError("config.yaml: storage.retention_days must be an integer")
    if retention_days < 0:
        raise ConfigError("config.yaml: storage.retention_days must be >= 0 (0 disables pruning)")
    archive_d = storage_d.get("archive", {}) or {}
    archive_enabled = archive_d.get("enabled", False) is True
    archive_backend = str(archive_d.get("backend", "s3") or "s3").strip()
    archive_bucket = str(archive_d.get("bucket", "") or "").strip()
    archive_prefix = str(archive_d.get("prefix", "") or "").strip()
    if archive_enabled and archive_backend == "s3" and not archive_bucket:
        raise ConfigError(
            "config.yaml: storage.archive.bucket is required when archive.enabled is true"
        )
    storage = StorageConfig(
        retention_days=retention_days,
        archive_enabled=archive_enabled,
        archive_backend=archive_backend,
        archive_bucket=archive_bucket,
        archive_prefix=archive_prefix,
    )

    return Config(
        account_number=account,
        dry_run=dry_run,
        kill_switch_file=str(_require(d, "kill_switch_file")),
        watchlist=watchlist,
        risk=risk,
        chat_model=chat_model,
        reasoner_model=reasoner_model,
        buy_type=buy_type,
        sell_mode=str(order.get("sell_mode", "close_position")),
        time_in_force=str(order.get("time_in_force", "gfd")),
        market_hours=str(order.get("market_hours", "regular_hours")),
        limit_slippage_pct=limit_slippage_pct,
        protective_stop_enabled=protective_stop_enabled,
        protective_stop_pct=protective_stop_pct,
        protective_stop_tif=protective_stop_tif,
        act_after_open_minutes=int(loop.get("act_after_open_minutes", 5)),
        analyze_timeout_sec=int(loop.get("analyze_timeout_sec", 900)),
        intraday_enabled=intraday_enabled,
        per_ticker_cooldown_min=per_ticker_cooldown_min,
        review_floor_min=review_floor_min,
        review_ceiling_open_min=review_ceiling_open_min,
        review_ceiling_min=review_ceiling_min,
        notify=notify,
        storage=storage,
        raw=d,
    )
