# Architecture

> **Production status (May 2026):** DETM's `gui_agent` runs on the **bash backend** by default — a single LLM (`openai/gpt-5.4` via OpenRouter) sees screenshots and writes raw `xdotool`/`wmctrl`/`scrot` commands directly. The **supervised** backend (Gemini Flash supervisor + UI-TARS grounder) described in some sections below is still in the codebase as `ACU_LIVE_UI_BACKEND=supervised` for benchmark reproducibility, but is no longer the default. See `docs/GUI-AGENT-BACKENDS.md` for the current backend reference and `docs/SCREENSHOT-CUA-MODELS.md` for the broader pixel-only CUA model landscape.

## Overview

**agentic-computer-use** is a Desktop Environment Task Manager (DETM) — an MCP server with a persistent HTTP daemon that provides hierarchical task tracking, smart visual waiting, GUI automation with natural language grounding, pluggable vision backends, and real-time live UI delegation.

```
OpenClaw LLM → DETM (task hierarchy) → Vision + GUI Agent → Desktop (:99)
```

Five layers:
1. **Task Management** — hierarchical: Task → Plan Items → Actions → Logs
2. **Smart Wait** — vision-based condition polling with adaptive intervals
3. **GUI Agent** — Gemini Flash supervisor + UI-TARS grounding (unified tool: `gui_agent`)
4. **Vision** — pluggable backends (Ollama, vLLM, Claude, OpenRouter, passthrough)
5. **Display** — single shared display `:99` (XFCE desktop, visible via VNC)

## Design Principles

**CLI-first on a bare desktop.** DETM runs on a headless Linux VM with a minimal XFCE desktop — no curated dock, no pinned apps, sparse desktop icons. The GUI is not designed for visual app discovery. Since it's just Linux, every application can be launched instantly from the command line. The agent should always launch apps via CLI and only use `gui_agent` for UI interaction within already-open applications. See `skill/SKILL.md` for the full rule and common launch commands.

**gui_agent for interaction, not navigation.** `gui_agent` (Gemini + UI-TARS) excels at clicking buttons, filling forms, and navigating within an app. It's slow and unreliable at finding and launching apps on a bare desktop. The split: CLI handles orchestration (launching, file ops, data processing), gui_agent handles visual interaction (forms, dialogs, web pages).

**Observe → Act → Verify.** Every desktop action follows the pattern: `desktop_look` to understand state, `gui_agent`/`desktop_action` to act, then verify the result. But avoid unnecessary screenshots — trust `gui_agent`'s built-in verification for routine actions.

## System Architecture

```
┌──────────────────────────────────────────────────────────────┐
│                    OpenClaw Gateway                          │
│                                                              │
│  MCP tools available to the model:                           │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────────────┐ │
│  │ Task Mgmt    │ │ Smart Wait   │ │ GUI Agent            │ │
│  │ task_register│ │ smart_wait   │ │ gui_agent (unified)  │ │
│  │ task_summary │ │ wait_status  │ │                      │ │
│  │ task_update  │ │ wait_update  │ │ Desktop              │ │
│  │ task_item_*  │ │ wait_cancel  │ │ desktop_action       │ │
│  │ task_log_*   │ │              │ │ desktop_look         │ │
│  │ task_drill_* │ │              │ │ video_record         │ │
│  └──────┬───────┘ └──────┬──────┘ └──────────┬───────────┘ │
└─────────┼────────────────┼───────────────────┼─────────────┘
          │     MCP (stdio)                    │
          ▼                ▼                   ▼
┌──────────────────────────────────────────────────────────────┐
│            agentic-computer-use MCP server                   │
│            (thin proxy → daemon HTTP calls)                  │
└──────────────────────┬───────────────────────────────────────┘
                       │ HTTP (127.0.0.1:18790)
                       ▼
┌──────────────────────────────────────────────────────────────┐
│                   DETM Daemon (persistent)                   │
│                                                              │
│  ┌────────────────────┐  ┌───────────────────────────────┐  │
│  │  Task Manager       │  │  Wait Engine                  │  │
│  │                     │  │                               │  │
│  │  tasks              │  │  Wait job queue               │  │
│  │  ├── plan_items     │  │  1s fixed poll                │  │
│  │  │   ├── actions    │  │  Binary YES/NO eval           │  │
│  │  │   │   └── logs   │  │  Condition eval (via Vision)  │  │
│  │  │   └── ...        │  │  Wake dispatch → OpenClaw     │  │
│  │  └── ...            │  │                               │  │
│  └─────────┬──────────┘  └──────────┬────────────────────┘  │
│            │                        │                        │
│  ┌─────────▼────────────────────────▼────────────────────┐  │
│  │              Vision Backend (pluggable)                │  │
│  │                                                       │  │
│  │  ┌──────────┐ ┌──────────┐ ┌────────┐ ┌───────────┐ ┌────────────┐ │  │
│  │  │ Ollama   │ │ vLLM     │ │ Claude │ │Passthrough│ │ OpenRouter │ │  │
│  │  │(default) │ │(UI-TARS) │ │ (API)  │ │ (no eval) │ │(cloud,any) │ │  │
│  │  └──────────┘ └──────────┘ └────────┘ └───────────┘ └────────────┘ │  │
│  └───────────────────────────────────────────────────────┘  │
│                                                              │
│  ┌──────────────────┐  ┌──────────────────────────────────┐ │
│  │  GUI Agent        │  │  Desktop Control                 │ │
│  │                   │  │                                  │ │
│  │  NL → grounding   │  │  xdotool: click, type, keys     │ │
│  │  ┌────────────┐   │  │  X11/Xvfb screen capture        │ │
│  │  │ UI-TARS    │   │  │  Window management               │ │
│  │  │ Claude CU  │   │  │  Video recording                 │ │
│  │  │ Direct     │   │  │                                  │ │
│  │  └────────────┘   │  │                                  │ │
│  └──────────────────┘  └──────────────────────────────────┘ │
│                                                              │
│  ┌──────────────────┐  ┌──────────────────────────────────┐ │
│  │  Display Manager  │  │  Stuck Detection Loop            │ │
│  │                   │  │                                  │ │
│  │  Shared :99       │  │  60s check interval              │ │
│  │  display          │  │  Wake OpenClaw via system event  │ │
│  │  Xlib caching     │  │  Resume packet with context      │ │
│  └──────────────────┘  └──────────────────────────────────┘ │
│                                                              │
│  ┌──────────────────┐  ┌──────────────────────────────────┐ │
│  │  SQLite Database  │  │  Web Dashboard (:18790/dashboard)│ │
│  │  ~/.agentic-      │  │                                  │ │
│  │  computer-use/    │  │  Task tree + message feed        │ │
│  │  data.db          │  │  Live MJPEG screen stream        │ │
│  │                   │  │  Task controls (cancel/pause)    │ │
│  └──────────────────┘  └──────────────────────────────────┘ │
│                                                              │
│  ┌──────────────────────────────────────────────────────────┐│
│  │  Debug Log — ~/.agentic-computer-use/logs/debug.log      ││
│  └──────────────────────────────────────────────────────────┘│
└──────────────────────────────────────────────────────────────┘
```

## Component Details

### MCP Server Layer

The MCP server (`server.py`) is a thin proxy. It defines tool schemas and forwards all calls to the persistent daemon over HTTP. OpenClaw launches it via stdio transport.

The daemon (`daemon.py`) runs independently on port 18790 and maintains state across MCP server restarts.

### Task Manager (Hierarchical)

Data model:

```
Task
├── id, name, status, metadata, created_at, updated_at
└── PlanItem[] (ordered by ordinal)
    ├── ordinal, title, status, started_at, completed_at, duration_seconds
    └── Action[]
        ├── action_type (cli|gui|wait|vision|reasoning|other)
        ├── summary, status, input_data, output_data, duration_ms
        └── ActionLog[]
            └── log_type, content, created_at
```

**Default view** is item-level: plan items with status + action counts. Drill-down expands individual items to show actions and logs.

**Status flow**:
- Task: active → paused/completed/failed/cancelled
- Plan Item: pending → active → completed/failed/skipped

### Smart Wait Engine

Vision-based async monitoring. The LLM delegates a visual wait, the daemon monitors, and wakes the LLM when the condition is met.

**Frame capture pipeline**:
```
Target (window or screen)
  │
  ▼  [run_in_executor — Xlib off-thread, serialized per display by _CAPTURE_LOCKS]
Frame Grabber → JPEG encode (960px max, quality 72) → Vision Backend (YES/NO) → Decision
                [run_in_executor — PIL off-thread]    persistent HTTP client,
                                                       no reconnect per call
```

**Adaptive poll**: base interval is 2s (configurable via `poll_interval`). Speeds up to 1s on a "partial" verdict, slows to 4s after many static frames. All overdue jobs evaluated concurrently via `asyncio.gather()`. `_resolve_job` / `_timeout_job` guard against double-resolution.

### Vision Backend (Pluggable)

Configured via `ACU_VISION_BACKEND`:

| Backend | Model | Use Case | Cost |
|---------|-------|----------|------|
| `ollama` (default) | Configurable (`ACU_VISION_MODEL`, default `minicpm-v`) | Local, free | Free (GPU) |
| `vllm` | UI-TARS-1.5-7B, Qwen, etc. | Best accuracy for grounding + wait | Free (GPU) |
| `claude` | Claude via Anthropic API | Zero-GPU fallback | ~$0.002/eval |
| `openrouter` | Any model (Gemini Flash, Haiku, etc.) | Cloud, no GPU, cheapest option | ~$0.0002/eval |
| `passthrough` | None | Debug — no evaluation | Free |

All backends implement `evaluate_condition(prompt, images)` and `check_health()`.

All backends use a **persistent `httpx.AsyncClient`** (module-level singleton) so TLS connections are reused across calls — saves 100–300 ms per cloud call.

**Default recommendation:** `google/gemini-2.0-flash-lite-001` (~$0.000045/eval) — designed for high-volume image classification, ideal for binary YES/NO screen polling. Set `OPENROUTER_API_KEY` + `ACU_VISION_BACKEND=openrouter`. Step up to `google/gemini-2.0-flash-001` (~$0.00022/eval) if you need stronger OCR or complex scene understanding.

### GUI Agent (NL Grounding)

Natural language → screen coordinates → xdotool execution.

```
"click the Export button"
  │
  ▼  [run_in_executor — Xlib/PIL off-thread, same pattern as SmartWait]
Screenshot capture → Grounding backend → (x, y) coordinates → xdotool click
                     (persistent HTTP client,
                      no TLS reconnect per call)
```

Four backends (`ACU_GUI_AGENT_BACKEND`):

| Backend | Model | Accuracy | Cost |
|---------|-------|----------|------|
| `uitars` | UI-TARS-1.5-7B (OpenRouter cloud or Ollama local) | 61.6% ScreenSpot-Pro | ~$0.0003/call or free |
| `claude_cu` | Claude computer_use API | ~27.7% | API cost |
| `omniparser` | YOLO + Florence-2 + Claude Haiku picker | High for icon/element grounding | Free (GPU) + Haiku per pick |
| `direct` | None (coords required) | N/A | Free |

The `uitars` backend auto-selects mode: if `OPENROUTER_API_KEY` is set, it uses OpenRouter's hosted model (fast, no GPU needed). Otherwise it uses Ollama with per-request `keep_alive` (default 5m) so UI-TARS auto-unloads from VRAM after idle, freeing space for minicpm-v smart_wait polls.

All backends use a **persistent `httpx.AsyncClient`** (module-level singleton) — no TLS reconnect per grounding call. The `uitars` backend maintains two clients (one for OpenRouter, one for Ollama). `omniparser`'s Claude Haiku picker also uses a persistent client.

`gui_agent` accepts **natural language only** — never raw coordinates. The instruction goes through iterative narrowing (3 passes: full frame → 300px crop → 150px crop) for precision on small targets. Use `desktop_action` for pixel-exact control.

**OmniParser pipeline** (`omniparser` backend):
```
screenshot → YOLO (detect bounding boxes)
           → Florence-2 batch caption all crops in one GPU forward pass
           → numbered overlay image
           → Claude Haiku: "which element matches the description?" → N
           → center of box N → (x, y)
```

### Desktop Control

`xdotool`-based execution layer for X11/Xvfb. Commands are **chained** in single subprocess calls to reduce spawn overhead:
- `mouse_click_at(x, y)` → `xdotool mousemove --sync x y click 1` (1 subprocess, was 2)
- `focus_window(wid)` → `xdotool windowfocus --sync wid windowraise wid` (1 subprocess, was 2)
- `mouse_drag(x1,y1, x2,y2)` → 2 subprocesses (was 4)
- Keyboard: type text, press key combos
- Windows: list, find, resize, move, close
- Screen: capture, record video clips

### Display

All tasks share a **single display `:99`** (XFCE desktop, served via VNC). There is no per-task Xvfb isolation. `register_task()` sets `metadata["display"] = config.DISPLAY`. All tools that accept `task_id` resolve to `:99` via `_resolve_task_display()`.

- `get_xlib_display(display_str)` — cached Xlib connections for fast frame capture
- Frame buffer loop captures JPEG at ~4 fps into `_frame_buffer[display]` for instant snapshot serving

### Stuck Detection

Background loop in the daemon (`stuck_detection_loop()`, 60s interval) monitors active tasks:

1. Skip tasks with active smart waits (they're legitimately waiting)
2. If `now - task.updated_at >= 300s` and no active waits → task is stuck
3. Build a resume packet: task state, plan items, active item expanded with action details + logs, last 5 messages, wait state
4. Inject `[task_stuck_resume] {json}` into OpenClaw via `openclaw system event --mode now`
5. Cooldown: no duplicate alerts within 300s per task

### Web Dashboard

Built-in web UI served by the daemon at `/dashboard`. No separate process.

Components (`dashboard/components/`):
- **task-list.js** — sidebar with status badges, progress bars, **Live button** (pulsing when `gui_agent` is active), download button for completed recordings
- **task-tree.js** — expandable plan items → actions → logs with screenshots, coordinates, lightbox; shows which vision model/backend was used per action
- **screen-viewer.js** — polled JPEG live view (2 fps) + **replay mode** (scrub through recorded frames frame-by-frame)
- **live-session-viewer.js** — modal viewer for `gui_agent` sessions: replay mode (frame scrubber + audio player with bidirectional sync) and **live monitoring mode** (SSE event feed + real-time frame + PCM audio via Web Audio API)

Task controls in the tree header: **Pause**, **Resume**, **Cancel** buttons that POST status changes to the daemon.

**Frame recording & video export:** While a task is active the daemon captures JPEG frames at ~2 fps into `~/.agentic-computer-use/recordings/{task_id}/`. On task completion or cancellation, frames are encoded to H.264 MP4 (ffmpeg, CRF 28, 2 fps) and the raw frames are deleted. The MP4 is available for download via the dashboard. Deleting a task also deletes the video. Only failed tasks discard frames immediately (no video created).

### Database

SQLite via `aiosqlite`. Single file at `~/.agentic-computer-use/data.db`.

Tables: `tasks`, `plan_items`, `actions`, `action_logs`, `task_messages`, `wait_jobs`.

### Logging

Debug log at `~/.agentic-computer-use/logs/debug.log`. Enable with `ACU_DEBUG=1` or `--debug` flag. Both the human and Claude Code can tail the log:

```bash
./dev.sh logs          # live colored tail
tail -f ~/.agentic-computer-use/logs/debug.log
```

## Configuration

All environment variables use the `ACU_*` prefix:

| Variable | Default | Description |
|----------|---------|-------------|
| `ACU_VISION_BACKEND` | `ollama` | SmartWait vision backend: ollama, vllm, claude, openrouter, passthrough |
| `ACU_VISION_MODEL` | `minicpm-v` | Ollama vision model for SmartWait |
| `ACU_VLLM_URL` | `http://localhost:8000` | vLLM API endpoint |
| `ACU_VLLM_MODEL` | `ui-tars-1.5-7b` | vLLM model name |
| `ACU_CLAUDE_VISION_MODEL` | `claude-sonnet-4-20250514` | Claude vision model |
| `OPENROUTER_API_KEY` | (none) | OpenRouter API key — for vision, GUI-TARS grounding, and gui_agent |
| `ACU_OPENROUTER_VISION_MODEL` | `google/gemini-2.0-flash-lite-001` | OpenRouter model for SmartWait vision |
| `OLLAMA_KEEP_ALIVE` | `10m` | How long Ollama keeps vision model in VRAM between calls |
| `ACU_GUI_AGENT_BACKEND` | `direct` | GUI grounding: direct, uitars, claude_cu, omniparser |
| `ACU_UITARS_OLLAMA_MODEL` | `0000/ui-tars-1.5-7b` | Ollama model for local UI-TARS grounding |
| `ACU_UITARS_KEEP_ALIVE` | `5m` | Ollama keep_alive for UI-TARS (frees VRAM after idle) |
| `ACU_OPENROUTER_LIVE_MODEL` | `google/gemini-3-flash-preview` | OpenRouter gui_agent model |
| `ACU_DEBUG` | `0` | Enable verbose debug logging |
| `ACU_WORKSPACE` | (none) | Workspace directory for memory files |
| `DISPLAY` | `:99` | Shared X11 display (all tasks) |
| `OLLAMA_URL` | `http://localhost:11434` | Ollama API URL |
| `ANTHROPIC_API_KEY` | (none) | Required for Claude vision/GUI backends |
| `MAVI_API_KEY` | (none) | Required for mavi_understand (Memories.AI) |
| `ACU_UITARS_OPENROUTER_MODEL` | `bytedance/ui-tars-1.5-7b` | OpenRouter model for UI-TARS grounding |

## Deployment Modes

### 1. Docker (recommended, any OS)

Everything runs inside a single container: Xvfb virtual display, XFCE4 window manager, x11vnc, websockify/noVNC, and the DETM daemon. The host only needs Docker and the MCP server proxy.

```
┌─────────────────────── Host ───────────────────────┐
│                                                     │
│  OpenClaw Gateway                                   │
│    └─ MCP Server (stdio, thin HTTP proxy)           │
│         └─ HTTP → 127.0.0.1:18790 ──────┐          │
│                                          │          │
│  ┌───────── Docker Container ────────────┼────────┐ │
│  │                                       ▼        │ │
│  │  DETM Daemon (:18790)                          │ │
│  │    ├─ Task Manager + SQLite                    │ │
│  │    ├─ Smart Wait Engine                        │ │
│  │    ├─ GUI Agent (Gemini + UI-TARS)             │ │
│  │    └─ Web Dashboard (/dashboard)               │ │
│  │                                                │ │
│  │  Xvfb :99 ──→ XFCE4                           │ │
│  │    └─ x11vnc ──→ websockify (:6080)            │ │
│  │                                                │ │
│  │  Volume: /data (DB, screenshots, recordings)   │ │
│  └────────────────────────────────────────────────┘ │
│                                                     │
│  Published ports:                                   │
│    18790 → Daemon API + Dashboard                   │
│    6080  → noVNC (browser-based desktop viewer)     │
└─────────────────────────────────────────────────────┘
```

**Files:**
- `docker/Dockerfile` — Ubuntu 24.04 base with all system deps
- `docker/entrypoint.sh` — starts Xvfb, XFCE4, VNC, noVNC, daemon
- `docker/detm-docker.sh` — start/stop/status/logs/build/shell helper
- `.dockerignore` — excludes .venv, benchmarks, docs, etc.

**Quick start:**
```bash
./docker/detm-docker.sh build
OPENROUTER_API_KEY=sk-or-... ./docker/detm-docker.sh start
```

### 2. Bare-metal Linux (development / GPU)

Direct install on the host. Required for local Ollama/vLLM GPU inference. Uses systemd services for Xvfb, VNC, noVNC, and the daemon.

```bash
./install.sh
```

See `install.sh` for full setup: Python venv, system deps, systemd services, OpenClaw integration (MCP server, skill, plugin).

## OpenClaw Integration

DETM integrates with OpenClaw through four layers:

### 1. MCP Server (tool registration)

`server.py` is a stateless MCP server using stdio transport. OpenClaw launches it per-session. It defines 25+ tools and proxies all calls to the daemon at `http://127.0.0.1:18790`.

Registered via `install.sh` which runs:
```bash
openclaw mcp set agentic-computer-use '{"command":"python3","args":["-m","agentic_computer_use.server"],"env":{"DISPLAY":":99","PYTHONPATH":"/path/to/src"}}'
```

### 2. Skill (behavioral instructions)

`skill/SKILL.md` provides behavioral instructions that OpenClaw reads at session start. Contains hard rules (task registration, narration, verification), tool usage patterns, and examples. YAML frontmatter declares metadata.

### 3. Plugin (tool logging)

`plugins/detm-tool-logger/` hooks `before_tool_call` and `after_tool_call` to log non-DETM tool calls to the dashboard, making all agent activity visible.

### 4. Sub-agents

Specialized agents in `openclaw/agents/` handle domain-specific tasks (e.g., LinkedIn research). The stuck detection system can route resume packets to the correct sub-agent via `agent_id`.
