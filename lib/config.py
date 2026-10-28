"""Load and validate config.yaml into a typed Config object.

Invalid required fields raise ``ConfigError`` at once, so a runner stops instead of
acting on a bad config. There is no dry-run mode: every order needs the owner's
Telegram tap, and that tap is the gate (plan §0).

``config.yaml`` carries no secrets. ``RH_ACCOUNT_NUMBER`` and the other keys come from
``.env`` on the laptop and from ``/etc/quiver/quiver.env`` on the box.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import yaml

_PLACEHOLDER_ACCOUNT = "XXXXXXXX"


@dataclass(frozen=True)
class RiskConfig:
    """The risk start values. ``desk-seed`` copies the four tunable ones into
    ``desk_settings``; ``lib/desk/settings.py`` then owns them (plan §10.3)."""
    daily_loss_halt_pct: float
    min_buying_power_buffer: float
    rebalance_drift_band_pct: float = 5.0
    cash_sleeve_ticker: str = "SGOV"      # the residual cash sleeve; never bought or sold
    min_trade_notional: float = 5.0


@dataclass(frozen=True)
class NotifyConfig:
    """Alert delivery (Telegram). Fails SAFE: disabled unless ``notify.enabled`` is
    exactly ``true``. The Telegram keys live in the environment, never here."""
    enabled: bool
    on_error: bool = True
    on_warning: bool = False


@dataclass(frozen=True)
class StorageConfig:
    """Retention for the log directories. ``retention_days <= 0`` disables pruning.
    No desk table is ever pruned (plan §6.4)."""
    retention_days: int
    archive_enabled: bool
    archive_backend: str
    archive_bucket: str
    archive_prefix: str


# (key, default, low, high). ``None`` as low/high means no bound on that side.
DESK_RANGES = {
    "requote_tolerance_pct": (1.5, 0.25, 5.0),
    "margin_of_safety_pct": (15.0, 0.0, 50.0),
    "valuation_discount_rate": (0.09, 0.05, 0.15),
    "valuation_terminal_growth": (0.03, 0.0, 0.04),
    "valuation_years": (10, 5, 15),
}


@dataclass(frozen=True)
class DeskDefaults:
    """The ``desk:`` block: start values for the §10.3 settings that the older
    ``risk:`` block lacks. Each value is checked against its §10.3 range."""
    requote_tolerance_pct: float = 1.5
    margin_of_safety_pct: float = 15.0
    valuation_discount_rate: float = 0.09
    valuation_terminal_growth: float = 0.03
    valuation_years: int = 10


@dataclass(frozen=True)
class Config:
    account_number: str
    kill_switch_file: str           # absolute path; resolved against the config file's directory
    risk: RiskConfig
    time_in_force: str
    market_hours: str
    notify: NotifyConfig
    storage: StorageConfig
    desk: DeskDefaults
    raw: dict
    # strategy is Optional[lib.strategy.StrategyConfig] (typed as object to keep config a
    # light leaf). It is None when strategy.yaml is absent or garbled. Only `desk-seed`
    # reads it: after the cutover the ledger holds the book.
    strategy_path: str = "strategy.yaml"
    strategy: object = None
    # Goal deposit capture (observability only; fail-safe, never raises).
    goal_auto_capture_flows: bool = False
    goal_flow_min_pct: float = 25.0
    goal_flow_min_abs: float = 30.0
    goal_flow_confirm_days: int = 2


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


def _desk_defaults(d: dict) -> DeskDefaults:
    block = d.get("desk", {}) or {}
    values = {}
    for key, (default, low, high) in DESK_RANGES.items():
        raw = block.get(key, default)
        try:
            v = int(raw) if key == "valuation_years" else float(raw)
        except (TypeError, ValueError):
            raise ConfigError(f"config.yaml: desk.{key} must be a number, got {raw!r}")
        if v < low or v > high:
            raise ConfigError(f"config.yaml: desk.{key} must be {low} to {high}, got {v}")
        values[key] = v
    return DeskDefaults(**values)


def load_config(path) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"config.yaml not found at {p}")
    d = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    root = p.resolve().parent

    # The secret values come from .env. Load it once so every entrypoint sees the same
    # values. A missing python-dotenv or .env is fine: the box sets the real environment.
    try:
        from dotenv import load_dotenv

        load_dotenv(root / ".env")
    except Exception:
        pass

    account = (os.environ.get("RH_ACCOUNT_NUMBER") or str(d.get("account_number", "") or "")).strip()
    if not account or account == _PLACEHOLDER_ACCOUNT:
        raise ConfigError(
            "account_number is not set. Put RH_ACCOUNT_NUMBER=<your agentic_allowed "
            "Robinhood account number> in .env (it must NOT be committed)."
        )

    # KILL resolves against the config file's directory, not the process cwd: four
    # processes read it (monitor, worker, broker, chat bridge).
    kill = str(_require(d, "kill_switch_file"))
    if not os.path.isabs(kill):
        kill = str(root / kill)

    risk_d = d.get("risk", {}) or {}
    try:
        band = float(risk_d.get("rebalance_drift_band_pct", 5.0) or 5.0)
    except (TypeError, ValueError):
        raise ConfigError("config.yaml: risk.rebalance_drift_band_pct must be a number")
    if band <= 0:
        raise ConfigError("config.yaml: risk.rebalance_drift_band_pct must be > 0")
    try:
        min_trade_notional = float(risk_d.get("min_trade_notional", 5.0) or 0.0)
    except (TypeError, ValueError):
        raise ConfigError("config.yaml: risk.min_trade_notional must be a number")
    if min_trade_notional < 0:
        raise ConfigError("config.yaml: risk.min_trade_notional must be >= 0")
    try:
        buffer = float(risk_d.get("min_buying_power_buffer", 0) or 0)
    except (TypeError, ValueError):
        raise ConfigError("config.yaml: risk.min_buying_power_buffer must be a number")
    risk = RiskConfig(
        daily_loss_halt_pct=_pos_num(risk_d, "daily_loss_halt_pct"),
        min_buying_power_buffer=buffer,
        rebalance_drift_band_pct=band,
        cash_sleeve_ticker=str(risk_d.get("cash_sleeve_ticker", "SGOV") or "SGOV").strip().upper(),
        min_trade_notional=min_trade_notional,
    )

    order = d.get("order", {}) or {}

    notify_d = d.get("notify", {}) or {}
    notify = NotifyConfig(
        enabled=notify_d.get("enabled", False) is True,
        on_error=notify_d.get("on_error", True) is not False,
        on_warning=notify_d.get("on_warning", False) is True,
    )

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
        raise ConfigError("config.yaml: storage.archive.bucket is required when archive.enabled is true")
    storage = StorageConfig(retention_days=retention_days, archive_enabled=archive_enabled,
                            archive_backend=archive_backend, archive_bucket=archive_bucket,
                            archive_prefix=archive_prefix)

    # Strategy seed input. OPTIONAL + FAIL-SAFE: an absent or garbled file gives None.
    strategy_path = str(d.get("strategy_path", "strategy.yaml") or "strategy.yaml")
    if not os.path.isabs(strategy_path):
        strategy_path = str(root / strategy_path)
    strategy_obj = None
    if os.path.exists(strategy_path):
        try:
            from lib.strategy import load_strategy
            strategy_obj = load_strategy(strategy_path)
        except Exception:  # noqa: BLE001 — a bad strategy.yaml must not stop a runner
            strategy_obj = None

    # Goal deposit capture: a garbled value must never raise. Coerce to the default.
    goal_d = d.get("goal", {}) or {}

    def _goal_num(key, default, cast):
        v = goal_d.get(key, default)
        try:
            return cast(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    return Config(
        account_number=account,
        kill_switch_file=kill,
        risk=risk,
        time_in_force=str(order.get("time_in_force", "gfd")),
        market_hours=str(order.get("market_hours", "regular_hours")),
        notify=notify,
        storage=storage,
        desk=_desk_defaults(d),
        raw=d,
        strategy_path=strategy_path,
        strategy=strategy_obj,
        goal_auto_capture_flows=goal_d.get("auto_capture_flows", False) is True,
        goal_flow_min_pct=_goal_num("flow_min_pct", 25.0, float),
        goal_flow_min_abs=_goal_num("flow_min_abs", 30.0, float),
        goal_flow_confirm_days=_goal_num("flow_confirm_days", 2, int),
    )
