# Roadmap

> **Production status (May 2026):** the supervised `gui_agent` pipeline (Phases 2–5) shipped and was the production default through April. Post-bake-off (`experiments/holo3-vs-flagships-2026-04-25/`), production switched to the **bash** backend with `openai/gpt-5.4` — `ACU_LIVE_UI_BACKEND=bash` is now the default. The supervised pipeline remains in the codebase as a fallback. See `docs/GUI-AGENT-BACKENDS.md`.

## Phase 1: Architecture & Research ← DONE

- [x] Problem definition
- [x] Research existing solutions (Claude Computer Use, UI-TARS, AgentRR, Kairos, MiniCPM-o)
- [x] Architecture design
- [x] Tool specifications
- [x] Smart Wait detailed design
- [x] Task Memory detailed design
- [x] Procedural Memory detailed design
- [ ] Alex review & feedback

## Phase 2: Smart Wait MVP ← DONE

**Goal**: Agent can call `smart_wait()` and get woken when a visual condition is met.

### 2a: Infrastructure
- [x] Set up Python project structure (MCP server skeleton)
- [x] Xvfb setup (headless Ubuntu server — :99 with XFCE4 WM)
- [x] Screen capture module (X11 via python-xlib, 67ms/frame)
- [x] OpenRouter backend (Gemini Flash Lite, ~$0.000045/eval — replaces local GPU model)

### 2b: Core Wait Loop
- [x] Wait job queue (SQLite-backed)
- [x] Fixed 1s poll — binary YES/NO, no diff gate, no adaptive logic, no context window
- [x] Timeout handling
- [x] Parallel job evaluation (asyncio.gather, per-display capture locks)

### 2c: OpenClaw Integration
- [x] MCP server exposing `smart_wait` tool (+ 6 more tools)
- [x] Wake mechanism (`openclaw system event --mode now`)
- [x] Register MCP server with OpenClaw via `openclaw mcp set`
- [x] End-to-end test: Chrome detection in 1.3-1.8s

### 2d: PTY Handling (vision-only)
- [x] Removed terminal regex/text fast path
- [x] Unified PTY and GUI waits under the same vision evaluator
- [ ] Optional future improvement: bind `pty:<session_id>` to a specific terminal window capture for cleaner frames

## Phase 3: Task Memory ← DONE

**Goal**: Agent can register tasks, report progress, and query state across context boundaries.

### 3a: Task CRUD
- [x] SQLite schema + migrations
- [x] task_register, task_update, task_list tools
- [x] Message history append + retrieval
- [x] Hierarchical model: Task → Plan Items → Actions → Logs

### 3b: Distillation
- [x] On-demand query answering (via vision backend)
- [x] Plan progress heuristic tracking

### 3c: Integration
- [x] Link smart_wait to tasks (auto-post on resolution)
- [x] SKILL.md narration guidance teaches agent to register tasks and narrate progress
- [x] Compact-with-focused-expansion context for task resumption (active item expanded, others compact)
- [ ] Test: multi-step deployment with context compaction mid-task

## Phase 4: Video Comprehension (Memories AI)

**Goal**: Agent can record and analyze video clips via Memories AI — both for immediate understanding and for building a library of procedural knowledge.

### 4a: Screen Recording Module ← DONE
- [x] On-demand video recording (ffmpeg + Xvfb, target window or full screen)
- [x] Configurable duration, FPS, resolution
- [x] Cleanup of ephemeral recordings

### 4b: Memories AI Integration
- [ ] API client (upload video + prompt, get analysis)
- [ ] Auth configuration (API key)
- [ ] Error handling + retry logic

### 4c: video_understand (sync mode)
- [ ] Record → upload → block until response → return as tool result
- [ ] Ephemeral: video discarded after analysis
- [ ] Timeout handling for slow API responses

### 4d: video_record (async mode)
- [ ] Record → upload → return immediately
- [ ] Background analysis → wake OpenClaw via `system event` when done
- [ ] Save analysis in Memories AI index with tags

### 4e: video_search
- [ ] Search previously saved video analyses
- [ ] Agent checks "have I recorded this before?" before re-learning workflows

## Phase 5: Visual OS Control ← DONE

**Goal**: Agent can interact with native desktop applications, not just browser and CLI.

- [x] xdotool integration for mouse/keyboard
- [x] Window management (focus, resize, move, close, list)
- [x] Desktop screenshot → action pipeline (`desktop_look`)
- [ ] Combine with procedural memory for efficient UI navigation

## Phase 6: Dashboard & Observability ← DONE

**Goal**: Human can observe and control tasks in real time.

- [x] Web dashboard served by daemon (no separate process)
- [x] Task list sidebar with status badges and progress bars
- [x] Expandable task tree with nested actions, screenshots, lightbox
- [x] Rich action logs: GUI screenshots, coordinates, confidence scores, backend/model used
- [x] Live polled JPEG screen view per task (2 fps)
- [x] Replay mode — scrub through recorded frames frame-by-frame
- [x] Color-coded message feed (agent narration, wait events, system messages)
- [x] Task control buttons (Pause, Resume, Cancel) — human can free up the LLM
- [x] Recording controls (start/stop per task, elapsed timer)
- [x] Video export — on task complete/cancel, frames encoded to H.264 MP4 via ffmpeg; download button in sidebar
- [x] Shared system display (single :99, visible in VNC + dashboard)
- [x] Stuck detection + automatic LLM wake with resume packets

## Phase 7.5: Pipeline Performance Optimization ← DONE

**Goal**: Reduce latency throughout the SmartWait and capture pipeline.

- [x] HTTP connection pooling — persistent `httpx.AsyncClient` in all vision backends (saves 100–300 ms/call)
- [x] OpenRouter vision backend — cloud alternative, no GPU required; Gemini 2.0 Flash 10× cheaper than Claude Haiku
- [x] Async frame capture — `capture_screen()` + `frame_to_jpeg()` off-thread via `run_in_executor`; event loop stays responsive
- [x] Parallel job evaluation — all overdue wait jobs evaluated concurrently via `asyncio.gather()`; Xlib serialized by `_CAPTURE_LOCK`
- [x] Ollama `keep_alive` default `"0"` → `"10m"` — eliminates 5–30 s cold-start between calls
- [x] Pixel-diff downsampling — diff computed on 320 px wide frame instead of full 1920 px (~30× faster)
- [x] Async subprocess for OpenClaw wake — `asyncio.create_subprocess_exec` replaces blocking `subprocess.run`
- [x] Dashboard frame buffer rate — 4 fps → 2 fps (halved Xlib load)

See `docs/PERFORMANCE-OPTIMIZATION.md` for full before/after measurements and cost analysis.

## Phase 7: Context Compaction & LLM Guidance ← DONE

**Goal**: Agent survives context loss and resumes effectively.

- [x] `build_resume_packet()` with active item expansion (actions + logs)
- [x] `detail_level="focused"` — compact view with only active item expanded
- [x] SKILL.md narration section — teaches agent to write reasoning to task history
- [x] Stuck detection loop (60s interval, 5min threshold, cooldown)
- [x] `[task_stuck_resume]` wake event with full resume context

## Phase 8: Cursor Control ← DONE

**Goal**: Eliminate coordinate hallucination from the main LLM; improve GUI grounding accuracy on small targets.

- [x] Remove raw coordinate input from `gui_agent` — NL only, no `click(x, y)` passthrough
- [x] Fix `_parse_coordinates()` in UI-TARS backend to use actual image dimensions (was hardcoded 1920×1080)
- [x] Iterative narrowing — 3-pass zoom: full frame → 300px crop → 150px crop (`_NARROW_RADII`)
- [ ] ShowUI-π style visuomotor policy for drag trajectories — **blocked: weights not released**

See `docs/CURSOR-CONTROL.md` for research landscape, training data, and roadmap.

---

## Future Ideas

- **Multi-machine federation**: Share procedural memory across devices
- **Community experience store**: AgentRR-style shared knowledge base
- **Voice narration**: Agent narrates what it's doing (MiniCPM-o supports speech)
- **Proactive suggestions**: "I noticed you do X every Monday — want me to automate it?"
- **Recording playback UI**: Web interface to browse and annotate screen recordings

## Tech Stack

| Component | Technology | Rationale |
|-----------|-----------|-----------|
| MCP Server | Python (`mcp[cli]`) | Best MCP SDK support, rich ML ecosystem |
| Database | SQLite | Zero-config, single-file, sufficient scale |
| Vision Model | Gemini 2.0 Flash Lite via OpenRouter | Cloud, ~$0.000045/eval, no GPU required |
| Virtual Display | Xvfb | Headless server — no physical monitor |
| Screen Capture | ffmpeg + python-xlib | Reads from Xvfb virtual framebuffer |
| Video Indexing | Memories AI API | Alex's company, advanced video comprehension |
| Pixel Diff | NumPy | Fast, minimal dependency |
| Wake Mechanism | `openclaw system event --mode now` | Direct CLI injection, seconds latency |
| Web Dashboard | aiohttp static + vanilla JS | Zero dependencies, served by daemon |
| Display Isolation | Xvfb per task | Each task gets its own virtual screen |

## Hardware Requirements

**Minimum (no GPU — default config):**
- 4 GB RAM
- Any modern x86_64 CPU
- `OPENROUTER_API_KEY` — all vision and GUI grounding via cloud (~$0.00005/eval)
- 500 MB disk for daemon + DB

**With local GPU (optional):**
- NVIDIA GPU with 8+ GB VRAM for running UI-TARS or vision model locally
- 16 GB system RAM
- `ACU_GUI_AGENT_BACKEND=uitars` + `ACU_VISION_BACKEND=ollama`
