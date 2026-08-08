"""(F8) Legacy TradingAgents framework config — REMOVED.

The tradingagents/ framework was deleted in the EVE migration; the brain is now
quiver_eve/run/decide.mjs (GLM-5.2 via OpenRouter, wired in quiver_eve/agent/
agent.ts). This module is kept as an empty stub so `import lib.ds_config`
(used by deploy/runner/healthcheck.py to detect a broken brain load) still
succeeds without pulling in the deleted tradingagents.default_config.

The model/provider selection that lived here now lives in the EVE brain:
quiver_eve/run/decide.mjs + agent.ts read OPENROUTER_API_KEY + the
QUIVER_REASONER_MODEL / QUIVER_CHAT_MODEL env slugs. (The legacy glm:/deepseek:
block in config.yaml is parsed by lib/config.py for back-compat only; the EVE
brain does not read it.)
"""

from __future__ import annotations
