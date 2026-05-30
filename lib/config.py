"""Load and validate config.yaml into a typed Config object.

Fails SAFE: a missing/garbled ``dry_run`` is treated as ``True`` (paper mode),
never as live trading. Invalid required fields raise immediately so the
orchestrator stops instead of trading on a bad config.
"""

from __future__ import annotations

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
    act_after_open_minutes: int
    analyze_timeout_sec: int
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


def load_config(path) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config.yaml not found at {p}")
    d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}

    # FAIL SAFE: anything other than an explicit False keeps us in paper mode.
    dry_run = d.get("dry_run", True) is not False

    account = str(_require(d, "account_number")).strip()
    if account == _PLACEHOLDER_ACCOUNT:
        raise ConfigError(
            "config.yaml: account_number is still the placeholder "
            f"'{_PLACEHOLDER_ACCOUNT}'. Set your agentic_allowed account number."
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

    # Notifications: fail SAFE — absent block or anything but an explicit True
    # keeps email OFF. Only validate addresses when the user opts in, so a
    # disabled block never blocks a trading tick.
    notify_d = d.get("notify", {}) or {}
    notify_enabled = notify_d.get("enabled", False) is True
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
        buy_type=str(order.get("buy_type", "market")),
        sell_mode=str(order.get("sell_mode", "close_position")),
        time_in_force=str(order.get("time_in_force", "gfd")),
        market_hours=str(order.get("market_hours", "regular_hours")),
        act_after_open_minutes=int(loop.get("act_after_open_minutes", 5)),
        analyze_timeout_sec=int(loop.get("analyze_timeout_sec", 900)),
        notify=notify,
        storage=storage,
        raw=d,
    )
