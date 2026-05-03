# gui_agent backend

DETM ships **one** production gui_agent backend: `bash` + `openai/gpt-5.4` via OpenRouter. It's the only configuration that achieved 5/5 on the LinkedIn 5-connect benchmark in <5 minutes (3m 32s, ~$0.20/task) — see `experiments/holo3-vs-flagships-2026-04-25/`.

The legacy `supervised` backend (Gemini Flash + UI-TARS-7B) is kept for benchmark reproducibility. Other backends explored during the bake-off (Holo3, generic OpenRouter direct, ByteDance UI-TARS-2, etc.) live on the `feat/multi-backend-gui-agent` branch — they didn't ship to production.

## Switching backends

```bash
# Production (default) — bash + gpt-5.4
export ACU_LIVE_UI_BACKEND=bash
export ACU_OPENROUTER_GUI_DIRECT_MODEL=openai/gpt-5.4
export OPENROUTER_API_KEY=<your key>

# Legacy supervised (benchmark reproducibility)
export ACU_LIVE_UI_BACKEND=supervised
```

`dev.sh` passes both `ACU_LIVE_UI_BACKEND` and `ACU_OPENROUTER_GUI_DIRECT_MODEL` through to the daemon.

## What the bash backend does

Each turn:
1. Capture screenshot (display `:99`)
2. POST `/chat/completions` to OpenRouter with `tools=[bash, screenshot, done, partial, failed, escalate]`
3. Model emits `bash(command)` — we execute via subprocess.Popen with the display env-var set, capture stdout/stderr/exit_code, kill the child group on timeout
4. Feed back tool result + post-action screenshot
5. Repeat until model picks a terminal tool or budget exhausts

The model writes `xdotool` / `wmctrl` / `scrot` / etc. directly. No structured-action wrapper. Why this works: real diagnostic feedback (vs opaque "ok"), multi-step composition per turn (`mousemove + click + sleep + ...`), matches gpt-5.4's training distribution for shell-using agents.

Implementation: `src/agentic_computer_use/live_ui/bash_backend.py`.

Companion docs:
- `docs/SCREENSHOT-CUA-MODELS.md` — pixel-only CUA model API recipes (Anthropic / OpenAI / Google native protocols), in case you want to try other models.
- `experiments/holo3-vs-flagships-2026-04-25/README.md` — empirical bake-off results.
- `experiments/linkedin-stresstest-2026-05-02/` — ongoing stress tests on the production config.
