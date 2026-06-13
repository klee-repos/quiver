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

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

# The sleeve label that marks the cash ballast. The cash holding is the residual
# (never churned like an engine name) and carries no rebalance band.
CASH_SLEEVE = "Cash"

# Weights are author-entered percentages; allow a small rounding slack so a book
# that sums to 99.7 or 100.2 still validates while a real typo (sums to 90) does not.
_WEIGHT_SUM_TOLERANCE = 0.5


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
class StrategyConfig:
    goal: Goal
    macro_thesis: MacroThesis
    rh_tradable_confirmed: frozenset
    books: Dict[str, Book]
    learning: LearningConfig
    default_book: str

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

    return StrategyConfig(
        goal=goal,
        macro_thesis=macro,
        rh_tradable_confirmed=allow,
        books=books,
        learning=learning,
        default_book=default_book,
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


def select_active_book(
    strategy_cfg: StrategyConfig, macro_reading: Optional[dict]
) -> Tuple[str, str]:
    """Deterministically pick the active book from the macro reading.

    STAND_DOWN -> the conservative default (core) book. DEPLOY -> the dial-up
    book ONLY if it both exists and is enabled (fail-safe: a DEPLOY reading with
    dial-up OFF stays on core and the digest surfaces the recommendation). HOLD
    -> the default book. Returns (book_name, human-readable reason).
    """
    regime = regime_label(strategy_cfg, macro_reading)
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
