"""Live UI vision/control — backend dispatcher.

Two backends, picked at runtime by ACU_LIVE_UI_BACKEND:
  - bash (default): single bash(command) tool over OpenRouter chat-completions.
                    Production model: openai/gpt-5.4. See live_ui/bash_backend.py.
  - supervised:     legacy Gemini-Flash supervisor + UI-TARS-1.5-7B grounder.
                    Kept for benchmark reproducibility.

Other backends (holo3, openrouter direct) live on feat/multi-backend-gui-agent.
"""
from .. import config
from .base import LiveUIProvider


def get_provider() -> LiveUIProvider:
    """Return the live UI provider for the configured backend."""
    backend = (config.LIVE_UI_BACKEND or "bash").lower()
    if backend == "bash":
        from .bash_backend import openrouter_bash_provider
        return openrouter_bash_provider()
    if backend != "supervised":
        raise ValueError(
            f"Unknown ACU_LIVE_UI_BACKEND={backend!r}. Use 'bash' (default) or 'supervised'. "
            f"holo3 / openrouter live on feat/multi-backend-gui-agent."
        )
    from .openrouter import OpenRouterVLMProvider
    return OpenRouterVLMProvider()
