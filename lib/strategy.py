"""Load and validate strategy.yaml into a frozen StrategyConfig.

The pure strategy layer: book DATA + deterministic active-book selection +
sleeve-thesis lookup. It encodes the 15%/12mo goal, the macro thesis, and the
two books (core_55_45 default, dial_up_63_37 OFF) as data the Python brain
consumes — it does NOT size, rebalance, touch the broker, or read trading
limits. Imports stdlib + yaml only (plus, later, a thin read-only ledger
wrapper); it never imports lib.signals/lib.risk so the decision wall holds.

Two failure postures, on purpose:
  * load_strategy() is STRICT (validate-or-raise) — used by `tick.py
    strategy-set` and the tests, so a bad book fails loudly at setup.
  * the lib.config lazy hook is FORGIVING — it catches any load failure and
    leaves cfg.strategy=None, so a malformed file makes the strategy layer
    INACTIVE rather than crashing a running tick. An absent file is also None.

Either way the layer never auto-switches to the riskier dial-up book: selecting
dial-up requires both a DEPLOY macro reading AND dial_up_63_37.enabled: true.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

# The sleeve label that marks the cash ballast. The cash holding is the residual
# (never churned like an engine name) and carries no rebalance band.
CASH_SLEEVE = "Cash"

# Weights are author-entered percentages; allow a small rounding slack so a book
# that sums to 99.7 or 100.2 still validates while a real typo (sums to 90) does not.
_WEIGHT_SUM_TOLERANCE = 0.5

# Dead-band (core-PCE % points) around the deploy/standdown triggers inside which a
# reading is treated as no-clear-signal (HOLD), mirroring lib/goal._REGIME_DEADBAND_PCT.
# Defined here (before the dataclasses) so ConsistencyConfig can default to it.
REGIME_DEADBAND_PCE = 0.1


class StrategyError(ValueError):
    """Raised by load_strategy on a missing or invalid strategy.yaml."""


@dataclass(frozen=True)
class Goal:
    target_return_pct: float
    horizon_months: int
    benchmark: str
    benchmark_annual_pct: float
    constraint: str


@dataclass(frozen=True)
class MacroThesis:
    version: str
    summary: str
    catalysts_to_watch: Tuple[str, ...]
    deploy_trigger_pce_pct: float
    standdown_trigger_pce_pct: float
    standdown_on_hike: bool
    correlation_note: str


@dataclass(frozen=True)
class Holding:
    sleeve: str
    ticker: str
    weight: float
    band: float          # 0.0 for the cash residual (no rebalance band)
    quotable: bool       # False -> equities MCP can't price it (spot crypto)
    proxy_ticker: Optional[str]

    @property
    def is_cash(self) -> bool:
        return self.sleeve == CASH_SLEEVE


@dataclass(frozen=True)
class Book:
    name: str
    default: bool
    enabled: bool        # an OFF book (dial-up) may be recommended but not activated
    holdings: Tuple[Holding, ...]


@dataclass(frozen=True)
class LearningConfig:
    underperf_window: int
    underperf_hit_floor: float
    underperf_mean_floor: float
    min_resolved_n: int
    sleeve_review_min_n: int
    derisk_on_standdown: bool
    auto_apply_derisk: bool
    auto_apply_universe_changes: bool
    goal_gap_derisk_pct: float
    proposal_expiry_days: int


@dataclass(frozen=True)
class ConsistencyConfig:
    """Strategic-change consistency knobs (Component C/D/E). All memory-grounded, not
    calendar rules baked into code — they parametrize the regime confirmation, the
    strategy-set min-interval note, and the universe-change anti-oscillation."""
    regime_confirm_n: int = 2              # consecutive confirming readings before a regime flips
    regime_min_dwell_days: int = 5         # min days an effective regime holds before it can change
    regime_deadband_pce: float = REGIME_DEADBAND_PCE  # band around the deploy/standdown triggers
    proposal_cooldown_days: int = 5        # suppress re-proposing a hash decided within this window (E1)
    universe_confirm_days: int = 2         # distinct days a REMOVE/DERISK must recur to be actionable (E2)
    min_strategy_set_interval_days: int = 0  # informational note when strategy-set re-runs sooner (D3)


@dataclass(frozen=True)
class StrategyConfig:
    goal: Goal
    macro_thesis: MacroThesis
    rh_tradable_confirmed: frozenset
    books: Dict[str, Book]
    learning: LearningConfig
    default_book: str
    consistency: "ConsistencyConfig" = None  # type: ignore[assignment]
    # The allocation POLICY (Q1): per-name/sleeve caps, cash floor, conviction curve,
    # smoothing, uncertainty damp, regime floor — the operator's strategy knobs that
    # lib.allocate consumes as `policy`. Passed through as a dict; lib.allocate defaults
    # every missing key, so an absent risk_policy is the safe (fallback) behavior.
    risk_policy: Dict[str, object] = field(default_factory=dict)
    # Per-SLEEVE screen criteria (Q3): {sleeve_name: {"screen": {sector, market_cap_min,
    # pe_max, momentum_window, ...}}}. The strategy (a sleeve + its screen) is durable;
    # the specific ticker filling a sleeve is the evolvable universe (seed-then-evolve).
    # Read by lib.screener; absent -> no auto-discovery for that sleeve.
    sleeves: Dict[str, dict] = field(default_factory=dict)

    def book(self, name: str) -> Book:
        if name not in self.books:
            raise StrategyError(f"unknown book '{name}'")
        return self.books[name]


# --- Regimes ----------------------------------------------------------------
# Deterministic regime the operator macro reading implies. STAND_DOWN de-risks
# toward the conservative core book; DEPLOY recommends the dial-up book (only
# activated if it is also enabled); HOLD keeps the current/default core book.
REGIME_HOLD = "HOLD"
REGIME_DEPLOY = "DEPLOY"
REGIME_STAND_DOWN = "STAND_DOWN"


def _req(d: dict, key: str, where: str):
    if key not in d:
        raise StrategyError(f"strategy.yaml: missing required '{key}' in {where}")
    return d[key]


def _num(v, key: str) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        raise StrategyError(f"strategy.yaml: '{key}' must be a number, got {v!r}")


def _parse_holding(raw: object, book_name: str, allow: frozenset) -> Holding:
    # raw is yaml.safe_load output (genuinely arbitrary) — guard before treating
    # it as a mapping so a malformed entry raises a clear StrategyError.
    if not isinstance(raw, dict):
        raise StrategyError(f"strategy.yaml: holding in {book_name} must be a mapping, got {raw!r}")
    ticker = str(_req(raw, "ticker", f"{book_name} holding")).upper()
    sleeve = str(_req(raw, "sleeve", f"{book_name} holding"))
    weight = _num(_req(raw, "weight", f"{book_name}/{ticker}"), f"{book_name}/{ticker} weight")
    band = _num(raw.get("band", 0.0), f"{book_name}/{ticker} band")
    quotable = bool(raw.get("quotable", True))
    proxy = raw.get("proxy_ticker")
    if ticker not in allow:
        raise StrategyError(
            f"strategy.yaml: {book_name}/{ticker} is not in rh_tradable_confirmed"
        )
    if weight <= 0:
        raise StrategyError(f"strategy.yaml: {book_name}/{ticker} weight must be > 0")
    is_cash = sleeve == CASH_SLEEVE
    # Every engine holding needs a no-trade dead-band strictly inside its weight;
    # the cash residual is sized last and carries no band.
    if not is_cash:
        if band <= 0:
            raise StrategyError(f"strategy.yaml: {book_name}/{ticker} band must be > 0")
        if band >= weight:
            raise StrategyError(
                f"strategy.yaml: {book_name}/{ticker} band ({band}) must be < weight ({weight})"
            )
    return Holding(sleeve=sleeve, ticker=ticker, weight=weight, band=band,
                   quotable=quotable, proxy_ticker=(str(proxy) if proxy else None))


def _parse_book(name: str, raw: object, allow: frozenset) -> Book:
    if not isinstance(raw, dict):
        raise StrategyError(f"strategy.yaml: book '{name}' must be a mapping")
    holdings_raw = _req(raw, "holdings", f"book '{name}'")
    if not isinstance(holdings_raw, list) or not holdings_raw:
        raise StrategyError(f"strategy.yaml: book '{name}' needs a non-empty holdings list")
    holdings = tuple(_parse_holding(h, name, allow) for h in holdings_raw)
    tickers = [h.ticker for h in holdings]
    if len(tickers) != len(set(tickers)):
        raise StrategyError(f"strategy.yaml: book '{name}' has duplicate tickers")
    total = sum(h.weight for h in holdings)
    if abs(total - 100.0) > _WEIGHT_SUM_TOLERANCE:
        raise StrategyError(
            f"strategy.yaml: book '{name}' weights sum to {total:.2f}, expected ~100"
        )
    return Book(
        name=name,
        default=bool(raw.get("default", False)),
        enabled=bool(raw.get("enabled", True)),  # default book has no 'enabled' key -> True
        holdings=holdings,
    )


def load_strategy(path) -> StrategyConfig:
    """Parse + validate strategy.yaml. Raises StrategyError on any problem.

    Strict by design: setup (`tick.py strategy-set`) and tests want a loud
    failure on a bad book. The forgiving absent->None behavior lives in the
    lib.config hook, not here.
    """
    p = Path(path)
    if not p.exists():
        raise StrategyError(f"strategy.yaml not found at {p}")
    try:
        raw = yaml.safe_load(p.read_text()) or {}
    except yaml.YAMLError as e:
        raise StrategyError(f"strategy.yaml is not valid YAML: {e}")
    if not isinstance(raw, dict):
        raise StrategyError("strategy.yaml: top level must be a mapping")
    if int(raw.get("schema", 0)) != 1:
        raise StrategyError("strategy.yaml: unsupported or missing 'schema' (expected 1)")

    g = _req(raw, "goal", "top level")
    goal = Goal(
        target_return_pct=_num(_req(g, "target_return_pct", "goal"), "goal.target_return_pct"),
        horizon_months=int(_num(_req(g, "horizon_months", "goal"), "goal.horizon_months")),
        benchmark=str(g.get("benchmark", "cash")),
        benchmark_annual_pct=_num(g.get("benchmark_annual_pct", 0.0), "goal.benchmark_annual_pct"),
        constraint=str(g.get("constraint", "")),
    )

    m = _req(raw, "macro_thesis", "top level")
    deploy_pct = _num(_req(m, "deploy_trigger_pce_pct", "macro_thesis"), "deploy_trigger_pce_pct")
    standdown_pct = _num(_req(m, "standdown_trigger_pce_pct", "macro_thesis"), "standdown_trigger_pce_pct")
    if deploy_pct >= standdown_pct:
        raise StrategyError(
            f"strategy.yaml: deploy_trigger_pce_pct ({deploy_pct}) must be < "
            f"standdown_trigger_pce_pct ({standdown_pct})"
        )
    macro = MacroThesis(
        version=str(m.get("version", "")),
        summary=str(m.get("summary", "")),
        catalysts_to_watch=tuple(str(c) for c in (m.get("catalysts_to_watch") or [])),
        deploy_trigger_pce_pct=deploy_pct,
        standdown_trigger_pce_pct=standdown_pct,
        standdown_on_hike=bool(m.get("standdown_on_hike", True)),
        correlation_note=str(m.get("correlation_note", "")),
    )

    allow = frozenset(str(t).upper() for t in (raw.get("rh_tradable_confirmed") or []))
    if not allow:
        raise StrategyError("strategy.yaml: rh_tradable_confirmed must be non-empty")

    books_raw = _req(raw, "books", "top level")
    if not isinstance(books_raw, dict) or not books_raw:
        raise StrategyError("strategy.yaml: 'books' must be a non-empty mapping")
    books = {name: _parse_book(name, braw, allow) for name, braw in books_raw.items()}
    defaults = [name for name, b in books.items() if b.default]
    if len(defaults) != 1:
        raise StrategyError(
            f"strategy.yaml: exactly one book must have default: true (found {len(defaults)})"
        )
    default_book = defaults[0]
    if not books[default_book].enabled:
        raise StrategyError(f"strategy.yaml: the default book '{default_book}' must be enabled")

    lr = raw.get("learning") or {}
    learning = LearningConfig(
        underperf_window=int(_num(lr.get("underperf_window", 8), "learning.underperf_window")),
        underperf_hit_floor=_num(lr.get("underperf_hit_floor", 0.40), "learning.underperf_hit_floor"),
        underperf_mean_floor=_num(lr.get("underperf_mean_floor", 0.0), "learning.underperf_mean_floor"),
        min_resolved_n=int(_num(lr.get("min_resolved_n", 5), "learning.min_resolved_n")),
        sleeve_review_min_n=int(_num(lr.get("sleeve_review_min_n", 6), "learning.sleeve_review_min_n")),
        derisk_on_standdown=bool(lr.get("derisk_on_standdown", True)),
        auto_apply_derisk=bool(lr.get("auto_apply_derisk", False)),
        auto_apply_universe_changes=bool(lr.get("auto_apply_universe_changes", False)),
        goal_gap_derisk_pct=_num(lr.get("goal_gap_derisk_pct", -5.0), "learning.goal_gap_derisk_pct"),
        proposal_expiry_days=int(_num(lr.get("proposal_expiry_days", 5), "learning.proposal_expiry_days")),
    )

    cons = raw.get("consistency") or {}
    consistency = ConsistencyConfig(
        regime_confirm_n=int(_num(cons.get("regime_confirm_n", 2), "consistency.regime_confirm_n")),
        regime_min_dwell_days=int(_num(cons.get("regime_min_dwell_days", 5), "consistency.regime_min_dwell_days")),
        regime_deadband_pce=_num(cons.get("regime_deadband_pce", REGIME_DEADBAND_PCE), "consistency.regime_deadband_pce"),
        proposal_cooldown_days=int(_num(cons.get("proposal_cooldown_days", 5), "consistency.proposal_cooldown_days")),
        universe_confirm_days=int(_num(cons.get("universe_confirm_days", 2), "consistency.universe_confirm_days")),
        min_strategy_set_interval_days=int(_num(cons.get("min_strategy_set_interval_days", 0),
                                                "consistency.min_strategy_set_interval_days")),
    )

    # Allocation policy (Q1). Coerce the known scalar knobs to float at LOAD time so a
    # bad value fails loudly here (strict load), not mid-tick inside the allocator.
    # default_conviction (a nested {rating: number} dict) and any other key pass through.
    rp_raw = raw.get("risk_policy") or {}
    if not isinstance(rp_raw, dict):
        raise StrategyError("strategy.yaml: risk_policy must be a mapping")
    _RP_NUM = ("per_name_max_pct", "sleeve_max_pct", "cash_floor_pct", "conviction_curve_k",
               "smoothing_alpha", "uncertainty_damp", "regime_min_factor", "min_weight_floor_pct")
    risk_policy: Dict[str, object] = {}
    for rk, rv in rp_raw.items():
        risk_policy[rk] = _num(rv, f"risk_policy.{rk}") if rk in _RP_NUM else rv

    # Per-sleeve screen criteria (Q3). Optional; the screener interprets each `screen` block.
    sleeves_raw = raw.get("sleeves") or {}
    if not isinstance(sleeves_raw, dict):
        raise StrategyError("strategy.yaml: sleeves must be a mapping")
    sleeves = {str(name): (spec if isinstance(spec, dict) else {})
               for name, spec in sleeves_raw.items()}

    return StrategyConfig(
        goal=goal,
        macro_thesis=macro,
        rh_tradable_confirmed=allow,
        books=books,
        learning=learning,
        default_book=default_book,
        consistency=consistency,
        risk_policy=risk_policy,
        sleeves=sleeves,
    )


def regime_label(strategy_cfg: StrategyConfig, macro_reading: Optional[dict]) -> str:
    """The deterministic regime an operator macro reading implies (read-only context).

    macro_reading: {"core_pce_pct": float|None, "fed_hike": bool}. A missing or
    empty reading is conservative HOLD (never DEPLOY without a real signal).
    """
    if not macro_reading:
        return REGIME_HOLD
    m = strategy_cfg.macro_thesis
    pce = macro_reading.get("core_pce_pct")
    hike = bool(macro_reading.get("fed_hike", False))
    if (m.standdown_on_hike and hike) or (pce is not None and pce >= m.standdown_trigger_pce_pct):
        return REGIME_STAND_DOWN
    if pce is not None and pce <= m.deploy_trigger_pce_pct:
        return REGIME_DEPLOY
    return REGIME_HOLD


def book_for_regime(strategy_cfg: StrategyConfig, regime: str) -> Tuple[str, str]:
    """Map an (already-decided) regime to a book. The regime→book half of
    select_active_book, factored out so the CONFIRMED effective regime (Component C)
    can drive the recommendation instead of the raw per-tick reading."""
    regime = normalize_regime(regime)
    default = strategy_cfg.default_book
    if regime == REGIME_STAND_DOWN:
        return default, "STAND_DOWN: core PCE at/above stand-down trigger or a Fed hike -> hold the conservative core book"
    if regime == REGIME_DEPLOY:
        dial_up = next(
            (name for name, b in strategy_cfg.books.items()
             if not b.default and b.enabled),
            None,
        )
        if dial_up is not None:
            return dial_up, "DEPLOY: core PCE at/below deploy trigger and dial-up enabled -> rotate to the dial-up book"
        return default, "DEPLOY: deploy reading, but dial-up is not enabled (fail-safe) -> stay on core; recommend enabling dial-up"
    return default, "HOLD: no deploy/stand-down trigger -> default core book"


def select_active_book(
    strategy_cfg: StrategyConfig, macro_reading: Optional[dict]
) -> Tuple[str, str]:
    """Deterministically pick the active book from the RAW macro reading (no
    confirmation). Kept for back-compat + the construct fallback before any thesis
    state exists; the live path now prefers the CONFIRMED effective regime via
    book_for_regime (Component C)."""
    return book_for_regime(strategy_cfg, regime_label(strategy_cfg, macro_reading))


# --- regime hysteresis + confirmation + min-dwell (Component C) --------------
# The raw regime (above) is a stateless function of one operator-typed PCE number.
# These add the missing hysteresis so the strategic posture changes INFREQUENTLY and
# only on a CONFIRMED signal: a dead-band around the triggers (boundary wobble reads
# as HOLD), N consecutive confirmations before a flip, and a min-dwell lock after one.
# Pure + unit-tested; tick.py persists the returned state into thesis_state.
# (REGIME_DEADBAND_PCE is defined near the top so ConsistencyConfig can default to it.)


def regime_label_banded(strategy_cfg: StrategyConfig, macro_reading: Optional[dict],
                        deadband: float = REGIME_DEADBAND_PCE) -> str:
    """``regime_label`` with a dead-band: a reading within ``deadband`` of a trigger is
    ambiguous and reads as HOLD, so boundary wobble (3.49 vs 3.51 around 3.5) can't flip
    the regime tick-to-tick. A Fed hike is categorical (no band). Conservative default."""
    if not macro_reading:
        return REGIME_HOLD
    m = strategy_cfg.macro_thesis
    pce = macro_reading.get("core_pce_pct")
    hike = bool(macro_reading.get("fed_hike", False))
    if m.standdown_on_hike and hike:
        return REGIME_STAND_DOWN
    if pce is not None:
        if pce >= m.standdown_trigger_pce_pct + deadband:
            return REGIME_STAND_DOWN
        if pce <= m.deploy_trigger_pce_pct - deadband:
            return REGIME_DEPLOY
    return REGIME_HOLD


def normalize_regime(r: Optional[str]) -> str:
    """Map any stored/legacy regime spelling to a canonical REGIME_* constant.

    thesis_state historically stored lowercase words (neutral|deploy|standdown); the
    layer's constants are HOLD|DEPLOY|STAND_DOWN. This bridges both so the confirmation
    state machine works in ONE vocabulary regardless of what's on disk."""
    u = str(r or "").strip().upper()
    return {
        "": REGIME_HOLD, "NEUTRAL": REGIME_HOLD, "HOLD": REGIME_HOLD,
        "DEPLOY": REGIME_DEPLOY,
        "STAND_DOWN": REGIME_STAND_DOWN, "STANDDOWN": REGIME_STAND_DOWN,
    }.get(u, REGIME_HOLD)


def regime_scalar(regime: Optional[str], *, min_factor: float = 0.5) -> float:
    """Deployment scalar the conviction allocator applies (lib.allocate regime_scalar).

    STAND_DOWN derisks the engine toward cash (returns ``min_factor``); DEPLOY/HOLD
    deploy the full base. The allocator clamps to [regime_min_factor, 1.0] itself, so
    this only needs to signal the cut. Fed from the CONFIRMED regime in thesis_state,
    never a single raw macro reading, so one noisy print can't shrink the book."""
    return float(min_factor) if normalize_regime(regime) == REGIME_STAND_DOWN else 1.0


def _regime_days_between(since: Optional[str], today: Optional[str]) -> Optional[int]:
    """Whole days between two ISO dates/timestamps (date portion only). None on bad input."""
    from datetime import date
    try:
        d0 = date.fromisoformat(str(since)[:10])
        d1 = date.fromisoformat(str(today)[:10])
    except (TypeError, ValueError):
        return None
    return (d1 - d0).days


def regime_with_confirmation(prior_state: Optional[dict], raw_regime: str, *,
                             confirm_n: int, min_dwell_days: int, today: str) -> dict:
    """The confirmed-regime state machine (memory-grounded, NOT a calendar rule).

    prior_state: the thesis_state row (or None) carrying the EFFECTIVE ``regime`` plus
    the in-flight ``pending_regime``/``confirm_count``/``regime_since``. raw_regime: this
    tick's banded reading. Returns the next state:
      {effective_regime, pending_regime, confirm_count, regime_since, changed, reason}.

    Rules:
      * raw agrees with effective       -> clear any pending; no change.
      * raw differs                     -> accumulate confirmations for that candidate;
        once confirm_count >= confirm_n AND the min-dwell since the last change has
        elapsed, the effective regime flips (stamping regime_since=today). Inside the
        dwell window the flip is LOCKED but the confirmation is retained, so it commits
        the moment the lock lifts. A different candidate resets the counter to 1.

    Fail-safe: a missing/HOLD reading never accumulates AWAY from a real regime toward
    HOLD unless HOLD itself is confirmed; it never jumps straight to DEPLOY.
    """
    confirm_n = max(1, int(confirm_n))
    effective = normalize_regime((prior_state or {}).get("regime"))
    raw = normalize_regime(raw_regime)
    prior_pending = (prior_state or {}).get("pending_regime")
    pending = normalize_regime(prior_pending) if prior_pending else None
    count = int((prior_state or {}).get("confirm_count") or 0)
    regime_since = (prior_state or {}).get("regime_since")
    prior_pending_since = (prior_state or {}).get("pending_since")

    def result(eff, pend, cnt, since, changed, reason, pend_since=None):
        return {"effective_regime": eff, "pending_regime": pend, "confirm_count": cnt,
                "regime_since": since, "pending_since": pend_since,
                "changed": changed, "reason": reason}

    if raw == effective:
        # The reading agrees with where we are — drop any half-formed candidate.
        return result(effective, None, 0, regime_since, False, "stable")

    # A candidate that differs from the effective regime: accumulate confirmations.
    if pending == raw:
        count += 1
        pending_since = prior_pending_since or today
    else:
        pending, count, pending_since = raw, 1, today

    if count < confirm_n:
        return result(effective, pending, count, regime_since,
                      False, f"confirming {count}/{confirm_n} -> {pending}", pending_since)

    # Enough confirmations — but honor the min-dwell lock on the current regime. Clamp the
    # elapsed span to >= 0 (a None/unparseable or FUTURE regime_since from clock skew /
    # a manual edit reads as 0 days elapsed => LOCKED, the conservative choice — never
    # pins the regime forever and never skips the lock to flip early).
    days = _regime_days_between(regime_since, today)
    days = days if (days is not None and days >= 0) else 0
    if regime_since and min_dwell_days > 0 and days < min_dwell_days:
        return result(effective, pending, count, regime_since, False,
                      f"confirmed {pending} but dwell-locked ({days}/{min_dwell_days}d)", pending_since)

    return result(pending, None, 0, today, True, f"confirmed -> {pending}")


def active_targets_for_book(strategy_cfg: StrategyConfig, book_name: str) -> List[Holding]:
    """The holdings of a book (the raw target weights, before drift/quote filtering)."""
    return list(strategy_cfg.book(book_name).holdings)


def sleeve_thesis(strategy_cfg: StrategyConfig, ticker: str) -> Optional[dict]:
    """Sleeve label + macro correlation note for a ticker (read-only context).

    Looks across all books for the ticker's sleeve. Returns None when the ticker
    is not in any book (analyze.py then injects no target block for it).
    """
    t = ticker.upper()
    for b in strategy_cfg.books.values():
        for h in b.holdings:
            if h.ticker == t:
                return {
                    "sleeve": h.sleeve,
                    "correlation_note": strategy_cfg.macro_thesis.correlation_note,
                    "thesis_version": strategy_cfg.macro_thesis.version,
                }
    return None
