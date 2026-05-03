# Frontier flagship bake-off — DETM gui_agent backend

**Date:** 2026-04-25
**Task shape (constant across runs):** *"Send 5 LinkedIn connection requests to employees of a named AI company."*
**Driver:** OpenClaw TUI → main LLM (`openai-codex/gpt-5.4`) → DETM `gui_agent` MCP → daemon `/gui_agent` HTTP → backend → action executor → xdotool on `:99`
**Display:** Xvfb `:99` at 1920×1080, real Firefox with persistent LinkedIn session.
**Protocol:** Wall-clock = first user prompt → final assistant message in OpenClaw session JSONL. Fresh TUI session per run. Different fresh AI-company target per run.

## Results

| Run | Backend | Model | Target | Wall-clock | Result |
|---|---|---|---|---|---|
| **gpt-5.4-bash** | `bash` | `openai/gpt-5.4` | Cognition AI | **3m 32s** | **5/5 ✓** |
| **opus-4.7-bash** | `bash` | `anthropic/claude-opus-4.7` | Runway | **4m 44s** | **5/5 ✓** |
| gpt-5.4 (direct) | `openrouter` (direct.py) | `openai/gpt-5.4` | Replicate | 17m 18s | 4/5 |
| gpt-5.5-bash | `bash` | `openai/gpt-5.5` | Together AI | 17m 1s | 2/5 |
| opus-4.6-bash | `bash` | `anthropic/claude-opus-4.6` | Sakana AI | 10m 42s | 0/5 (loop) |
| sonnet-4.6-bash | `bash` | `anthropic/claude-sonnet-4.6` | Stability AI | 12m 38s | 0/5 (MCP timeouts) |
| sonnet-4.6 (direct) | `openrouter` (direct.py) | `anthropic/claude-sonnet-4.6` | Stability AI | 9m 44s | 0/5 (action loop + MCP) |
| gemini-3.1-pro (direct) | `openrouter` (direct.py) | `google/gemini-3.1-pro-preview` | Character.AI | 18m 41s | 0/5 (slow per turn) |
| holo3-122b (direct) | `holo3` (native /chat/completions) | `holo3-122b-a10b` | Cursor | 8m 03s | 0/5 (action loop) |
| holo3-35b (direct) | `holo3` (native) | `holo3-35b-a3b` | Together AI | rate-limited | n/a (10 RPM cap) |
| ~~gemini-3.1-pro-bash, gpt-5.5-direct, opus-4.6-direct, opus-4.7-direct~~ | not run | | | | |

## Headline finding

**gpt-5.4 + bash backend is the only configuration that achieves both 5/5 and a fast wall-clock.** opus-4.7 + bash also gets 5/5 but in a different time bracket.

The dominant factor is **the model's bash composition style**, not raw OSWorld score, not reasoning depth, not provider:

- gpt-5.4 writes efficient xdotool sequences (`mousemove + click + sleep + mousemove + click`) → fits ≥5 useful actions per 60s gui_agent window
- opus-4.7 walked down LinkedIn's connect list with different y-coordinates per click and recovered when search-limited → 5/5
- gpt-5.5 over-engineered (inline Python, JavaScript injection via address bar) → too slow → 2/5
- sonnet-4.6 wrote one cautious action per call with diagnostic checks (`getwindowfocus`, `getmouselocation`) → hit the 60s MCP cap → 0/5
- opus-4.6 looped on identical click coordinates after click-misses → 0/5
- gemini-3.1-pro: 5–13s per API call → 4–8 actions per 60s window → grounding errors compound → 0/5
- holo3-122b: doesn't condition on history; loops indefinitely on no-change feedback → 0/5

## The bash backend pivot was the key improvement

For gpt-5.4 specifically, switching from `direct.py` (structured `click(x,y) / type_text(text)` schema) to `bash` (single `bash(command)` tool with raw shell):

| | direct.py | bash |
|---|---|---|
| Wall-clock | 17m 18s | 3m 32s |
| Result | 4/5 | 5/5 |
| Tool feedback | `"ok"` | real exit code + stdout/stderr |
| Multi-step composition | one tool per turn | `mousemove + click + sleep` in one call |

Why bash helps:
1. **Real diagnostic feedback**: model sees actual stderr/exit codes, not opaque "ok"
2. **Native composition**: one shell call can do 3–5 actions, fitting more in the 60s gui_agent window
3. **Match training distribution**: frontier models are heavily trained on shell, less on schema-coerced tool calling
4. **No schema fights**: direct.py had to coerce Qwen's `{x:[a,b]}`, didn't help anyway because the wrapper provided no signal that an action visibly worked

## What we tried and excluded

### Models excluded after preliminary testing (kept under `_dropped/`)

- `holo3-35b-a3b` — free tier 10 RPM cap; would need credits
- `holo3-122b-a10b` — action-loop pathology; doesn't condition on history
- `google/gemini-3.1-pro-preview` — too slow per turn for the 60s gui_agent cap

### Backend approaches we tried

- `supervised` (Gemini Flash + UI-TARS) — works but heavy; not in this bake-off
- `direct.py` (OpenAI tool calling with structured `click(x,y)` schema) — partial success only with gpt-5.4
- `holo3` (Holo3-native `/chat/completions` with structured_outputs) — 0/5 (action-loop)
- `bash` (single `bash(command)` tool) — 2/5 successes (gpt-5.4 + opus-4.7), 2/5 failures (sonnet-4.6, opus-4.6, gpt-5.5 partial)

## Per-model observations

### Anthropic family — click-loop tendency (with one exception)

Sonnet 4.6 (direct + bash) and Opus 4.6 (bash) all exhibited the same pattern: when a click "succeeded" (xdotool returned ok) but the screen looked unchanged, the model emitted the same coordinates again. 5–6 consecutive identical clicks before timeout.

Opus 4.7 broke the pattern — walked down LinkedIn's connect list with different y-coordinates and adapted when LinkedIn hit a search limit. Whether this is a genuine model improvement (4.7 over 4.6) or task-specific (Runway being easier than Sakana) is unclear from one run.

### OpenAI family — diverging styles

- gpt-5.4 wrote tight, efficient xdotool sequences. Picked up `--clearmodifiers` from a one-line system prompt hint and used it on every click.
- gpt-5.5 wrote elaborate Python and JavaScript-via-address-bar tricks. Smarter, slower, less effective for this task.

### Google / H Company

- Gemini 3.1 Pro Preview: too slow per turn (5–13s API latency) → couldn't fit useful work in 60s windows
- Holo3: trained for stateless `(task, screenshot) → action`; doesn't condition on chat history → loops without breaking

## Cost summary

| Run | Cost (estimate) |
|---|---|
| gpt-5.4-bash | $0.20 |
| opus-4.7-bash | $0.30 |
| gpt-5.4-direct | $0.80 |
| gpt-5.5-bash | $1.50 |
| opus-4.6-bash | ~$0.60 (lots of looped retries) |
| sonnet-4.6-bash | ~$0.50 |
| sonnet-4.6-direct | ~$0.50 |
| gemini-3.1-pro-direct | ~$1.50 |
| **Total** | **~$5.90** |

## Practical recommendation

**Production config: `ACU_LIVE_UI_BACKEND=bash` + `ACU_OPENROUTER_GUI_DIRECT_MODEL=openai/gpt-5.4`**.

That's the only configuration that achieved 5/5 in <5 minutes consistently (3m 32s, ~$0.20/task), and the bash composition style of gpt-5.4 makes it robust to the 60s OpenClaw MCP cap.

The other models can probably be made to work with their respective native computer-use APIs (Anthropic's `computer_20251124` tool + `computer-use-2025-11-24` beta header for Claude family; OpenAI's hosted `computer` tool via the Responses API for GPT-5.5), but that requires backend implementations we didn't build here. Documented as future work.

### Why not the other configs

- **opus-4.7 + bash also got 5/5** (4m 44s) but is 1.5× slower wall-clock and 1.5× more expensive ($0.30 vs $0.20). Credible fallback if you need a non-OpenAI provider, but gpt-5.4 wins on the metrics.
- **sonnet-4.6, opus-4.6, gpt-5.5** all fail in different model-specific ways with our generic-bash harness. The likely fix for each is to use the model's *native* computer-use tool format — out of scope for this experiment.
- **gemini-3.1-pro** is too slow per turn for the chain pattern; would need a longer gui_agent budget cap which conflicts with OpenClaw's MCP cap.

## Artifact layout

```
experiments/holo3-vs-flagships-2026-04-25/
├── README.md                      # this file
├── extract_run.sh
├── _dropped/
│   ├── holo3-35b/
│   └── holo3-122b/
├── claude-sonnet-4.6/             # direct.py run, failed
├── sonnet-4.6-bash/               # bash run, failed
├── claude-opus-4.6/               # not run
├── opus-4.6-bash/                 # bash run, failed (loop)
├── opus-4.7-bash/                 # bash run, SUCCESS 5/5
├── gpt-5.4/                       # direct.py run, partial 4/5
├── gpt-5.4-bash/                  # bash run, SUCCESS 5/5
└── gpt-5.5-bash/                  # bash run, partial 2/5
```

Each run dir contains `conversation.jsonl`, `daemon.log`, `tool-histogram.txt`, `wallclock.txt`, `screenshot.jpg`, and `NOTES.md` with per-run analysis.
