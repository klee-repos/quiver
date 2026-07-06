"""(F8) Legacy TradingAgents framework config — REMOVED.

The tradingagents/ framework was deleted in the EVE migration; the brain is now
quiver_eve/run/decide.mjs (GLM-5.2 via OpenRouter, wired in quiver_eve/agent/
agent.ts). This module is kept as an empty stub so `import lib.ds_config`
(used by deploy/runner/healthcheck.py to detect a broken brain load) still
succeeds without pulling in the deleted tradingagents.default_config.

The model/provider selection that lived here now lives in:
  - config.yaml (glm: block -> cfg.chat_model/reasoner_model/providers)
  - quiver_eve/agent/agent.ts (the EVE model wiring reads OPENROUTER_API_KEY +
    QUIVER_REASONER_MODEL / QUIVER_CHAT_MODEL slugs).
"""

from __future__ import annotations
