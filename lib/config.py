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

# Supported LLM providers for the per-role chat_provider/reasoner_provider keys.
# Mirror of tradingagents.llm_clients.factory._OPENAI_COMPATIBLE, kept LOCAL on
# purpose: config.py is a light leaf, and importing the framework here would run
# tradingagents/__init__.py's load_dotenv() as a side effect mid-load — clobbering
# the os.environ of env-isolated callers (tests, the notify-recipient resolution).
_SUPPORTED_LLM_PROVIDERS = (
    "openai", "xai", "deepseek", "qwen", "qwen-cn",
    "glm", "glm-cn", "minimax", "minimax-cn", "ollama", "openrouter",
)


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
    # --- F2/F3 anti-churn knobs -------------------------------------------
    # F2: economic minimum trade size. A rebalance TRIM or BUY-to-target below this dollar
    # notional is SKIPPED (not sent) — kills the sub-$1..$5 churn orders. Full EXITS /
    # reconciles are EXEMPT (they wind a position to zero at RH's $1 fractional floor). The
    # sell path floors the effective minimum at $1 so a configured 0 can never strip that
    # hard RH floor. Default $5 (well above the $1 broker minimum).
    min_trade_notional: float = 5.0
    # F3: conviction target-weight HYSTERESIS (material-change gate). The conviction
    # allocator only MOVES a name's target weight when the fresh reading shifts it by >= this
    # many weight-points; smaller day-to-day noise keeps the PRIOR target (no trim), so a
    # noisy signal can't churn the book. 0 disables (re-clip every tick — the pre-F3 behavior).
    conviction_rebalance_min_delta_pct: float = 2.0


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
    # ``loud_digest`` (default True): the daily run-complete digest pings LOUD so the
    # operator gets a heartbeat every trading day, incl. quiet 0-order days — a silent
    # digest reads as "the bot is dead". Set false to restore the silent-daily-record mode.
    loud_digest: bool = True


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
class LegislativeConfig:
    """Legislative-catalyst settings. Analysis/proposal side only — NEVER trading.

    Fails SAFE: stays disabled unless ``legislative.enabled`` is exactly ``true``; a
    disabled or garbled block never blocks a tick. Thresholds are validated ONLY when
    enabled. The Congress.gov key lives in ``.env`` (``CONGRESS_API_KEY``), never here.
    ``remove_enabled`` defaults OFF — a suffer->REMOVE needs the operator's
    ``ticker_policy_areas`` map (ticker -> Congress policyArea/subject codes) to bind a
    held name to a bill via a structured, non-attacker-controlled field.
    """
    enabled: bool
    api_key: str
    passage_prob_min: float
    impact_min: float
    judge_enabled: bool
    judge_votes: int
    judge_pass_min: int
    max_proposals_per_review: int
    max_bills_analyzed_per_review: int
    lookback_days: int
    add_weight: float
    passage_source: str
    model: str
    remove_enabled: bool
    review_deadline_sec: int
    ticker_policy_areas: dict


@dataclass(frozen=True)
class IntelConfig:
    """Strategy-intelligence settings (section-level power-map -> ADDITIVE strategy proposals).
    Analysis/proposal side only — NEVER trading. Fails SAFE: disabled unless ``intel.enabled`` is
    exactly ``true``. Proposals are always tier 'intel' (human-approve only); the service can only
    ADD (never overwrite the seeded book), and new positions draw from cash capped at
    ``intel_max_total_pct``, so the seeded book can't be diluted below (100 - cap)%."""
    enabled: bool
    min_score: float
    add_weight: float
    intel_max_total_pct: float
    new_sleeve_min_names: int
    threat_score: float
    protected_threat_score: float
    max_sections_per_run: int


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
    legislative: LegislativeConfig
    intel: IntelConfig
    raw: dict
    # Per-role LLM providers (mixed-provider support). Default to "glm" so an
    # absent block/key keeps today's single-provider GLM behavior byte-identical.
    # The quick/chat role (analysts' tool-calling) and the deep/reasoner role
    # (debates/judgment) can name different providers (e.g. deepseek + glm).
    chat_provider: str = "glm"
    reasoner_provider: str = "glm"
    # --- Brain engine (EVE migration) ------------------------------------
    # brain_engine selects the analysis backend. "tradingagents" (default) = the
    # legacy in-tree LangGraph framework (kept live as the rollback path until F8
    # deletes tradingagents/). "eve" = the Node/EVE deep-research agent. The
    # dispatch lives in analyze.py:run_analysis(); the legacy import is deferred
    # into the else-branch so the EVE path never loads tradingagents/. Flip to
    # "eve" only after the live e2e is green on the EVE brain.
    brain_engine: str = "eve"
    eve_dir: str = "quiver_eve"
    eve_url: str = "http://127.0.0.1:2244"  # the EVE server (eve dev / eve start) HTTP endpoint
    research_rounds: int = 1  # F4: bull/bear + risk-debate rounds (default 1; spec max 2)
    # --- Self-learning tail (lib/levers) --------------------------------
    # auto_apply_levers=false (default) = a discovered lever needs human
    # `tick.py levers-approve` to activate (mirrors universe-apply). true =
    # activate on next tick but still score-gated (auto-retire on underperformance).
    # Levers only add EVALUATION inputs; they never touch sizing/caps/ref_ids.
    auto_apply_levers: bool = False
    lever_min_decisions: int = 8
    lever_retire_alpha: float = -0.02
    # --- Strategy layer (Stage 0) ----------------------------------------
    # strategy is Optional[lib.strategy.StrategyConfig] (typed as object to keep
    # config a light leaf with no heavy import). It is None when strategy.yaml is
    # absent OR garbled -> the whole strategy layer is INACTIVE and analyze.py +
    # cmd_plan behave exactly as the validated once-a-day path.
    strategy_path: str = "strategy.yaml"
    strategy: object = None
    # --- Goal deposit auto-capture (observability; OFF by default) --------
    # auto_capture_flows=false (default) = the tick only DETECTS + SURFACES suspected
    # external cash flows (flow-suggest CLI + goal-track ops-log); the operator records
    # them via `tick.py flow-record`. true = the goal-track tail also auto-writes the
    # SETTLED, persisted candidates (confirmable_flows) to cash_flows. Read ONLY by the
    # goal/digest deposit-adjust path — never sizing, order placement, or the daily-loss
    # halt (those never read cash_flows). thresholds tune the jump detector.
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
    # F2/F3 anti-churn knobs (default to sensible non-zero values; 0 disables each).
    try:
        min_trade_notional = float(risk_d.get("min_trade_notional", 5.0) or 0.0)
    except (TypeError, ValueError):
        raise ConfigError("config.yaml: risk.min_trade_notional must be a number")
    if min_trade_notional < 0:
        raise ConfigError("config.yaml: risk.min_trade_notional must be >= 0")
    try:
        conviction_rebalance_min_delta_pct = float(
            risk_d.get("conviction_rebalance_min_delta_pct", 2.0) or 0.0)
    except (TypeError, ValueError):
        raise ConfigError("config.yaml: risk.conviction_rebalance_min_delta_pct must be a number")
    if conviction_rebalance_min_delta_pct < 0:
        raise ConfigError("config.yaml: risk.conviction_rebalance_min_delta_pct must be >= 0")
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
        min_trade_notional=min_trade_notional,
        conviction_rebalance_min_delta_pct=conviction_rebalance_min_delta_pct,
    )

    # Model IDs live under a `glm:` block (the name is historical — providers are
    # now per-role). Fall back to the legacy `deepseek:` key so an older config.yaml
    # never hard-crashes a tick (fail-safe back-compat).
    glm_block = d.get("glm")
    models = glm_block or d.get("deepseek") or {}
    chat_model = str(_require(models, "chat_model")).strip()
    reasoner_model = str(_require(models, "reasoner_model")).strip()
    if "verify" in chat_model.lower() or "verify" in reasoner_model.lower():
        raise ConfigError(
            "config.yaml: model IDs are still placeholders — verify the current "
            "IDs in the provider's model list and set them."
        )
    # Per-role providers. The block-default provider (which block matched) is the
    # fallback for any role that doesn't name its own, so absent keys keep today's
    # behavior: a `glm:` block => both roles glm; a legacy `deepseek:` block => both
    # deepseek. The live mixed setup names each role explicitly (deepseek + glm).
    block_default = "glm" if glm_block else ("deepseek" if d.get("deepseek") else "glm")
    chat_provider = str(models.get("chat_provider", block_default) or block_default).strip().lower()
    reasoner_provider = str(models.get("reasoner_provider", block_default) or block_default).strip().lower()
    for _role, _prov in (("chat_provider", chat_provider), ("reasoner_provider", reasoner_provider)):
        if _prov not in _SUPPORTED_LLM_PROVIDERS:
            raise ConfigError(
                f"config.yaml: {_role}={_prov!r} is not a supported provider "
                f"({', '.join(_SUPPORTED_LLM_PROVIDERS)})."
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
    # Daily digest pings LOUD by default; only an explicit false silences it.
    loud_digest = notify_d.get("loud_digest", True) is not False
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
    # The alert CHANNEL is Telegram, and its creds/recipients are resolved from ENV at send
    # time (TELEGRAM_BOT_TOKEN + TELEGRAM_ALERT_CHAT_IDS/TELEGRAM_ALLOWED_CHAT_IDS via
    # lib.telegram.resolve_env), NOT from config.yaml — mirroring how the last-resort sender
    # already resolves its own creds. So enabling notifications no longer hard-requires an email
    # address here. That relaxation is deliberate and load-bearing: _cfg_and_ledger() (hence
    # every preflight/plan/commit) calls load_config, so a hard-raise on a missing recipient
    # would let a stale/absent NOTIFY_TO abort the WHOLE tick — a real footgun once email is off.
    # A missing/unreachable channel is instead surfaced at RUNTIME (tick.py report-send +
    # run_tick.py _maybe_alert emit `unconfigured`/`no_token`/`no_chats`; the digest footer shows
    # `pager: NOT configured`) and gated at DEPLOY by `tick.py send-test`. The email fields
    # (to/alerts_to/from_addr) are still parsed above so a rollback-to-email needs no config
    # surgery, but they are no longer validated as addresses.
    notify = NotifyConfig(
        enabled=notify_enabled, to=to, from_addr=from_addr, subject_prefix=subject_prefix,
        on_complete=on_complete, on_error=on_error, on_warning=on_warning,
        alerts_to=alerts_to, loud_digest=loud_digest,
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

    # --- Brain engine (EVE migration) ----------------------------------
    # brain.engine: "tradingagents" (default, legacy) | "eve" (Node/EVE agent).
    # FAIL-SAFE: anything other than an explicit "eve" stays on the legacy path
    # (the validated once-a-day brain) — a missing/garbled block never silently
    # flips the live brain.
    brain = d.get("brain", {}) or {}
    brain_engine = str(brain.get("engine", "eve") or "eve").strip().lower()
    if brain_engine not in ("tradingagents", "eve"):
        brain_engine = "eve"
    eve_dir = str(brain.get("eve_dir", "quiver_eve") or "quiver_eve").strip()
    eve_url = str(brain.get("eve_url", os.environ.get("QUIVER_EVE_URL", "http://127.0.0.1:2244"))
                  or "http://127.0.0.1:2244").strip()
    # F4: research_rounds bounds the bull/bear + risk-debate turns (each round =
    # +2 quick-model turns). Default 1 (~8 turns/ticker worst case) preserves the
    # v1 cost ceiling; the spec's max is 2. Raise only with data to justify it.
    try:
        research_rounds = max(1, int(brain.get("research_rounds", 1) or 1))
    except (TypeError, ValueError):
        research_rounds = 1

    # --- Self-learning tail (lib/levers) --------------------------------
    learning = d.get("learning", {}) or {}
    auto_apply_levers = bool(learning.get("auto_apply_levers", False))
    lever_min_decisions = int(learning.get("lever_min_decisions", 8))
    lever_retire_alpha = float(learning.get("lever_retire_alpha", -0.02))

    # --- Legislative catalyst (opt-in; fail-safe OFF) -------------------
    # Parse fields with safe defaults always; validate ranges ONLY when enabled so a
    # disabled/garbled block never blocks a tick (mirrors the notify idiom). The
    # Congress.gov key comes from .env, never config.yaml.
    leg_d = d.get("legislative", {}) or {}

    def _leg_num(key, default, cast):
        # None/garbage -> default (fail-safe), but preserve an explicit 0 so a
        # nonsensical-when-enabled value like impact_min:0 is caught by validation
        # rather than silently swallowed by an `x or default` truthiness check.
        v = leg_d.get(key, default)
        try:
            return cast(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    leg_enabled = leg_d.get("enabled", False) is True
    leg_passage_min = _leg_num("passage_prob_min", 0.55, float)
    leg_impact_min = _leg_num("impact_min", 0.5, float)
    leg_judge_enabled = leg_d.get("judge_enabled", True) is not False
    leg_judge_votes = _leg_num("judge_votes", 3, int)
    leg_judge_pass_min = _leg_num("judge_pass_min", 2, int)
    leg_max_props = _leg_num("max_proposals_per_review", 3, int)
    leg_max_bills = _leg_num("max_bills_analyzed_per_review", 15, int)
    leg_lookback = _leg_num("lookback_days", 3, int)
    leg_add_weight = _leg_num("add_weight", 4.0, float)
    leg_passage_source = str(leg_d.get("passage_source", "heuristic") or "heuristic").strip().lower()
    leg_model = str(leg_d.get("model", "claude") or "claude").strip()
    leg_remove_enabled = leg_d.get("remove_enabled", False) is True
    leg_deadline = int(leg_d.get("review_deadline_sec", 300) or 300)
    _tpa = leg_d.get("ticker_policy_areas", {}) or {}
    leg_tpa = {str(k).upper(): [str(x) for x in (v or [])]
               for k, v in _tpa.items()} if isinstance(_tpa, dict) else {}
    if leg_enabled:
        if not (0.0 < leg_passage_min <= 1.0):
            raise ConfigError("config.yaml: legislative.passage_prob_min must be in (0, 1]")
        if not (0.0 < leg_impact_min <= 1.0):
            raise ConfigError("config.yaml: legislative.impact_min must be in (0, 1]")
        if leg_judge_enabled and (leg_judge_votes < 1 or not (1 <= leg_judge_pass_min <= leg_judge_votes)):
            raise ConfigError("config.yaml: legislative.judge_votes must be >= 1 and "
                              "1 <= judge_pass_min <= judge_votes")
        if leg_add_weight <= 0:
            raise ConfigError("config.yaml: legislative.add_weight must be > 0")
        if leg_passage_source not in ("heuristic", "kalshi_blend"):
            raise ConfigError("config.yaml: legislative.passage_source must be 'heuristic' or 'kalshi_blend'")
        if leg_remove_enabled and not leg_tpa:
            raise ConfigError("config.yaml: legislative.remove_enabled needs a non-empty "
                              "ticker_policy_areas map (a suffer->REMOVE must bind a held name to a "
                              "bill via structured policy codes, never free bill text)")
    legislative_cfg = LegislativeConfig(
        enabled=leg_enabled, api_key=os.environ.get("CONGRESS_API_KEY", "").strip(),
        passage_prob_min=leg_passage_min, impact_min=leg_impact_min,
        judge_enabled=leg_judge_enabled, judge_votes=leg_judge_votes, judge_pass_min=leg_judge_pass_min,
        max_proposals_per_review=leg_max_props, max_bills_analyzed_per_review=leg_max_bills,
        lookback_days=leg_lookback, add_weight=leg_add_weight, passage_source=leg_passage_source,
        model=leg_model, remove_enabled=leg_remove_enabled, review_deadline_sec=leg_deadline,
        ticker_policy_areas=leg_tpa)

    # Strategy-intelligence config — fail-safe OFF; validated only when enabled.
    intel_d = d.get("intel", {}) or {}
    intel_enabled = intel_d.get("enabled", False) is True

    def _intel_num(key, default, cast):
        v = intel_d.get(key, default)
        try:
            return cast(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    intel_min_score = _intel_num("min_score", 0.5, float)
    intel_add_weight = _intel_num("add_weight", 4.0, float)
    intel_max_total = _intel_num("intel_max_total_pct", 20.0, float)
    intel_new_sleeve_min = _intel_num("new_sleeve_min_names", 2, int)
    intel_threat = _intel_num("threat_score", 0.6, float)
    intel_protected_threat = _intel_num("protected_threat_score", 1.5, float)
    intel_max_sections = _intel_num("max_sections_per_run", 120, int)
    if intel_enabled:
        if intel_max_sections < 1:
            raise ConfigError("config.yaml: intel.max_sections_per_run must be >= 1")
        if intel_add_weight <= 0:
            raise ConfigError("config.yaml: intel.add_weight must be > 0")
        if not (0.0 < intel_max_total <= 100.0):
            raise ConfigError("config.yaml: intel.intel_max_total_pct must be in (0, 100]")
        if intel_threat <= 0 or intel_protected_threat <= 0:
            raise ConfigError("config.yaml: intel.threat_score and protected_threat_score must be > 0")
        if intel_protected_threat < intel_threat:
            raise ConfigError("config.yaml: intel.protected_threat_score must be >= threat_score "
                              "(a protected seeded name needs a STRONGER threat to propose an exit)")
    intel_cfg = IntelConfig(
        enabled=intel_enabled, min_score=intel_min_score, add_weight=intel_add_weight,
        intel_max_total_pct=intel_max_total, new_sleeve_min_names=intel_new_sleeve_min,
        threat_score=intel_threat, protected_threat_score=intel_protected_threat,
        max_sections_per_run=intel_max_sections)

    # --- Goal / deposit-auto-capture (observability; fail-safe, MUST NOT raise) --------
    # load_config runs at the START of every subcommand (before the best-effort tail), so a
    # garbled goal: field here must NEVER raise ConfigError and stop a tick. Coerce each
    # threshold to its default on bad input (mirrors the _leg_num idiom); no range validation.
    # The write path (auto_capture_flows) is strictly opt-in via `is True` — a quoted/stray
    # value can't silently enable it. Read ONLY by the goal/digest deposit-adjust path.
    goal_d = d.get("goal", {}) or {}

    def _goal_num(key, default, cast):
        v = goal_d.get(key, default)
        try:
            return cast(v) if v is not None else default
        except (TypeError, ValueError):
            return default

    goal_auto_capture_flows = goal_d.get("auto_capture_flows", False) is True
    goal_flow_min_pct = _goal_num("flow_min_pct", 25.0, float)
    goal_flow_min_abs = _goal_num("flow_min_abs", 30.0, float)
    goal_flow_confirm_days = _goal_num("flow_confirm_days", 2, int)

    return Config(
        account_number=account,
        dry_run=dry_run,
        kill_switch_file=str(_require(d, "kill_switch_file")),
        risk=risk,
        chat_model=chat_model,
        reasoner_model=reasoner_model,
        chat_provider=chat_provider,
        reasoner_provider=reasoner_provider,
        brain_engine=brain_engine,
        eve_dir=eve_dir,
        eve_url=eve_url,
        research_rounds=research_rounds,
        auto_apply_levers=auto_apply_levers,
        lever_min_decisions=lever_min_decisions,
        lever_retire_alpha=lever_retire_alpha,
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
        legislative=legislative_cfg,
        intel=intel_cfg,
        raw=d,
        strategy_path=strategy_path,
        strategy=strategy_obj,
        goal_auto_capture_flows=goal_auto_capture_flows,
        goal_flow_min_pct=goal_flow_min_pct,
        goal_flow_min_abs=goal_flow_min_abs,
        goal_flow_confirm_days=goal_flow_confirm_days,
    )
