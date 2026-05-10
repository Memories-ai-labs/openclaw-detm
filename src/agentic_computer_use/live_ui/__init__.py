"""Live UI vision/control — backend dispatcher.

Backends, picked at runtime by ACU_LIVE_UI_BACKEND:

  - bash (default): single bash(command) tool over OpenRouter chat-completions.
                    Production model: openai/gpt-5.4. See live_ui/bash_backend.py.
  - supervised:     legacy Gemini-Flash supervisor + UI-TARS-1.5-7B grounder.
                    Kept for benchmark reproducibility.

  v2 native CUA backends (use the model's lab-tuned computer-use API):
  - openai_cua:     OpenAI Responses API + tools=[{type:"computer"}] (gpt-5.4)
  - anthropic_cua:  Anthropic Messages API + computer_20251124 + computer-use-2025-11-24 beta
                    (claude-opus-4-7 / claude-sonnet-4-6)
  - gemini_cua:     google-genai SDK ComputerUse(env=BROWSER) via AI Studio
                    (gemini-2.5-computer-use-preview-10-2025)
  - kimi_fc:        Kimi K2.5 with standard function-calling computer schema, OpenRouter
"""
from .. import config
from .base import LiveUIProvider


def get_provider() -> LiveUIProvider:
    """Return the live UI provider for the configured backend."""
    backend = (config.LIVE_UI_BACKEND or "bash").lower()

    if backend == "bash":
        from .bash_backend import openrouter_bash_provider
        return openrouter_bash_provider()

    if backend == "supervised":
        from .openrouter import OpenRouterVLMProvider
        return OpenRouterVLMProvider()

    if backend == "openai_cua":
        from .openai_cua_backend import openai_cua_provider
        return openai_cua_provider()

    if backend == "anthropic_cua":
        from .anthropic_cua_backend import anthropic_cua_provider
        return anthropic_cua_provider()

    if backend == "gemini_cua":
        from .gemini_cua_backend import gemini_cua_provider
        return gemini_cua_provider()

    if backend == "kimi_fc":
        from .kimi_fc_backend import kimi_fc_provider
        return kimi_fc_provider()

    raise ValueError(
        f"Unknown ACU_LIVE_UI_BACKEND={backend!r}. "
        f"Valid: 'bash' (default), 'supervised', "
        f"'openai_cua', 'anthropic_cua', 'gemini_cua', 'kimi_fc'."
    )
