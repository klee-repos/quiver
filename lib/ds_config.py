"""Build the TradingAgents config dict wired for GLM 5.2 (z.ai).

Single source of truth for model selection + endpoint. Model IDs come from
``config.yaml`` (so swapping models never touches code); the API key is read
from the environment (``GLM_API_KEY``, loaded from ``.env`` by the caller).

Design rule (verified against the framework source):
- ``quick_think_llm`` is used by the market/news/fundamentals analysts, which
  fetch data via tool-calling -> it MUST support function calling. GLM 5.2 is
  OpenAI-compatible with full tool calling.
- ``deep_think_llm`` is used by the researchers, trader, risk debators, and
  judges. Both roles use GLM 5.2 with thinking mode enabled at max reasoning
  effort (wired in tradingagents.llm_clients: GLMChatOpenAI + the glm-5.2
  capability row), so judgment gets the deepest reasoning available.
- ``backend_url`` is left None on purpose: the framework's OpenAIClient resolves
  the GLM provider default (https://api.z.ai/api/paas/v4/) and langchain appends
  the path. Forcing a "/v1" suffix here risks a doubled path.
"""

from __future__ import annotations

import os

from tradingagents.default_config import DEFAULT_CONFIG


def build_glm_config(chat_model: str, reasoner_model: str, *, state_dir: str) -> dict:
    """Return a TradingAgents config dict pointed at GLM (z.ai) and our state dir."""
    cfg = dict(DEFAULT_CONFIG)  # shallow copy is enough; we only reassign top-level keys
    cfg["llm_provider"] = "glm"
    cfg["backend_url"] = None  # let the provider default apply (see module docstring)
    cfg["quick_think_llm"] = chat_model
    cfg["deep_think_llm"] = reasoner_model

    # Keep all writable artifacts inside our own state/ tree, not ~/.tradingagents.
    cfg["results_dir"] = os.path.join(state_dir, "results")
    cfg["data_cache_dir"] = os.path.join(state_dir, "cache")
    cfg["memory_log_path"] = os.path.join(state_dir, "memory", "trading_memory.md")
    return cfg
