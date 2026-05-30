# Provenance

This `tradingagents/` package is a **vendored-and-owned fork** of the upstream
multi-agent trading framework. It was lifted out of a gitignored `vendor/` clone into
the tracked repo so Quiver can modify it freely.

- **Upstream:** https://github.com/TauricResearch/TradingAgents
- **Forked at commit:** `61522e103e61601c553b4544abcd53fa7ebf9f1d`
  — _"fix(llm): skip Anthropic effort kwarg on non-supporting models (#831)"_
- **De-vendored:** 2026-05-30

## What we changed when adopting it

- Pruned non-runtime weight: `cli/`, `tests/`, `assets/`, `scripts/`, the nested upstream
  `.git/`, and `*.egg-info/`.
- Trimmed `llm_clients/` to the DeepSeek / OpenAI-compatible path only; removed the
  Anthropic, Google, and Azure provider clients and their `factory.py` dispatch branches
  (DeepSeek routes through `openai_client` as an OpenAI-compatible provider).
- Slimmed dependencies in the repo-root `pyproject.toml` accordingly.

This is no longer tracked against upstream; treat it as first-class Quiver source.
To compare against upstream for reference, diff against the commit above.
