# Screenshot-only CUA models — API configuration guide

How to call each pixel-based computer-use model **correctly**, in a way the model was actually trained for. Most failures we saw in `experiments/holo3-vs-flagships-2026-04-25/` were not capability problems — they were protocol mismatches (wrong API surface, wrong coord format, wrong reasoning persistence). Use this doc as the "if you point your harness at model X, here's the recipe" reference.

## TL;DR

- The whole post-2025 frontier is screenshot-only. Anthropic, OpenAI, Google, ByteDance, Alibaba, H Company all train on raw pixels + mouse/keyboard, no DOM.
- **But each one has its own native API surface.** Calling Claude through OpenRouter chat-completions instead of Anthropic's `computer_20251124` tool with the `computer-use-2025-11-24` beta header takes the model off-distribution and triggers click-loops.
- **OpenRouter chat-completions only really works as a generic shell** for two models: `openai/gpt-5.4` (5/5 with the `bash` backend) and `anthropic/claude-opus-4-7` (5/5, slower). Everyone else needs their native protocol — see `Provider compatibility matrix` below.

---

## Benchmark table (screenshot-only models)

`[self-reported]` = vendor-submitted, not independently verified. Closed-API models are often only available via their native protocol.

| Model | Provider | OSWorld-Verified | ScreenSpot-Pro | Modality | Coord space | Native API |
|---|---|---|---|---|---|---|
| Claude Opus 4.7 | Anthropic | **78.0%** `[self-reported]` | — | Screenshot-only | abs pixels | Messages API + `computer_20251124` tool + `computer-use-2025-11-24` beta header |
| Claude Opus 4.6 | Anthropic | 72.7% `[self-reported]` | 83.1% | Screenshot-only | abs pixels | same as above |
| Claude Sonnet 4.6 | Anthropic | 72.5% | — | Screenshot-only | abs pixels | same as above |
| Claude Sonnet 4.5 | Anthropic | 61.4% | — | Screenshot-only | abs pixels | same as above |
| Claude Haiku 4.5 | Anthropic | 50.7% | — | Screenshot-only | abs pixels | same as above |
| GPT-5.5 | OpenAI | **78.7%** `[self-reported]` | — | Screenshot-only | abs pixels | Responses API + `computer_use_preview` tool |
| GPT-5.4 | OpenAI | **75.0%** | 85.4% (#1) | Screenshot-only | abs pixels | same as above |
| OpenAI CUA / Operator | OpenAI | 38.1% (Jan 2025 release) | — | Screenshot-only | abs pixels | same as above |
| Gemini 3.1 Pro | Google | 72.7% `[self-reported]` | 84.4% (#2) | Screenshot-only | abs pixels | Gemini API + `computer_use` tool *(rolling out — verify)* |
| Gemini 2.5 Computer Use | Google | — (browser-optimized, not OSWorld-tuned) | — | Screenshot + recent-action history | normalized 0–1000 | Gemini API or Vertex AI + `computer_use` tool |
| Holo3-122B-A10B | H Company | **78.85%** `[self-reported]` | — | Screenshot-only, stateless | normalized 0–1000 | H Company API + `extra_body.structured_outputs.json` |
| Holo3-35B-A3B | H Company | 77.8–82.6% `[self-reported]` | — | Screenshot-only, stateless | normalized 0–1000 | same as above; Apache-2 weights also on HF |
| UI-TARS-2 (~72B) | ByteDance | 47.5% | — | Screenshot-only | normalized 0–1000 (resized space) | OpenAI-compatible chat-completions; output format `Thought: ...\nAction: click(start_box='(x,y)')` |
| UI-TARS-1.5-7B | ByteDance | 22.7–24.6% | 38.1% raw / +MVP 56.1% | Screenshot-only | normalized 0–1000 (resized space) | same as above |
| Qwen3-VL-235B-A22B Instruct | Alibaba | 66.7% | 61.8% | Screenshot-only | grounding: 0–1000 / agent: abs pixels | OpenAI-compatible chat-completions; system prompt must specify format |
| Qwen3-VL-32B | Alibaba | — | 60.5% raw / +MVP 74.0% | Screenshot-only | same dual-mode | same as above |

---

## Per-provider API recipes

### Anthropic — Claude (Sonnet/Opus 4.x)

**Endpoint:** `https://api.anthropic.com/v1/messages` via the official SDK. **Not** OpenRouter chat-completions — OpenRouter strips beta headers.

**Beta header:** `anthropic-beta: computer-use-2025-11-24` (or the older `2025-01-24` for pre-4.6 deployments).

**Tool definition:**
```python
tools = [{
    "type": "computer_20251124",
    "name": "computer",
    "display_width_px": 1920,
    "display_height_px": 1080,
    # display_number is optional, for X11 multi-display
}]
```

**Action types** (model emits these as `tool_use` items): `key`, `hold_key`, `type`, `cursor_position`, `mouse_move`, `left_mouse_down`, `left_mouse_up`, `left_click`, `left_click_drag`, `right_click`, `middle_click`, `double_click`, `triple_click`, `scroll`, `wait`, `screenshot`, `zoom` (Opus 4.7+).

**Coord format:** absolute pixels in the calibrated `display_width_px × display_height_px` space. Resize your screenshots to that exact size before sending.

**Image ceiling:** Opus 4.7 caps at ~3.75 megapixels per screenshot. Downscale before submission or you'll waste tokens / get errors.

**Recommended system prompt pattern:** Anthropic's quickstart explicitly includes "after each action, take a screenshot and verify the result before proceeding." Skipping this is the documented cause of the 4.6 click-loop pathology ([claude-code#24585](https://github.com/anthropics/claude-code/issues/24585), [#26761](https://github.com/anthropics/claude-code/issues/26761)).

**Reference implementation:** [anthropic-quickstarts/computer-use-demo/loop.py](https://github.com/anthropics/anthropic-quickstarts/tree/main/computer-use-demo). Shows the canonical screenshot → tool_use → tool_result → screenshot loop, including the recommended system prompt and action executor.

**Why this matters for our project:** running Sonnet/Opus 4.6 through `bash` backend on OpenRouter gave 0/5 with the click-loop. The fix is *not* prompt engineering — it's switching to the native `computer_20251124` schema. The model was RL'd against that schema; off-distribution it loops.

### OpenAI — GPT-5.4, GPT-5.5, CUA / Operator

**Endpoint:** `https://api.openai.com/v1/responses` (the **Responses API**, not Chat Completions). OpenRouter does not yet proxy the Responses API for the hosted `computer` tool, so call OpenAI directly for native CU.

**Tool definition:**
```python
tools = [{
    "type": "computer_use_preview",
    "display_width": 1920,
    "display_height": 1080,
    "environment": "linux",   # or "browser" | "mac" | "windows"
}]
```

**Action types** (emitted in `computer_call` items): `click`, `double_click`, `drag`, `keypress`, `move`, `screenshot`, `scroll`, `type`, `wait`.

**Coord format:** absolute pixels.

**Reasoning persistence (load-bearing):** between turns, pass `previous_response_id=<id of last response>`. The Responses API stores reasoning items server-side and re-uses them; in chat-completions, reasoning items are dropped between turns and the model loses ~3% on benchmarks ([OpenAI Responses migration guide](https://developers.openai.com/api/docs/guides/migrate-to-responses)). This is why GPT-5.5 over-engineers via OpenRouter chat-completions — it has no reasoning continuity.

**Safety checks:** every `computer_call` may include `pending_safety_checks` (e.g. password fields, suspected destructive action). Acknowledge them before executing or the next turn will refuse.

**Reference implementation:** [openai/openai-cua-sample-app](https://github.com/openai/openai-cua-sample-app). Shows the loop, safety-check handling, and Playwright/Docker environments.

**Why this matters for our project:** `bash + gpt-5.4` works for us via OpenRouter chat-completions only because GPT-5.4 generalizes well. For GPT-5.5 we need the Responses API + `previous_response_id` to recover the published 78.7%.

### Google — Gemini 2.5 / 3 Computer Use

**Endpoint:** Gemini API (`generativelanguage.googleapis.com`) or Vertex AI. AI Studio API key works for the Gemini API path. OpenRouter does proxy Gemini chat-completions, but the `computer_use` tool may not be exposed there — verify or call Google directly.

**Tool definition:**
```python
tools = [{
    "computer_use": {
        "environment": "ENVIRONMENT_BROWSER",  # or ENVIRONMENT_DESKTOP, ENVIRONMENT_ANDROID
        # excluded_predefined_functions: optional list
    }
}]
```

**Action types:** see the [Gemini 2.5 Computer Use eval PDF](https://storage.googleapis.com/deepmind-media/gemini/computer_use_eval_additional_info.pdf). Coordinates are screen-space **normalized 0–1000** — rescale to pixels before dispatching to xdotool.

**Conditioning:** the model is fed the *recent action history* alongside the current screenshot. Don't drop assistant turns — keep the last N actions in the messages array.

**Caveats:**
- Google explicitly says "browser-optimized, not OS-optimized" — don't expect OSWorld-class desktop performance.
- Gemini-3-pro on OpenRouter has 5–13s per-turn API latency. Even on Google's first-party API it's slow per turn; will not fit a 60s MCP cap reliably.
- Realistic Gemini play in our stack: **Gemini Flash as a planner under a separate grounder** (Holo3 or UI-TARS), matching the existing `supervised` backend architecture.

### H Company — Holo3

**Endpoint:** `https://api.hcompany.ai/v1/chat/completions` (OpenAI-compatible). Auth via `HAI_API_KEY`. Holo3 is **not** on OpenRouter.

**Structured-output mechanism:**
```python
extra_body = {
    "structured_outputs": {"json": SCHEMA},
    "chat_template_kwargs": {"enable_thinking": False},  # for grounding-only calls
}
```
Note: this is **not** OpenAI's `response_format={"type": "json_schema", ...}`. H Company's quickstart specifies `extra_body.structured_outputs.json`. ([hub.hcompany.ai/quickstart](https://hub.hcompany.ai/quickstart))

**Schema:** keep it small and single-purpose. Cookbook examples define one Pydantic class per call (`ClickCoordinates`, `NavigationStep`), not a 13-way action union. The published 78.85% OSWorld-Verified score is with H Company's own (closed) scaffold — assumed to be planner+grounder split, not single-model agent loop.

**Coordinate format:** **0–1000 normalized**, with `Field(ge=0, le=1000)` on each `x`, `y`. Convert back: `pixel_x = int(x / 1000 * image.width)`. Sending pixel coords or unbounded ints silently desyncs.

**Smart-resize input image** (load-bearing — the cookbook is explicit about this): use Qwen-style `smart_resize` with `factor=32`, `min_pixels=64*32**2`, `max_pixels=16384*32**2`. The served model performs internal resizing; mismatched dimensions silently misalign coords. Reference: [hai-cookbook/utils/image.py](https://github.com/hcompai/hai-cookbook/blob/main/utils/image.py), [holo2 localization notebook](https://github.com/hcompai/hai-cookbook/blob/main/holo2/holo_2_localization_hosted_api.ipynb).

**Stateless calls:** every cookbook example is `(task, single screenshot) → ClickCoordinates`. No multi-turn pattern is published. Don't stuff chat history; carry agent state in the user message text or in a `note` field, not in `messages`.

**Reasoning:** Holo3 always emits `message.reasoning` separately. Log it, don't feed it back. Set `enable_thinking: False` for grounding calls to cut ~2× latency.

**Temperature:** `0.0` for deterministic grounding.

**Why our integration failed (0/5 on bake-off):** we sent unbounded pixel coords + a 13-way action union + chat history at native screen resolution. The fix is the recipe above. See `docs/HOLO3-INTEGRATION-FIX.md` *(to be written)* or the H Company cookbook directly.

### ByteDance — UI-TARS-1.5 / UI-TARS-2

**Endpoint:** OpenAI-compatible chat-completions. Available on OpenRouter as `bytedance/ui-tars-1.5-7b`. The 72B variant is sometimes listed on OpenRouter but returns 404 — self-host or use ByteDance's own endpoint.

**Output format (native, not tool_calls):** `Thought: ...\nAction: click(start_box='(x,y)')`. Other actions follow the same `Action: <name>(...)` grammar.

**Coord format:** **0–1000 normalized**, but in the *resized* image space (after the model's smart-resize), not original screenshot dimensions. You **must** re-resize the input image with `IMAGE_FACTOR=28` (multiples of 28 px on each side) and rescale coords back to original pixels.

**Reference parser:** [xlang-ai/OSWorld/mm_agents/uitars_agent.py](https://github.com/xlang-ai/OSWorld/blob/main/mm_agents/uitars_agent.py). Canonical implementation of the smart-resize + coord-rescale dance. Also [UI-TARS README_coordinates.md](https://github.com/bytedance/UI-TARS/blob/main/README_coordinates.md).

**System prompt:** UI-TARS publishes templated system prompts per action space (mobile vs desktop vs web). Use the official template — homegrown prompts that don't match the training distribution underperform.

### Alibaba — Qwen3-VL series

**Endpoint:** OpenAI-compatible chat-completions. Available on OpenRouter (`qwen/qwen3-vl-235b-a22b-instruct`, `qwen/qwen3-vl-32b-instruct`).

**Variant choice:** **Use `Instruct`, not `Thinking`, for OSWorld-style tasks.** The Thinking variant *underperforms* Instruct on OSWorld (38.1% vs 66.7%) per the Qwen3-VL technical report — counterintuitive but documented.

**Coordinate format (dual-mode quirk):**
- **Grounding tasks** return `bbox_2d=[x1, y1, x2, y2]` in 0–1000 normalized space. Take the bbox center, then rescale: `pixel_x = int((x1+x2)/2 / 1000 * image.width)`.
- **GUI-agent tasks** return absolute screen pixels.

The mode is selected by the system prompt. The infamous `{x:[a,b]}` malformed output we saw in the bake-off was the model leaking its bbox grounding format under the wrong system prompt — symptom of an unset / ambiguous prompt mode.

**System prompt:** must explicitly require the coord format with a one-shot example, e.g.:
```
Output one JSON action per turn, like: {"action":"click","bbox":[x1,y1,x2,y2]}
where bbox is normalized 0-1000. Example: {"action":"click","bbox":[420,300,460,330]}
```

**Smart-resize:** Qwen image processor uses `factor=28` (multiples of 28). Same family as UI-TARS.

**Issues:** [QwenLM/Qwen3-VL#1486](https://github.com/QwenLM/Qwen3-VL/issues/1486), [#1927](https://github.com/QwenLM/Qwen3-VL/issues/1927) document the dual-coord-space gotcha.

---

## Provider compatibility matrix

What works on OpenRouter chat-completions vs what needs the native protocol.

| Provider | Via OpenRouter chat-completions | Via native protocol | Notes |
|---|---|---|---|
| Anthropic | works for `bash` backend (gpt-5.4-style raw shell), but `computer_20251124` tool **not exposed** | required for native CU; beta header strips on OpenRouter | Opus 4.7 generalizes well enough for `bash` to give 5/5; 4.6 needs native |
| OpenAI | works for `bash` backend; **Responses API not proxied** | required for `computer_use_preview` + reasoning persistence | GPT-5.4 hits 5/5 via `bash` chat-completions; GPT-5.5 needs native to recover |
| Google | Gemini chat-completions are proxied; **`computer_use` tool exposure unclear** — verify | safer to use Gemini API direct (AI Studio key) or Vertex | Per-turn latency caps usefulness regardless |
| H Company (Holo3) | **not on OpenRouter at all** | required: H Company API + `extra_body.structured_outputs.json` | Use their cookbook recipe verbatim |
| ByteDance (UI-TARS) | works (output is plain text the harness parses) | OpenRouter is fine; ByteDance endpoint also available | UI-TARS-72B listed on OpenRouter but **404 — not actively served** |
| Alibaba (Qwen3-VL) | works | OpenRouter is fine; Alibaba Cloud / DashScope also available | Set system prompt explicitly for coord mode |

---

## Recommendation matrix — which model + which harness

For our DETM stack (xdotool on Linux Xvfb, no DOM):

| Goal | Recommended config |
|---|---|
| **Best closed-API today** | GPT-5.4 + OpenAI Responses API + `computer_use_preview` tool. Or GPT-5.4 + `bash` backend on OpenRouter (we've verified 5/5, 3m 32s, ~$0.20/task). |
| **Non-OpenAI fallback** | Opus 4.7 + Anthropic Messages API + `computer_20251124` + beta header. Or Opus 4.7 + `bash` backend on OpenRouter (5/5 in 4m 44s, ~$0.30). |
| **Anthropic's smaller models** (Sonnet 4.6, Opus 4.6) | Must use native `computer_20251124` + beta header. They click-loop on generic `bash`. Recommended system prompt: "after each action take a screenshot and verify before proceeding." |
| **Best open-weight grounder** | Holo3-35B-A3B or Qwen3-VL-32B. Use as grounder under a separate Gemini Flash planner — that's how the published OSWorld scores were achieved. |
| **Best OpenRouter-only stack** | `bash` backend + `openai/gpt-5.4`. Confirmed in our bake-off; this is the production config. |
| **Cheapest end-to-end** | DETM `supervised` backend (Gemini Flash + UI-TARS-7B + MVP) — ~$0.07/task on OSWorld Chrome subset. Already implemented. |

---

## Caveats and footguns

- **`[self-reported]` scores ≠ verified.** The OSWorld team flags vendor submissions as not independently re-run. Treat Holo3 78.85, GPT-5.5 78.7, Opus 4.7 78.0, Gemini 3.1 Pro 72.7 as upper bounds, not guaranteed.
- **Pipeline > model size up to a point.** Inference-time augmentation (RegionFocus +5pp, MVP +14–20pp on ScreenSpot-Pro) often beats jumping to a larger base model. Worth trying before spending more on a flagship.
- **OSWorld vs OSWorld-Verified** are different tracks. Sonnet 3.5's 14.9% / 22.0% on the original 2024 paper variant are not directly comparable to today's Verified track.
- **Coord-format mismatches silently fail.** A model emits `[480, 300]` and your harness clicks `(480, 300)` in pixels — but the model meant 0–1000 normalized in a 1920×1080 image, so the actual target is `(921, 324)`. The click "succeeds" (xdotool returns 0) but lands nowhere meaningful; the screen doesn't change; the model retries the same coords; click-loop. Always verify coord space before integrating.
- **Beta-header / Responses-API gating.** OpenRouter does not pass Anthropic's `anthropic-beta` header for computer-use, and does not (yet) proxy OpenAI's Responses API for the hosted `computer` tool. To use the native protocols you must call the provider directly.
- **Smart-resize is non-optional.** Qwen3-VL family (Holo3, UI-TARS, Qwen3-VL) all expect images preprocessed to multiples of 28 or 32 pixels. The served model resizes internally; mismatch desyncs coords.
- **Reasoning persistence.** OpenAI's chain-of-thought items live across turns *only* via `previous_response_id` in the Responses API. Chat-completions drops them — costs ~3pp on benchmarks.

---

## Reference implementations to study

- [anthropic-quickstarts/computer-use-demo](https://github.com/anthropics/anthropic-quickstarts/tree/main/computer-use-demo) — canonical Anthropic Messages API + `computer_20251124` loop.
- [openai/openai-cua-sample-app](https://github.com/openai/openai-cua-sample-app) — canonical Responses API + `computer_use_preview` loop with safety checks.
- [hcompai/hai-cookbook](https://github.com/hcompai/hai-cookbook) — Holo2/Holo3 grounding notebooks; the smart-resize util + `ClickCoordinates` schema.
- [bytedance/UI-TARS README_coordinates.md](https://github.com/bytedance/UI-TARS/blob/main/README_coordinates.md) — coord-rescale recipe.
- [xlang-ai/OSWorld/mm_agents/uitars_agent.py](https://github.com/xlang-ai/OSWorld/blob/main/mm_agents/uitars_agent.py) — production UI-TARS parser used for the public OSWorld leaderboard.
- [browser-use](https://github.com/browser-use/browser-use) — for a contrasting view: a non-pixel DOM-anchored framework with separate Anthropic / OpenAI prompt branches.

---

*Last updated: 2026-04-30. Companion doc: `docs/GUI-AGENT-BACKENDS.md` (which backends DETM ships) and `experiments/holo3-vs-flagships-2026-04-25/README.md` (empirical bake-off).*
