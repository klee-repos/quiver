"""Load and validate config.yaml into a typed Config object.

Fails SAFE: a missing/garbled ``dry_run`` is treated as ``True`` (paper mode),
never as live trading. Invalid required fields raise immediately so the
orchestrator stops instead of trading on a bad config.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List

import yaml

_PLACEHOLDER_ACCOUNT = "XXXXXXXX"


@dataclass(frozen=True)
class RiskConfig:
    max_dollars_per_trade: float
    daily_loss_halt_pct: float
    daily_capital_deploy_cap: float
    min_buying_power_buffer: float
    # Intraday-only caps (default 1 == classic once-a-day behavior). max_actions
    # bounds repeat TRADES per ticker/day; max_analyses is the LLM-cost circuit
    # breaker bounding repeat ANALYSES per ticker/day.
    max_actions_per_ticker_per_day: int
    max_analyses_per_ticker_per_day: int
    # --- Strategy-layer rebalance knobs (Stage 3) -------------------------
    # Default to TODAY's behavior: rebalance OFF -> cmd_plan is byte-identical.
    # (The analysis universe is always the active portfolio book — see
    # tick.py:_analysis_universe — there is no separate watchlist to drive it.)
    rebalance_enabled: bool = False
    rebalance_drift_band_pct: float = 5.0
    cash_sleeve_ticker: str = "SGOV"      # the residual ballast; never churned like an engine name
    # Self-reconciliation: when a strategy goal is active, SELL any held position that
    # is NOT in the book (and not the cash sleeve) down to zero — the bot heals its own
    # drift from a book edit / a prior watchlist. Long-only (only ever sells; de-risks
    # to cash) and independent of rebalance_enabled. Dormant with no active goal, so the
    # validated classic path is unchanged. Default ON; set false to keep off-book holdings.
    reconcile_unmanaged: bool = True
    # --- Memory-grounded strategy-consistency gate (Component A) ----------
    # Suppresses RANDOM cross-day buy<->sell reversals while allowing those grounded in
    # a consistent recorded strategy (a stop/target/review trigger firing, or a genuine
    # basis change within the churn budget). NOT a calendar dwell. Default ON because it
    # only ever SUPPRESSES a reversal (strictly more conservative); a fresh ledger never
    # reverses, so the validated path is unchanged. consistency_enabled=false restores
    # byte-identical classic behavior.
    consistency_enabled: bool = True
    max_discretionary_reversals: int = 1   # ungrounded "changed my mind" flips tolerated per window
    consistency_flip_window: int = 6       # how many recent COMPLETED trades the budget looks back over
    loss_catalyst_pct: float = 8.0         # a sell within the window is grounded if the position is down >= this


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
    # Per-event toggles (default ON for run-complete + critical alerts; warning
    # hiccups OFF by default — they self-heal and would otherwise train the operator
    # to ignore the channel). ``alerts_to`` lets critical alerts route to a separate
    # pager list; it falls back to ``to`` when unset.
    on_complete: bool = True
    on_error: bool = True
    on_warning: bool = False
    alerts_to: List[str] = field(default_factory=list)


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
class MemoryConfig:
    """Reflective-memory + deterministic-risk settings (analysis CONTEXT only).

    Feeds the LLM agents' reasoning; NEVER changes sizing/clamps. Unlike notify/
    intraday (opt-in, fail-safe OFF), this defaults ON — it's read-only and
    non-trading, and the design wants it on every run. Bad threshold ordering
    (reduced > elevated) raises rather than silently mis-guiding.
    """
    enabled: bool
    dir: str
    risk_free_rate: float
    low_confidence_min_n: int
    rolling_window: int
    periods_per_year: float
    hit_rate_elevated: float
    hit_rate_reduced: float
    sharpe_elevated: float
    sharpe_reduced: float

    def thresholds(self):
        """Build the lib.risk.GuidanceThresholds this config implies (lazy import
        to keep config a light leaf)."""
        from lib.risk import GuidanceThresholds
        return GuidanceThresholds(
            low_confidence_min_n=self.low_confidence_min_n,
            hit_rate_elevated=self.hit_rate_elevated,
            hit_rate_reduced=self.hit_rate_reduced,
            sharpe_elevated=self.sharpe_elevated,
            sharpe_reduced=self.sharpe_reduced,
            rolling_window=self.rolling_window,
            periods_per_year=self.periods_per_year,
        )


@dataclass(frozen=True)
class Config:
    account_number: str
    dry_run: bool
    kill_switch_file: str
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
    memory: MemoryConfig
    raw: dict
    # --- Strategy layer (Stage 0) ----------------------------------------
    # strategy is Optional[lib.strategy.StrategyConfig] (typed as object to keep
    # config a light leaf with no heavy import). It is None when strategy.yaml is
    # absent OR garbled -> the whole strategy layer is INACTIVE and analyze.py +
    # cmd_plan behave exactly as the validated once-a-day path.
    strategy_path: str = "strategy.yaml"
    strategy: object = None


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
    # alongside GLM_API_KEY. Load it here so every entrypoint (tick.py,
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

    # The trading universe is DERIVED from the active portfolio book (strategy.yaml
    # / the ledger goal), not a hand-maintained list — see tick.py:_analysis_universe.
    # config.yaml carries no `watchlist`.

    risk_d = d.get("risk", {}) or {}
    # Strategy-layer rebalance knobs. Default to TODAY's behavior so the validated
    # path is unchanged until rebalance_enabled is explicitly true.
    rebalance_enabled = risk_d.get("rebalance_enabled", False) is True
    try:
        rebalance_band = float(risk_d.get("rebalance_drift_band_pct", 5.0) or 5.0)
    except (TypeError, ValueError):
        raise ConfigError("config.yaml: risk.rebalance_drift_band_pct must be a number")
    if rebalance_band <= 0:
        raise ConfigError("config.yaml: risk.rebalance_drift_band_pct must be > 0")
    cash_sleeve_ticker = str(risk_d.get("cash_sleeve_ticker", "SGOV") or "SGOV").strip().upper()
    # Default ON (only an explicit false disables): selling off-book holdings to cash is
    # the safe de-risking direction, and it only acts when a strategy goal is active.
    reconcile_unmanaged = risk_d.get("reconcile_unmanaged", True) is not False
    # Strategy-consistency gate (Component A). A nested `consistency` block; all keys
    # optional with safe defaults. Fail-safe ON; an explicit false disables.
    cons_d = risk_d.get("consistency", {}) or {}
    consistency_enabled = cons_d.get("enabled", True) is not False
    try:
        max_discretionary_reversals = int(cons_d.get("max_discretionary_reversals", 1))
        consistency_flip_window = int(cons_d.get("flip_window", 6))
        loss_catalyst_pct = float(cons_d.get("loss_catalyst_pct", 8.0))
    except (TypeError, ValueError):
        raise ConfigError("config.yaml: risk.consistency.{max_discretionary_reversals,"
                          "flip_window,loss_catalyst_pct} must be numbers")
    if max_discretionary_reversals < 0:
        raise ConfigError("config.yaml: risk.consistency.max_discretionary_reversals must be >= 0")
    if consistency_flip_window <= 0:
        raise ConfigError("config.yaml: risk.consistency.flip_window must be > 0")
    if loss_catalyst_pct < 0:
        raise ConfigError("config.yaml: risk.consistency.loss_catalyst_pct must be >= 0")
    risk = RiskConfig(
        max_dollars_per_trade=_pos_num(risk_d, "max_dollars_per_trade"),
        daily_loss_halt_pct=_pos_num(risk_d, "daily_loss_halt_pct"),
        daily_capital_deploy_cap=_pos_num(risk_d, "daily_capital_deploy_cap"),
        min_buying_power_buffer=float(risk_d.get("min_buying_power_buffer", 0) or 0),
        max_actions_per_ticker_per_day=_pos_int(risk_d, "max_actions_per_ticker_per_day", 1),
        max_analyses_per_ticker_per_day=_pos_int(risk_d, "max_analyses_per_ticker_per_day", 1),
        rebalance_enabled=rebalance_enabled,
        rebalance_drift_band_pct=rebalance_band,
        cash_sleeve_ticker=cash_sleeve_ticker,
        reconcile_unmanaged=reconcile_unmanaged,
        consistency_enabled=consistency_enabled,
        max_discretionary_reversals=max_discretionary_reversals,
        consistency_flip_window=consistency_flip_window,
        loss_catalyst_pct=loss_catalyst_pct,
    )

    # Model IDs live under a `glm:` block. Fall back to the legacy `deepseek:` key
    # so an older config.yaml never hard-crashes a tick (fail-safe back-compat).
    models = d.get("glm") or d.get("deepseek") or {}
    chat_model = str(_require(models, "chat_model")).strip()
    reasoner_model = str(_require(models, "reasoner_model")).strip()
    if "verify" in chat_model.lower() or "verify" in reasoner_model.lower():
        raise ConfigError(
            "config.yaml: glm model IDs are still placeholders — verify the "
            "current IDs in the z.ai model list and set them."
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
    # Per-event toggles. on_complete/on_error default ON (preserve today's always-send
    # behavior for the digest + critical alerts); on_warning defaults OFF (best-effort
    # hiccups self-heal — fold them into the digest, don't page). `... is not False`
    # so only an explicit false disables; anything else stays on.
    on_complete = notify_d.get("on_complete", True) is not False
    on_error = notify_d.get("on_error", True) is not False
    on_warning = notify_d.get("on_warning", False) is True
    # Critical alerts can route to a separate pager list. Env NOTIFY_ALERTS_TO wins,
    # then config notify.alerts_to, else fall back to the digest recipients (`to`).
    alerts_env = os.environ.get("NOTIFY_ALERTS_TO", "").strip()
    if alerts_env:
        alerts_raw = alerts_env.split(",")
    else:
        alerts_raw = notify_d.get("alerts_to", None)
        if alerts_raw is None:
            alerts_raw = to_raw  # inherit the digest recipients
        elif isinstance(alerts_raw, str):
            alerts_raw = [alerts_raw]
    alerts_to = [str(x).strip() for x in (alerts_raw or []) if str(x).strip()]
    if notify_enabled:
        if not to or any("@" not in addr for addr in to):
            raise ConfigError(
                "config.yaml: notify.to must be a non-empty list of email "
                "addresses when notify.enabled is true"
            )
        # `from` is optional for the in-tick MCP path (blank means the Resend MCP's own
        # SENDER_EMAIL_ADDRESS is used). The Python last-resort HTTP sender, however,
        # has no implicit sender — it resolves RESEND_FROM/notify.from at send time and
        # skips loudly if both are blank (see lib/mailer). Only validate the override.
        if from_addr and "@" not in from_addr:
            raise ConfigError(
                "config.yaml: notify.from, if set, must be a sender email address "
                "(or leave it blank to use the Resend MCP's configured sender)"
            )
        # When critical alerts are enabled, the resolved alert recipients (post-fallback)
        # must be valid — else a real incident would page no one.
        if on_error and (not alerts_to or any("@" not in addr for addr in alerts_to)):
            raise ConfigError(
                "config.yaml: notify.alerts_to (or its fallback notify.to / NOTIFY_TO) "
                "must be a non-empty list of email addresses when notify.on_error is true"
            )
        # NOTE: a blank `from` is deliberately allowed even with on_error — the in-tick
        # MCP path has an implicit sender. The Python last-resort pager (run_tick.py)
        # has none, so a blank from + no RESEND_FROM leaves THAT path unconfigured; that
        # is surfaced at runtime (run_tick emits `alert_unconfigured`, the digest footer
        # shows "last-resort alerting: NOT configured") and is gated by `tick.py
        # send-test` at deploy — not hard-failed here, so the MCP-only config stays valid.
    notify = NotifyConfig(
        enabled=notify_enabled, to=to, from_addr=from_addr, subject_prefix=subject_prefix,
        on_complete=on_complete, on_error=on_error, on_warning=on_warning,
        alerts_to=alerts_to,
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

    # Reflective memory + deterministic risk metrics. Analysis CONTEXT only —
    # never sizing. Defaults ON (opt-out): a missing block is fine and uses the
    # validated defaults below. Bad threshold ordering raises here, not at runtime.
    mem_d = d.get("memory", {}) or {}
    memory_enabled = mem_d.get("enabled", True) is not False
    mem_dir = str(mem_d.get("dir", "state/memory/reflect") or "state/memory/reflect").strip()
    if not os.path.isabs(mem_dir):
        mem_dir = str(p.resolve().parent / mem_dir)
    try:
        mem_rf = float(mem_d.get("risk_free_rate", 0.0) or 0.0)
    except (TypeError, ValueError):
        raise ConfigError("config.yaml: memory.risk_free_rate must be a number")
    if mem_rf < 0:
        raise ConfigError("config.yaml: memory.risk_free_rate must be >= 0")
    mem_min_n = _pos_int(mem_d, "low_confidence_min_n", 5)
    mem_window = _pos_int(mem_d, "rolling_window", 10)
    try:
        mem_ppy = float(mem_d.get("periods_per_year", 50.0) or 50.0)
    except (TypeError, ValueError):
        raise ConfigError("config.yaml: memory.periods_per_year must be a number")
    if mem_ppy <= 0:
        raise ConfigError("config.yaml: memory.periods_per_year must be > 0")
    conv = mem_d.get("conviction", {}) or {}

    def _conv_num(key: str, default: float) -> float:
        try:
            return float(conv.get(key, default))
        except (TypeError, ValueError):
            raise ConfigError(f"config.yaml: memory.conviction.{key} must be a number")

    hit_el = _conv_num("hit_rate_elevated", 0.60)
    hit_rd = _conv_num("hit_rate_reduced", 0.40)
    shp_el = _conv_num("sharpe_elevated", 0.50)
    shp_rd = _conv_num("sharpe_reduced", 0.0)
    for nm, v in (("hit_rate_elevated", hit_el), ("hit_rate_reduced", hit_rd)):
        if not (0.0 <= v <= 1.0):
            raise ConfigError(f"config.yaml: memory.conviction.{nm} must be in [0, 1]")
    if hit_rd > hit_el:
        raise ConfigError(
            "config.yaml: memory.conviction.hit_rate_reduced must be <= hit_rate_elevated"
        )
    if shp_rd > shp_el:
        raise ConfigError(
            "config.yaml: memory.conviction.sharpe_reduced must be <= sharpe_elevated"
        )
    memory = MemoryConfig(
        enabled=memory_enabled, dir=mem_dir, risk_free_rate=mem_rf,
        low_confidence_min_n=mem_min_n, rolling_window=mem_window, periods_per_year=mem_ppy,
        hit_rate_elevated=hit_el, hit_rate_reduced=hit_rd,
        sharpe_elevated=shp_el, sharpe_reduced=shp_rd,
    )

    # Strategy layer (15%-goal macro book). OPTIONAL + FAIL-SAFE: heavy parsing
    # lives in lib.strategy (keeps config a light leaf). An absent file -> None;
    # a GARBLED file is caught here and ALSO degrades to None, so a malformed
    # strategy.yaml can never crash a running tick — the layer just goes INACTIVE.
    # (`tick.py strategy-set` and the tests call load_strategy directly to get the
    # strict validate-or-raise behavior at setup time.)
    strategy_path = str(d.get("strategy_path", "strategy.yaml") or "strategy.yaml")
    if not os.path.isabs(strategy_path):
        strategy_path = str(p.resolve().parent / strategy_path)
    strategy_obj = None
    if os.path.exists(strategy_path):
        try:
            from lib.strategy import load_strategy
            strategy_obj = load_strategy(strategy_path)
        except Exception:
            strategy_obj = None

    return Config(
        account_number=account,
        dry_run=dry_run,
        kill_switch_file=str(_require(d, "kill_switch_file")),
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
        memory=memory,
        raw=d,
        strategy_path=strategy_path,
        strategy=strategy_obj,
    )
