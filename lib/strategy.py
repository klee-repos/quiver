"""Load and validate strategy.yaml: the seed input of ``tick.py desk-seed`` (plan §16.1).

It parses the goal, the macro thesis, the books, and the two ``risk_policy`` start values
(``per_name_max_pct``, ``cash_floor_pct``). It does not size, trade, or read limits.
After the cutover the ledger holds the book; only a confirmed proposal changes it.

Two failure postures, on purpose:
  * load_strategy() is STRICT (validate-or-raise): ``desk-seed`` and the tests want a loud
    failure on a bad book.
  * the lib.config hook is FORGIVING: it leaves cfg.strategy=None on any failure.
The keys of retired features (``learning``, ``consistency``, ``sleeves``) are ignored.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Tuple

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
class StrategyConfig:
    goal: Goal
    macro_thesis: MacroThesis
    rh_tradable_confirmed: frozenset
    books: Dict[str, Book]
    default_book: str
    # risk_policy: the per_name_max_pct and cash_floor_pct start values of desk_settings.
    risk_policy: Dict[str, object] = field(default_factory=dict)

    def book(self, name: str) -> Book:
        if name not in self.books:
            raise StrategyError(f"unknown book '{name}'")
        return self.books[name]




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

    # The two risk_policy keys the desk reads (§10.3). Every other key of an older file
    # (learning, consistency, sleeves, the PTJ knobs) is ignored.
    rp_raw = raw.get("risk_policy") or {}
    if not isinstance(rp_raw, dict):
        raise StrategyError("strategy.yaml: risk_policy must be a mapping")
    risk_policy: Dict[str, object] = {}
    for rk in ("per_name_max_pct", "cash_floor_pct"):
        if rp_raw.get(rk) is not None:
            risk_policy[rk] = _num(rp_raw[rk], f"risk_policy.{rk}")

    return StrategyConfig(
        goal=goal,
        macro_thesis=macro,
        rh_tradable_confirmed=allow,
        books=books,
        default_book=default_book,
        risk_policy=risk_policy,
    )
