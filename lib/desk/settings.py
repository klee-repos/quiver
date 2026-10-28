"""Effective desk settings: the config start values with the ``desk_settings`` overlay.

``config.yaml`` (``risk:`` and ``desk:``) and ``strategy.yaml`` (``risk_policy``) give the
start values. ``desk-seed`` writes them into ``desk_settings``. After that a confirmed
``set_risk`` proposal changes a row, and this module validates every value against its
§10.3 range both when the desk proposes it and when the tap applies it.
"""

from __future__ import annotations

import json
from typing import Optional

from lib.config import DESK_RANGES

# key -> (low, high). cash_floor_pct also takes None (follow the cash sleeve's weight).
RANGES = {
    "daily_loss_halt_pct": (5.0, 50.0),
    "min_buying_power_buffer": (0.0, 1000.0),
    "min_trade_notional": (1.0, 500.0),
    "requote_tolerance_pct": (DESK_RANGES["requote_tolerance_pct"][1], DESK_RANGES["requote_tolerance_pct"][2]),
    "cash_floor_pct": (0.0, 100.0),
    "per_name_max_pct": (1.0, 100.0),
    "margin_of_safety_pct": (DESK_RANGES["margin_of_safety_pct"][1], DESK_RANGES["margin_of_safety_pct"][2]),
    "valuation_discount_rate": (DESK_RANGES["valuation_discount_rate"][1], DESK_RANGES["valuation_discount_rate"][2]),
    "valuation_terminal_growth": (DESK_RANGES["valuation_terminal_growth"][1], DESK_RANGES["valuation_terminal_growth"][2]),
    "valuation_years": (DESK_RANGES["valuation_years"][1], DESK_RANGES["valuation_years"][2]),
}
KEYS = tuple(RANGES)
_INT_KEYS = {"valuation_years"}


class SettingError(ValueError):
    """A key the desk does not know, or a value outside its range."""


def validate(key: str, value):
    """Return the value as stored (float, int, or None for cash_floor_pct). Raise
    ``SettingError`` for an unknown key or a value outside the §10.3 range."""
    if key not in RANGES:
        raise SettingError(f"unknown setting '{key}'; settings are: {', '.join(KEYS)}")
    if value is None:
        if key == "cash_floor_pct":
            return None
        raise SettingError(f"{key} needs a number")
    if isinstance(value, bool):
        raise SettingError(f"{key} needs a number, got {value!r}")
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise SettingError(f"{key} needs a number, got {value!r}")
    if v != v:  # NaN
        raise SettingError(f"{key} needs a number, got NaN")
    low, high = RANGES[key]
    if v < low or v > high:
        raise SettingError(f"{key} must be {low:g} to {high:g}, got {v:g}")
    if key in _INT_KEYS:
        if v != int(v):
            raise SettingError(f"{key} must be a whole number, got {v:g}")
        return int(v)
    return v


def start_values(cfg) -> dict:
    """The §10.3 start value of each key, from config.yaml and strategy.yaml."""
    rp = {}
    if getattr(cfg, "strategy", None) is not None:
        rp = dict(getattr(cfg.strategy, "risk_policy", {}) or {})
    per_name = rp.get("per_name_max_pct")
    cash_floor = rp.get("cash_floor_pct")
    return {
        "daily_loss_halt_pct": float(cfg.risk.daily_loss_halt_pct),
        "min_buying_power_buffer": float(cfg.risk.min_buying_power_buffer),
        "min_trade_notional": max(1.0, float(cfg.risk.min_trade_notional)),
        "requote_tolerance_pct": float(cfg.desk.requote_tolerance_pct),
        "cash_floor_pct": None if cash_floor is None else float(cash_floor),
        "per_name_max_pct": float(per_name) if per_name is not None else 25.0,
        "margin_of_safety_pct": float(cfg.desk.margin_of_safety_pct),
        "valuation_discount_rate": float(cfg.desk.valuation_discount_rate),
        "valuation_terminal_growth": float(cfg.desk.valuation_terminal_growth),
        "valuation_years": int(cfg.desk.valuation_years),
    }


def effective(conn, cfg) -> dict:
    """The start values with each valid ``desk_settings`` row laid over them. A stored
    row that fails validation is skipped, so a bad row can never widen a limit."""
    out = start_values(cfg)
    for row in conn.execute("SELECT key, value_json FROM desk_settings").fetchall():
        key = row[0]
        if key not in RANGES:
            continue
        try:
            out[key] = validate(key, json.loads(row[1]))
        except (SettingError, ValueError, TypeError):
            continue
    return out


def write(conn, key: str, value, now: str, proposal_id: Optional[int] = None) -> None:
    """Validate, then upsert one ``desk_settings`` row."""
    v = validate(key, value)
    conn.execute("INSERT INTO desk_settings (key, value_json, updated_at, proposal_id) "
                 "VALUES (?, ?, ?, ?) ON CONFLICT(key) DO UPDATE SET "
                 "value_json=excluded.value_json, updated_at=excluded.updated_at, "
                 "proposal_id=excluded.proposal_id", (key, json.dumps(v), now, proposal_id))


def seed(conn, cfg, now: str) -> int:
    """Write each absent key with its start value (``desk-seed`` step 5). Returns the
    number of rows written. A present key keeps its value."""
    n = 0
    for key, value in start_values(cfg).items():
        exists = conn.execute("SELECT 1 FROM desk_settings WHERE key=?", (key,)).fetchone()
        if exists:
            continue
        write(conn, key, value, now, None)
        n += 1
    return n


def cash_floor_pct(settings: dict, book_rows: list, cash_ticker: str) -> float:
    """The cash floor in % of deployable. ``None`` follows the cash sleeve's target
    weight, so a change to that weight moves the floor with it."""
    v = settings.get("cash_floor_pct")
    if v is not None:
        return float(v)
    cash = str(cash_ticker or "SGOV").upper()
    for r in book_rows:
        if str(r.get("ticker", "")).upper() == cash and r.get("status", "active") == "active":
            return float(r.get("target_weight") or 0.0)
    return 0.0
