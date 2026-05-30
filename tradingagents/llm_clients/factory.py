from typing import Optional

from .base_client import BaseLLMClient

# Providers that use the OpenAI-compatible chat completions API
_OPENAI_COMPATIBLE = (
    "openai", "xai", "deepseek",
    "qwen", "qwen-cn",
    "glm", "glm-cn",
    "minimax", "minimax-cn",
    "ollama", "openrouter",
)


def create_llm_client(
    provider: str,
    model: str,
    base_url: Optional[str] = None,
    **kwargs,
) -> BaseLLMClient:
    """Create an LLM client for the specified provider.

    Provider modules are imported lazily so that simply importing this
    factory (e.g. during test collection) does not pull in heavy LLM SDKs
    or fail when their API keys are absent.

    Args:
        provider: LLM provider name
        model: Model name/identifier
        base_url: Optional base URL for API endpoint
        **kwargs: Additional provider-specific arguments

    Returns:
        Configured BaseLLMClient instance

    Raises:
        ValueError: If provider is not supported
    """
    provider_lower = provider.lower()

    if provider_lower in _OPENAI_COMPATIBLE:
        from .openai_client import OpenAIClient
        return OpenAIClient(model, base_url, provider=provider_lower, **kwargs)

    # Quiver fork is DeepSeek-only (an OpenAI-compatible provider). The
    # Anthropic / Google / Azure clients were removed when de-vendoring; if you
    # ever need them, restore from upstream (see tradingagents/UPSTREAM.md).
    raise ValueError(
        f"Unsupported LLM provider: {provider}. This fork supports only the "
        f"OpenAI-compatible providers: {', '.join(_OPENAI_COMPATIBLE)}."
    )
