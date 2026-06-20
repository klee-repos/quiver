"""Pydantic schemas used by agents that produce structured output.

The framework's primary artifact is still prose: each agent's natural-language
reasoning is what users read in the saved markdown reports and what the
downstream agents read as context.  Structured output is layered onto the
three decision-making agents (Research Manager, Trader, Portfolio Manager)
so that:

- Their outputs follow consistent section headers across runs and providers
- Each provider's native structured-output mode is used (json_schema for
  OpenAI/xAI, response_schema for Gemini, tool-use for Anthropic)
- Schema field descriptions become the model's output instructions, freeing
  the prompt body to focus on context and the rating-scale guidance
- A render helper turns the parsed Pydantic instance back into the same
  markdown shape the rest of the system already consumes, so display,
  memory log, and saved reports keep working unchanged
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Shared rating types
# ---------------------------------------------------------------------------


class PortfolioRating(str, Enum):
    """5-tier rating used by the Research Manager and Portfolio Manager."""

    BUY = "Buy"
    OVERWEIGHT = "Overweight"
    HOLD = "Hold"
    UNDERWEIGHT = "Underweight"
    SELL = "Sell"


class TraderAction(str, Enum):
    """3-tier transaction direction used by the Trader.

    The Trader's job is to translate the Research Manager's investment plan
    into a concrete transaction proposal: should the desk execute a Buy, a
    Sell, or sit on Hold this round.  Position sizing and the nuanced
    Overweight / Underweight calls happen later at the Portfolio Manager.
    """

    BUY = "Buy"
    HOLD = "Hold"
    SELL = "Sell"


# ---------------------------------------------------------------------------
# Research Manager
# ---------------------------------------------------------------------------


class ResearchPlan(BaseModel):
    """Structured investment plan produced by the Research Manager.

    Hand-off to the Trader: the recommendation pins the directional view,
    the rationale captures which side of the bull/bear debate carried the
    argument, and the strategic actions translate that into concrete
    instructions the trader can execute against.
    """

    recommendation: PortfolioRating = Field(
        description=(
            "The investment recommendation. Exactly one of Buy / Overweight / "
            "Hold / Underweight / Sell. Reserve Hold for situations where the "
            "evidence on both sides is genuinely balanced; otherwise commit to "
            "the side with the stronger arguments."
        ),
    )
    rationale: str = Field(
        description=(
            "Conversational summary of the key points from both sides of the "
            "debate, ending with which arguments led to the recommendation. "
            "Speak naturally, as if to a teammate."
        ),
    )
    strategic_actions: str = Field(
        description=(
            "Concrete steps for the trader to implement the recommendation, "
            "including position sizing guidance consistent with the rating."
        ),
    )


def render_research_plan(plan: ResearchPlan) -> str:
    """Render a ResearchPlan to markdown for storage and the trader's prompt context."""
    return "\n".join([
        f"**Recommendation**: {plan.recommendation.value}",
        "",
        f"**Rationale**: {plan.rationale}",
        "",
        f"**Strategic Actions**: {plan.strategic_actions}",
    ])


# ---------------------------------------------------------------------------
# Trader
# ---------------------------------------------------------------------------


class TraderProposal(BaseModel):
    """Structured transaction proposal produced by the Trader.

    The trader reads the Research Manager's investment plan and the analyst
    reports, then turns them into a concrete transaction: what action to
    take, the reasoning that justifies it, and the practical levels for
    entry, stop-loss, and sizing.
    """

    action: TraderAction = Field(
        description="The transaction direction. Exactly one of Buy / Hold / Sell.",
    )
    reasoning: str = Field(
        description=(
            "The case for this action, anchored in the analysts' reports and "
            "the research plan. Two to four sentences."
        ),
    )
    entry_price: Optional[float] = Field(
        default=None,
        description="Optional entry price target in the instrument's quote currency.",
    )
    stop_loss: Optional[float] = Field(
        default=None,
        description="Optional stop-loss price in the instrument's quote currency.",
    )
    position_sizing: Optional[str] = Field(
        default=None,
        description="Optional sizing guidance, e.g. '5% of portfolio'.",
    )
    position_pct: Optional[float] = Field(
        default=None,
        description=(
            "Optional structured sizing: the percent of total portfolio equity to "
            "allocate to this position, as a plain number (e.g. 5 means 5%). Prefer "
            "filling this over (or in addition to) the prose position_sizing — the "
            "execution layer reads this number directly and still clamps it to its caps."
        ),
    )
    strategy_basis: Optional[str] = Field(
        default=None,
        description=(
            "The named strategy/thesis this call rests on, as a short stable tag (e.g. "
            "'momentum_breakout', 'mean_reversion_band', 'long_term_secular', "
            "'thesis_intact_hold', 'rate_cut_beneficiary'). Keep it the SAME across runs "
            "for the same ticker as long as the thesis holds — it is how the system keeps "
            "your day-to-day decisions self-consistent. Only change it when a real new "
            "catalyst changes the thesis (and then name that catalyst below)."
        ),
    )
    catalyst: Optional[str] = Field(
        default=None,
        description=(
            "REQUIRED ONLY WHEN YOU REVERSE your prior stance on this ticker (you were "
            "accumulating and now want to sell, or you exited and now want to buy back): "
            "name the SPECIFIC, data-backed NEW catalyst that justifies the reversal — an "
            "earnings miss, a broken support level, a macro-regime shift, a stop breached. "
            "Absent a genuine named catalyst, DO NOT reverse: hold your prior stance. A "
            "reversal with no catalyst and no triggered stop/target is treated as "
            "inconsistent and suppressed by the execution layer."
        ),
    )
    target_price: Optional[float] = Field(
        default=None,
        description=(
            "Optional take-profit target price (quote currency). If the live price reaches "
            "it, a later sell is treated as the recorded plan executing — a consistent "
            "exit, not a flip — so set it when your thesis has a clear profit objective."
        ),
    )


def render_trader_proposal(proposal: TraderProposal) -> str:
    """Render a TraderProposal to markdown.

    The trailing ``FINAL TRANSACTION PROPOSAL: **BUY/HOLD/SELL**`` line is
    preserved for backward compatibility with the analyst stop-signal text
    and any external code that greps for it.
    """
    parts = [
        f"**Action**: {proposal.action.value}",
        "",
        f"**Reasoning**: {proposal.reasoning}",
    ]
    if proposal.entry_price is not None:
        parts.extend(["", f"**Entry Price**: {proposal.entry_price}"])
    if proposal.stop_loss is not None:
        parts.extend(["", f"**Stop Loss**: {proposal.stop_loss}"])
    if proposal.position_sizing:
        parts.extend(["", f"**Position Sizing**: {proposal.position_sizing}"])
    if proposal.position_pct is not None:
        parts.extend(["", f"**Position Pct**: {proposal.position_pct}"])
    if proposal.strategy_basis:
        parts.extend(["", f"**Strategy Basis**: {proposal.strategy_basis}"])
    if proposal.target_price is not None:
        parts.extend(["", f"**Target Price**: {proposal.target_price}"])
    if proposal.catalyst:
        parts.extend(["", f"**Catalyst**: {proposal.catalyst}"])
    parts.extend([
        "",
        f"FINAL TRANSACTION PROPOSAL: **{proposal.action.value.upper()}**",
    ])
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Portfolio Manager
# ---------------------------------------------------------------------------


class PortfolioDecision(BaseModel):
    """Structured output produced by the Portfolio Manager.

    The model fills every field as part of its primary LLM call; no separate
    extraction pass is required. Field descriptions double as the model's
    output instructions, so the prompt body only needs to convey context and
    the rating-scale guidance.
    """

    rating: PortfolioRating = Field(
        description=(
            "The final position rating. Exactly one of Buy / Overweight / Hold / "
            "Underweight / Sell, picked based on the analysts' debate."
        ),
    )
    conviction: Optional[float] = Field(
        default=None,
        description=(
            "Your numeric conviction in this call, 0-100 (a plain number). This is the "
            "BINDING strength the execution layer uses to size the position: a higher "
            "number deploys more capital to this name, a lower number less. Ground it in "
            "how strongly the analysts AGREE, how decisive the bull/bear debate was, and "
            "how solid the underlying data is. Guide: 80-100 = high-conviction, evidence "
            "all points one way; 50-65 = a real but contested lean; 30-45 = weak/marginal; "
            "0-25 = avoid/exit. A Hold should carry your conviction in HOLDING the current "
            "position. Do not inflate — a marginal Buy must score lower than a strong Buy."
        ),
    )
    uncertainty: Optional[float] = Field(
        default=None,
        description=(
            "How uncertain you are about this call, 0-100 (a plain number): 0 = the data "
            "is clean and the analysts agree; 100 = thin/conflicting data or a genuine "
            "toss-up. The execution layer damps sizing when uncertainty is high, so report "
            "it honestly rather than projecting false confidence."
        ),
    )
    executive_summary: str = Field(
        description=(
            "A concise action plan covering entry strategy, position sizing, "
            "key risk levels, and time horizon. Two to four sentences."
        ),
    )
    investment_thesis: str = Field(
        description=(
            "Detailed reasoning anchored in specific evidence from the analysts' "
            "debate. If prior lessons are referenced in the prompt context, "
            "incorporate them; otherwise rely solely on the current analysis."
        ),
    )
    price_target: Optional[float] = Field(
        default=None,
        description="Optional target price in the instrument's quote currency.",
    )
    time_horizon: Optional[str] = Field(
        default=None,
        description="Optional recommended holding period, e.g. '3-6 months'.",
    )
    next_review_hours: Optional[float] = Field(
        default=None,
        description=(
            "Optional: in how many HOURS should this ticker be re-analyzed? Use a "
            "smaller value for fast-moving or high-conviction setups that need close "
            "watching, a larger value for stable theses. This is a re-check cadence "
            "(distinct from the holding horizon); the system clamps it to a safe range."
        ),
    )


def render_pm_decision(decision: PortfolioDecision) -> str:
    """Render a PortfolioDecision back to the markdown shape the rest of the system expects.

    Memory log, CLI display, and saved report files all read this markdown,
    so the rendered output preserves the exact section headers (``**Rating**``,
    ``**Executive Summary**``, ``**Investment Thesis**``) that downstream
    parsers and the report writers already handle.
    """
    parts = [
        f"**Rating**: {decision.rating.value}",
    ]
    if decision.conviction is not None:
        parts.append(f"**Conviction**: {decision.conviction}")
    if decision.uncertainty is not None:
        parts.append(f"**Uncertainty**: {decision.uncertainty}")
    parts += [
        "",
        f"**Executive Summary**: {decision.executive_summary}",
        "",
        f"**Investment Thesis**: {decision.investment_thesis}",
    ]
    if decision.price_target is not None:
        parts.extend(["", f"**Price Target**: {decision.price_target}"])
    if decision.time_horizon:
        parts.extend(["", f"**Time Horizon**: {decision.time_horizon}"])
    if decision.next_review_hours is not None:
        parts.extend(["", f"**Next Review Hours**: {decision.next_review_hours}"])
    return "\n".join(parts)
