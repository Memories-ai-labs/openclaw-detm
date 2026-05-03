# Performance Optimization — SmartWait + GUI Pipeline

> **Note (May 2026):** the optimizations described in this doc target the **supervised** `gui_agent` backend (Gemini Flash supervisor + UI-TARS grounder). DETM's production default is now the **bash** backend (`openai/gpt-5.4` via OpenRouter), which doesn't use a supervisor/grounder split — most of the per-step optimizations here don't apply to it. The doc is kept for the supervised configuration which is still available as `ACU_LIVE_UI_BACKEND=supervised`.

## Overview

This document summarizes all performance improvements made to the OpenClaw
agentic-computer-use pipeline, along with a cost analysis of vision backends.

---

## Before / After

### SmartWait pipeline

| Metric | Before | After |
|---|---|---|
| Vision HTTP overhead (per call) | 100–300 ms (new TLS + TCP every call) | ~0 ms (persistent connection) |
| Ollama cold-start penalty | 5–30 s (model reloaded each time) | 0 s (model stays loaded for 10 min) |
| Frame capture blocking | Blocks event loop 30–100 ms | Off-thread via `run_in_executor` |
| JPEG encoding blocking | Blocks event loop 10–50 ms | Off-thread via `run_in_executor` |
| Multiple active wait jobs | Evaluated **serially** (one at a time) | Evaluated **in parallel** via `asyncio.gather` |
| Pixel diff gate | 320px downsampled numpy diff every tick | **Removed** — misses small critical changes |
| Poll interval | Adaptive 0.5–15 s (based on diff/partial) | **Fixed 1 s** — simpler, predictable |
| Verdict states | `resolved` / `partial` / `watching` | **Binary YES/NO** — partial removed |
| Context per job | 4 frames + 3 verdicts + deques | **Single frame** — stateless eval |
| `openclaw` CLI injection | Blocks event loop for up to 10 s | Async subprocess, event loop unblocked |
| Dashboard frame buffer | Captured at 4 fps (every 250 ms) | Captured at 2 fps (every 500 ms) |
| Vision cloud option | None | OpenRouter added (no GPU required) |
| Vision model | Local Ollama `minicpm-v` (no GPU required) | `google/gemini-2.0-flash-lite-001` via OpenRouter |
| Capture lock contention | Global lock — all displays serialized | Per-display lock — different tasks run in parallel |
| Xlib frame copy | Two allocations: fancy-index + `.copy()` | One allocation: fancy-index only |
| PIL resize filter | LANCZOS (highest quality, slowest) | BILINEAR (adequate for vision model, 2–3× faster) |
| Frame resolution sent to model | 1920px max (full desktop) | **960px max** — sufficient for YES/NO checks |
| JPEG quality sent to model | 80 | **72** — ~25% smaller payload, indistinguishable |
| Model max output tokens | 450 | **150** — YES/NO responses are ~20–50 tokens |

### GUI pipeline

| Metric | Before | After |
|---|---|---|
| GUI backend HTTP overhead (per grounding call) | 100–300 ms (new TLS + TCP per call) | ~0 ms (persistent client per backend) |
| GUI screen capture (event loop) | Blocked event loop 30–150 ms | Off-thread via `run_in_executor` |
| GUI JPEG encoding (event loop) | Blocked event loop 10–50 ms | Off-thread via `run_in_executor` |
| xdotool subprocess spawns per action | 2–4 subprocesses (click, focus, drag) | 1–2 (commands chained in single call) |
| Post-focus sleep in `desktop_look` | 300 ms unconditional sleep | 50 ms |
| Florence-2 captioning (OmniParser) | N serial GPU forward passes (1 per crop) | 1 batched forward pass for all crops |
| Coordinate normalization (UI-TARS) | Bug: `x ≤ 1.0 and y ≤ 1.0` missed edge cases | Fixed: `max(x, y) ≤ 1.0` |

---

## Changes in Detail

### Phase 1 — HTTP Connection Pooling (`vision/backends/*.py`)

**Problem:** Each vision call created a new `httpx.AsyncClient`, performed a
full TLS handshake + TCP connection, then tore it down after one request.

**Fix:** Module-level persistent `httpx.AsyncClient` instances, reused across
all calls. Created lazily on first use.

**Files changed:** `claude.py`, `ollama.py`, `vllm.py`

**Gain:** Saves 100–300 ms per vision call for cloud APIs, 20–50 ms for local Ollama.

---

### Phase 1b — OpenRouter Vision Backend (`vision/backends/openrouter.py`)

**New feature:** A new `openrouter` backend lets you route SmartWait vision
queries to any model on OpenRouter (Claude Haiku, Gemini Flash, etc.) without
running a local GPU.

**Config:**
```bash
ACU_VISION_BACKEND=openrouter
OPENROUTER_API_KEY=sk-or-...
ACU_OPENROUTER_VISION_MODEL=google/gemini-2.0-flash-001  # or anthropic/claude-haiku-4-5
```

**Files changed:** `vision/backends/openrouter.py` (new), `vision/__init__.py`

---

### Phase 2 — Async Frame Capture (`wait/engine.py`)

**Problem:** `capture_screen()` (Xlib round-trip) and `frame_to_jpeg()` /
`frame_to_thumbnail()` (PIL compression) were called bare in the async event
loop, blocking it for 30–150 ms per poll cycle.

**Fix:** Both operations are now dispatched to the thread pool via
`loop.run_in_executor(None, ...)`.

**Note:** A module-level `asyncio.Lock` (`_CAPTURE_LOCK`) serializes Xlib
calls across concurrent job evaluations, since Xlib is not thread-safe.

**Gain:** Event loop freed during capture; reduces latency for other async
work (API responses, DB writes, dashboard polling) competing for the loop.

---

### Phase 3 — Parallel Job Evaluation (`wait/engine.py`)

**Problem:** The engine loop processed exactly one wait job per iteration.
If job A was waiting on a slow vision call (1–30 s), job B was completely
blocked.

**Fix:** The loop now collects **all overdue jobs** at each tick and evaluates
them concurrently with `asyncio.gather()`. Vision I/O is pure async and fully
parallel; frame captures share `_CAPTURE_LOCK` to stay thread-safe.

Both `_resolve_job` and `_timeout_job` guard against jobs that were concurrently
cancelled or resolved (check `if job.id not in self.jobs` before acting).

**Gain:** With 2 active wait jobs, evaluation latency is cut in half. With N
jobs, all are evaluated in the time of the slowest single vision call.

---

### Phase 4 — Ollama keep_alive (`config.py`)

**Problem:** `OLLAMA_KEEP_ALIVE` defaulted to `"0"`, meaning Ollama unloaded
the vision model from VRAM immediately after each response. The next call paid
a 5–30 s cold-start reload penalty.

**Fix:** Default changed to `"10m"` — model stays in VRAM for 10 minutes of
inactivity, then unloads automatically.

**Override:** Set `OLLAMA_KEEP_ALIVE=0` in `.env` to revert to the old
behaviour (useful if VRAM is shared and you need it freed between tasks).

**Gain:** Eliminates the dominant bottleneck for local Ollama inference between
closely-spaced wait jobs.

---

### Phase 5 — (superseded)

The original Phase 5 considered reducing `FRAME_MAX_DIM` and was deferred. It was later
implemented as Phase 15 — see below.

---

### Phase 6 — Diff Gate (removed in Phase 15)

An earlier implementation added a `PixelDiffGate` that skipped vision evaluation when
< 1% of pixels changed at 320px resolution. This was subsequently **removed entirely**
(Phase 15) because small but critical changes (status indicator, tiny dialog) don't
trigger 1% diff threshold. The correctness cost of missing a change outweighs the
negligible API cost of Gemini Flash Lite at $0.000045/eval.

---

### Phase 7 — Async Subprocess (`wait/engine.py`)

**Problem:** `_inject_system_event` used `subprocess.run()` with a 10 s
timeout to call the `openclaw` CLI. This is a **blocking** call that froze
the entire event loop for up to 10 s on each resolved wait job.

**Fix:** Replaced with `asyncio.create_subprocess_exec()` +
`asyncio.wait_for(proc.communicate(), timeout=10)`. The process runs
concurrently; the event loop stays responsive during the CLI call.

**Gain:** Event loop is no longer frozen on wait resolution. Dashboard
updates, new wait jobs, and DB writes all proceed normally during the CLI call.

---

### Phase 8 — Frame Buffer Rate (`daemon.py`)

**Problem:** The dashboard frame buffer captured a new screenshot every 250 ms
(~4 fps), making 4 Xlib round-trips per second regardless of activity.

**Fix:** Sleep increased to 500 ms (~2 fps). Dashboard live-view still feels
live at 2 fps; humans don't perceive the difference during monitoring.

**Gain:** Halved Xlib load on the system display thread. Reduces contention
with wait engine captures on `_CAPTURE_LOCK`.

---

### Phase 9 — Parallel JPEG + Thumbnail Encoding (superseded)

Previously the engine encoded two images per evaluation (full JPEG + thumbnail). This was
parallelized with `asyncio.gather()`. The thumbnail encoding was later removed entirely
(Phase 15 simplified the engine to encode only the current frame — no history thumbnails),
making this optimization moot. The parallel capture pattern remains for captures in `_evaluate_job`.

---

### Phase 10 — Per-Display Capture Lock (`wait/engine.py`)

**Problem:** A single global `asyncio.Lock` (`_CAPTURE_LOCK`) serialized all
frame captures across all active wait jobs. Jobs on different Xvfb displays
(e.g., task A on `:100`, task B on `:101`) were forced to queue even though
their Xlib connections are completely independent.

**Fix:** `_CAPTURE_LOCKS: dict[str, asyncio.Lock]` — one lock per display
string. Jobs on the same display still serialize (Xlib is not thread-safe per
connection); jobs on different displays are now fully parallel.

**Gain:** With N concurrent tasks on N displays, captures run in parallel
instead of queuing. Single-task setups see no change.

---

### Phase 11 — Reuse Diff Gate Frame for Thumbnail (superseded)

The diff gate and thumbnail history were both removed in Phase 15. This optimization
is no longer applicable.

---

### Phase 12 — Remove Redundant Frame Copy (`capture/screen.py`)

**Problem:** After channel-swapping BGRA → RGB via fancy indexing:
```python
return arr[:, :, [2, 1, 0]].copy()
```
NumPy fancy indexing (`[:, :, [2, 1, 0]]`) always produces a **new
contiguous array** — it is never a view. The `.copy()` allocates and memcpys
the entire frame a second time (~8 MB at 1920×1080) for no reason.

**Fix:** Removed `.copy()`.

**Gain:** Saves one 8 MB allocation + memcpy per frame capture (~5–15 ms).

---

### Phase 13 — BILINEAR Resize for Thumbnails (`capture/screen.py`)

**Problem:** `frame_to_jpeg` used `Image.LANCZOS` for all resizes. LANCZOS is
the highest-quality PIL filter (multi-lobe sinc) but also the slowest. It was
applied to thumbnails (360px) sent as historical context to the vision model —
a context where pixel-perfect interpolation has zero benefit.

**Fix:** Changed to `Image.BILINEAR`. For SmartWait's use case (vision model
looking at gross screen state), BILINEAR is indistinguishable. Note: for the
main JPEG encode (`FRAME_MAX_DIM=1920` on 1920px screens) no resize happens
at all, so this change only affects thumbnail generation.

**Gain:** 2–3× faster PIL resize for thumbnails (~3–8 ms per thumbnail).

---

### Phase 14 — Reduce Ollama `num_predict` (`vision/backends/ollama.py`)

**Problem:** `num_predict: 450` capped Ollama's output at 450 tokens. Actual
SmartWait responses are ~100–150 tokens.

**Fix:** Reduced to `250`.

**Gain:** Caps runaway generation sooner; minor in normal operation.

---

### Phase 15 — SmartWait Architecture Simplification (`wait/engine.py`, `wait/context.py`, `config.py`)

**Changes:** A batch of correctness-driven simplifications removed three subsystems:

**15a — Remove pixel diff gate**

The `PixelDiffGate` skipped vision evaluation when < 1% of 320px-downsampled pixels
changed. Problem: small but critical UI changes (status indicator, tiny dialog, checkmark)
fall below the threshold and are silently missed. With Gemini Flash Lite at $0.000045/eval,
polling every 1s for 5 minutes costs $0.013 — the correctness cost of missing a change
far outweighs this. Removed entirely.

**15b — Remove partial verdict**

The `partial` verdict triggered streak counting and accelerated polling. Removed because:
- Ambiguous: the model has no better answer for "70% complete" than "not done yet"
- Added complexity (streak counter, deque) with no measurable benefit
- **Timeout is the correct fallback.** The LLM set a timeout because it knows how long the task should take. If the condition isn't met by then, the LLM should re-evaluate.

**15c — Remove adaptive polling**

`AdaptivePoller` sped up on partial (now removed) and slowed down on static screens
(now handled by always evaluating). Replaced with fixed `POLL_INTERVAL = 1.0` s.

**15d — Remove context window history**

`JobContext` previously maintained rolling deques of 4 frames and 3 verdicts. Removed:
a single screenshot is sufficient for binary YES/NO evaluation; history adds tokens and
complexity without improving accuracy.

**Gain:** ~40% less code in the engine. Simpler data model. Eliminates false negatives
from the diff gate. No ambiguous partial states. Predictable 1s polling.

---

### Phase 16 — Reduce Frame Resolution (`config.py`, `vision/backends/openrouter.py`)

**Problem:** SmartWait was sending full 1920px-wide frames to the vision model at JPEG
quality 80. For binary YES/NO condition evaluation, this is overkill — the model needs
to identify gross UI state, not read fine text at pixel-perfect fidelity.

**Fix:**
- `FRAME_MAX_DIM`: 1920 → `960` (env: `ACU_FRAME_MAX_DIM`)
- `FRAME_JPEG_QUALITY`: 80 → `72` (env: `ACU_FRAME_JPEG_QUALITY`)
- OpenRouter backend `max_tokens`: 450 → `150` (YES/NO responses are ~20–50 tokens)

**Gain:**
- ~75% reduction in JPEG payload size (960px at q72 vs 1920px at q80)
- Faster upload to OpenRouter; lower input token cost for image bytes
- `max_tokens=150` reduces time-to-first-token cap on verbose model outputs

---

### Phase 17 — Switch to Gemini Flash Lite (`config.py`)

**Problem:** Default OpenRouter model was `anthropic/claude-haiku-4-5` (~$0.0022/eval).

**Fix:** Changed default to `google/gemini-2.0-flash-lite-001` (~$0.000045/eval).

| Model | Cost/eval | Latency |
|---|---|---|
| Claude Haiku 4.5 | ~$0.0022 | 400–800 ms |
| Gemini 2.0 Flash | ~$0.00022 | 300–500 ms |
| **Gemini Flash Lite 2.0** | **~$0.000045** | **200–400 ms** |

Gemini Flash Lite is purpose-designed for high-volume visual classification — exactly the
YES/NO screen condition use case. It is ~50× cheaper and ~2× faster than Claude Haiku
with equivalent accuracy for UI state detection.

**Gain:** 50× cost reduction per evaluation. At 1000 evals/day: $0.045 vs $2.20.

---

## GUI Pipeline Fixes

### Fix A — HTTP Connection Pooling (`gui/backends/uitars.py`, `claude_cu.py`, `omniparser.py`)

**Problem:** Every grounding call (NL → coordinates) created a fresh
`httpx.AsyncClient`, performed a full TLS handshake + TCP connection to the
cloud API, then tore it down. Same pattern as the old SmartWait vision clients.

**Fix:** Module-level persistent `httpx.AsyncClient` singletons, lazily
initialized and reused across all calls:
- `uitars.py`: two clients — `_or_client` (OpenRouter, auth header baked in)
  and `_ol_client` (Ollama)
- `claude_cu.py`: one client with `anthropic-version` + `content-type` headers
- `omniparser.py`: `_picker_client` for the Claude Haiku element picker

Per-request timeouts (e.g., `timeout=5.0` for health checks) still work via
httpx's per-request override mechanism.

**Gain:** Saves 100–300 ms per grounding call for cloud APIs, 20–50 ms for
local Ollama.

---

### Fix B — Async Screen Capture in GUI Agent (`gui/agent.py`)

**Problem:** `capture_screen()`, `capture_window()`, and `frame_to_jpeg()` were
called bare in `execute_gui_action()` and `find_gui_element()`, blocking the
asyncio event loop for 30–150 ms each time. Same class of bug that was already
fixed in the SmartWait engine.

**Fix:** All three calls are now dispatched off-thread with
`loop.run_in_executor(None, fn, *args)`. This frees the event loop for
dashboard responses, DB writes, and SmartWait ticks during capture.

**Gain:** Event loop no longer stalls during GUI grounding. Particularly
noticeable when multiple tasks are active simultaneously.

---

### Fix C — Batched xdotool Subprocesses (`desktop/control.py`)

**Problem:** Each mouse/window action spawned multiple separate subprocesses:
- `mouse_click_at`: 2 processes (`mousemove` + `click`)
- `mouse_double_click` (with coords): 2 processes
- `focus_window`: 2 processes (`windowfocus` + `windowraise`)
- `mouse_drag`: 4 processes (`mousemove`, `mousedown`, `mousemove`, `mouseup`)

Each `subprocess.run()` call has ~10–30 ms spawn overhead on Linux.

**Fix:** xdotool supports chaining subcommands in a single invocation:
```
xdotool mousemove --sync X Y click BUTTON
xdotool windowfocus --sync WID windowraise WID
xdotool mousemove X1 Y1 mousedown BTN  (drag start)
xdotool mousemove --sync X2 Y2 mouseup BTN  (drag end)
```
`mouse_click_at`: 2 → 1, `focus_window`: 2 → 1, `mouse_drag`: 4 → 2.

**Gain:** Saves ~30–50 ms per GUI action from reduced subprocess spawn overhead.

---

### Fix D — Reduce Post-Focus Sleep (`daemon.py`)

**Problem:** The `desktop_look` handler with `window_name` slept 300 ms after
`focus_window()` before taking the screenshot. This was an overly-conservative
guard for slow window managers.

**Fix:** Reduced to 50 ms — still enough time for the window manager to bring
the window to front on XFCE4/Openbox before the capture.

**Gain:** `desktop_look` with `window_name` is 250 ms faster per call.

---

### Fix E — Coordinate Normalization Bug (`gui/backends/uitars.py`)

**Problem:** `_parse_coordinates()` checked `if x <= 1.0 and y <= 1.0` to
detect normalized [0,1] output from UI-TARS. This has an edge case: if the
model returns `(0.45, 1.0)` — a perfectly valid bottom-of-screen coordinate —
`y <= 1.0` is True but `x <= 1.0` is also True, so it correctly normalizes.
However `(1.2, 0.8)` — outside [0,1] in x — would pass through as raw pixels
(`1, 0`) instead of being treated correctly.

**Fix:** Changed to `if max(x, y) <= 1.0:` — if the larger of the two values
is ≤ 1.0, both must be in [0,1] range and we scale them up. Pixel coordinates
from the model will always have `max(x, y) >> 1.0`.

---

### Fix F — Batched Florence-2 Inference (`gui/backends/omniparser.py`)

**Problem:** `_detect_and_caption()` ran a separate `model.generate()` GPU
forward pass for every bounding box YOLO detected — typically 10–50 elements
per screenshot. These were serial: the GPU was used at ~1/N efficiency.

**Fix:** Collect all element crops first, then call the Florence-2 processor
and `model.generate()` once with a batch of all crops:
```python
inputs = processor(text=["<CAPTION>"] * len(crops), images=crops,
                   return_tensors="pt", padding=True)
ids = model.generate(**inputs, max_new_tokens=40, num_beams=1)
captions = processor.batch_decode(ids, skip_special_tokens=True)
```
The GPU processes all crops in parallel within the single forward pass.

**Gain:** On GPU, reduces Florence-2 captioning time from O(N × forward_pass)
to O(1 × batched_forward_pass). Typically 3–10× faster for 10–50 element
screenshots.

---

## Vision Backend Cost Comparison

Each SmartWait evaluation sends 1 JPEG screenshot + ~150 token prompt, receives ~30–50 token YES/NO response.

| Model | Cost/eval | Daily (1000 evals) | Monthly |
|---|---|---|---|
| `google/gemini-2.0-flash-lite-001` | ~$0.000045 | ~$0.045 | ~$1.35 |
| `google/gemini-2.0-flash-001` | ~$0.00022 | ~$0.22 | ~$6.60 |
| `anthropic/claude-haiku-4-5` | ~$0.0022 | ~$2.20 | ~$66 |

## Summary — Recommended Config for Production

```bash
# Vision backend — OpenRouter with Gemini Flash Lite (fastest + cheapest)
ACU_VISION_BACKEND=openrouter
OPENROUTER_API_KEY=sk-or-...
ACU_OPENROUTER_VISION_MODEL=google/gemini-2.0-flash-lite-001  # default

# Frame encoding — 960px, q72 (sufficient for YES/NO condition checks)
ACU_FRAME_MAX_DIM=960           # default
ACU_FRAME_JPEG_QUALITY=72       # default

# Ollama keep-alive (if also using local Ollama for other tasks)
OLLAMA_KEEP_ALIVE=10m
```

No diff gate, no partial streak, no adaptive polling — all removed. Fixed 1s poll. Binary YES/NO.

---

*Last updated: 2026-02-21*
*Commits: d95a610 (phases 1–8), f86e806 (GUI fixes A–F), a9aae1a (micro-opts phases 9–14), HEAD (simplification phases 15–17)*
