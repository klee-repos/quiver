"""Build the TradingAgents config dict, with per-role LLM providers.

Single source of truth for model selection + endpoint. Model IDs AND providers
come from ``config.yaml`` (so swapping models/providers never touches code); each
provider's API key is read from the environment (``GLM_API_KEY`` /
``DEEPSEEK_API_KEY``, loaded from ``.env`` by the caller).

Design rule (verified against the framework source):
- ``quick_think_llm`` is used by the market/news/fundamentals analysts, which
  fetch data via tool-calling -> it MUST support function calling. The live setup
  runs DeepSeek V4 Flash here (the smaller "manages the agents" role).
- ``deep_think_llm`` is used by the researchers, trader, risk debators, and
  judges. The live setup runs GLM 5.2 with thinking mode enabled at high reasoning
  effort (one step below GLM's max; wired in tradingagents.llm_clients:
  GLMChatOpenAI + the glm-5.2 capability row).
- Each role carries its own provider (``quick_think_provider`` /
  ``deep_think_provider``); ``llm_provider`` is set to the deep provider as the
  single-provider default that ``trading_graph._get_provider_kwargs`` reads.
- ``backend_url`` is left None on purpose: the framework's OpenAIClient resolves
  each provider's default endpoint (z.ai for glm, api.deepseek.com for deepseek)
  and langchain appends the path. Forcing a "/v1" suffix here risks a doubled path.
"""

from __future__ import annotations

import os

from tradingagents.default_config import DEFAULT_CONFIG


def build_agents_config(
    chat_model: str,
    chat_provider: str,
    reasoner_model: str,
    reasoner_provider: str,
    *,
    state_dir: str,
) -> dict:
    """Return a TradingAgents config dict with per-role providers + our state dir.

    The quick (chat / analyst tool-calling) role and the deep (reasoner / debates)
    role each carry their own provider, so they may differ (e.g. deepseek + glm).
    ``llm_provider`` is set to the deep/reasoner provider as the single-provider
    default consumed by ``trading_graph._get_provider_kwargs`` and any back-compat
    reader; ``trading_graph`` routes each client off the per-role provider keys.
    ``backend_url`` stays None so each provider resolves its own default endpoint.
    """
    cfg = dict(DEFAULT_CONFIG)  # shallow copy is enough; we only reassign top-level keys
    cfg["llm_provider"] = reasoner_provider  # deep is the sensible single-provider default
    cfg["deep_think_provider"] = reasoner_provider
    cfg["quick_think_provider"] = chat_provider
    cfg["backend_url"] = None  # let each provider default apply (see module docstring)
    cfg["quick_think_llm"] = chat_model
    cfg["deep_think_llm"] = reasoner_model

    # Keep all writable artifacts inside our own state/ tree, not ~/.tradingagents.
    cfg["results_dir"] = os.path.join(state_dir, "results")
    cfg["data_cache_dir"] = os.path.join(state_dir, "cache")
    cfg["memory_log_path"] = os.path.join(state_dir, "memory", "trading_memory.md")
    return cfg


def build_glm_config(chat_model: str, reasoner_model: str, *, state_dir: str) -> dict:
    """Back-compat shim: single-provider GLM (both roles) — preserves the prior
    contract (llm_provider='glm', backend_url=None, quick/deep think models)."""
    return build_agents_config(
        chat_model, "glm", reasoner_model, "glm", state_dir=state_dir
    )
