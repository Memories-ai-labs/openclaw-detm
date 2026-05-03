# Development Guide

## Repository Structure

```
openclaw-memoriesai/
├── src/agentic_computer_use/    # Python package
│   ├── server.py                # MCP server (stateless stdio proxy)
│   ├── daemon.py                # Persistent HTTP daemon (:18790)
│   ├── config.py                # All configuration (env vars)
│   ├── task/                    # Task manager + SQLite models
│   ├── wait/                    # Smart Wait engine
│   ├── gui_agent/               # legacy single-shot grounding (UI-TARS, Claude CU, OmniParser) — used by desktop_action only
│   ├── live_ui/                 # multi-step gui_agent loop (the production gui_agent MCP path)
│   │   ├── bash_backend.py      # PRODUCTION: single LLM (openai/gpt-5.4) + raw bash. Default.
│   │   ├── openrouter.py        # Legacy supervised: Gemini Flash + UI-TARS grounder. ACU_LIVE_UI_BACKEND=supervised
│   │   ├── actions.py           # xdotool action execution (used by supervised)
│   │   └── session.py           # Session recording
│   ├── capture/                 # Screen capture (Xlib, PIL)
│   ├── vision/                  # Vision backends (Ollama, vLLM, Claude, OpenRouter)
│   ├── mavi.py                  # MAVI video intelligence
│   └── dashboard/               # Web dashboard (HTML/JS served by daemon)
├── skill/SKILL.md               # OpenClaw skill (behavioral instructions)
├── plugins/detm-tool-logger/    # OpenClaw plugin (tool logging)
├── openclaw/                    # OpenClaw integration files
│   ├── agents/                  # Sub-agent definitions
│   └── MEMORY-fragment.md       # Legacy memory injection (deprecated)
├── docker/                      # Docker deployment
│   ├── Dockerfile
│   ├── entrypoint.sh
│   └── detm-docker.sh           # Container management helper
├── benchmarks/                  # OSWorld, ScreenSpot-Pro benchmarks
├── scripts/                     # Dev/debug scripts
├── install.sh                   # One-line installer
└── pyproject.toml               # Python package config
```

## Local Development

### Prerequisites

- Python 3.11+
- Linux with X11 (or Xvfb)
- xdotool, scrot, ffmpeg
- `OPENROUTER_API_KEY` for cloud vision/grounding

### Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -e .
```

### Running the daemon

```bash
# Debug mode with verbose logging
ACU_DEBUG=1 DISPLAY=:99 PYTHONPATH=src .venv/bin/python3 -m agentic_computer_use.daemon

# Or use dev.sh if available
./dev.sh start
./dev.sh logs     # tail debug log
./dev.sh stop
```

The daemon starts on `http://127.0.0.1:18790`. Dashboard at `/dashboard`.

### Running the MCP server standalone

```bash
DISPLAY=:99 PYTHONPATH=src .venv/bin/python3 -m agentic_computer_use.server
```

This is what OpenClaw launches via `openclaw mcp set`. It communicates over stdio (JSON-RPC).

### Testing gui_agent

```bash
PYTHONPATH=src .venv/bin/python3 scripts/test_live_ui.py
```

### Inspecting gui_agent sessions

```bash
# List recent sessions
PYTHONPATH=src .venv/bin/python3 scripts/inspect_session.py --list

# View specific session
PYTHONPATH=src .venv/bin/python3 scripts/inspect_session.py --session <session_id>
```

## Docker Development

### Build

```bash
docker build -t detm:latest -f docker/Dockerfile .

# Or use the helper:
./docker/detm-docker.sh build
```

### Run

```bash
OPENROUTER_API_KEY=sk-or-... ./docker/detm-docker.sh start

# Access:
#   Dashboard: http://127.0.0.1:18790/dashboard
#   noVNC:     http://127.0.0.1:6080/vnc.html
#   Health:    curl http://127.0.0.1:18790/health
```

### Develop with live code

Mount the source directory for live-reload development:

```bash
docker run -d --name detm \
    -p 18790:18790 -p 6080:6080 \
    -v $HOME/.agentic-computer-use:/data \
    -v $(pwd)/src:/opt/detm/src \
    -e OPENROUTER_API_KEY=$OPENROUTER_API_KEY \
    detm:latest
```

Changes to `src/` are reflected immediately (restart the daemon process inside the container if needed).

### Shell into container

```bash
./docker/detm-docker.sh shell
# or
docker exec -it detm bash
```

## Key Code Paths

### Tool call flow

```
OpenClaw → MCP server (server.py) → HTTP POST → daemon.py handler → business logic → response
```

Every tool defined in `server.py` maps to a daemon HTTP endpoint. The mapping is in `TOOL_ROUTES`.

### gui_agent flow (production: bash backend)

```
gui_agent(instruction)
  → daemon.handle_gui_agent()
  → live_ui/__init__.get_provider() picks backend from ACU_LIVE_UI_BACKEND (default 'bash')
  → bash_backend.BashGuiProvider.run() — single-LLM loop:
    ├─ Capture screenshot → POST /chat/completions with tools=[bash, screenshot, done, partial, failed, escalate]
    ├─ Model emits bash(command="xdotool key Ctrl+l; xdotool type 'https://...'; xdotool key Return")
    ├─ Daemon executes via subprocess.Popen + os.killpg on timeout → returns stdout/stderr/exit_code
    ├─ Capture fresh screenshot, append to messages, repeat
    ├─ Model emits done(summary="...") | partial(...) | failed(...) | escalate(...)
    └─ Return result to daemon → MCP server → OpenClaw
```

### gui_agent flow (legacy: supervised backend, ACU_LIVE_UI_BACKEND=supervised)

```
gui_agent(instruction)
  → daemon.handle_gui_agent()
  → openrouter.run() — Gemini supervisor + UI-TARS grounder:
    ├─ Capture screenshot → send to Gemini with tool definitions
    ├─ Gemini calls move_to(target="...")
    │   → _refine_cursor() → UI-TARS grounding + iterative narrowing + convergence check
    │   → smooth mouse move (no click)
    │   → capture screenshot with cursor visible → send to Gemini
    ├─ Gemini calls click() → xdotool at current cursor position
    ├─ Gemini calls type_text(text="...") → xdotool type
    ├─ Gemini calls done(success=True, summary="...")
    └─ Return result to daemon → MCP server → OpenClaw
```

### Smart Wait flow

```
smart_wait(condition, target)
  → daemon creates wait job in DB
  → wait engine polls at 1-2s intervals:
    ├─ Capture frame (Xlib → JPEG)
    ├─ Pixel diff gate (skip if < 1% change)
    ├─ Vision backend: "Is this condition met? YES/NO"
    ├─ If YES with sufficient confidence → resolve
    └─ If timeout → resolve with timeout status
  → Wake event sent to OpenClaw
```

## Making Changes

### Adding a new tool

1. Add tool definition in `server.py` (name, description, inputSchema)
2. Add route mapping in `server.py` `TOOL_ROUTES`
3. Add handler in `daemon.py`
4. Document in `skill/SKILL.md`

### Adding a new vision backend

1. Create module in `src/agentic_computer_use/vision/`
2. Implement `evaluate_condition(prompt, images)` and `check_health()`
3. Register in `vision/__init__.py` backend selector
4. Add config vars in `config.py`

### Modifying the dashboard

Dashboard files are in `src/agentic_computer_use/dashboard/`. The daemon serves them statically. Changes take effect on daemon restart.

## Configuration Reference

See `config.py` for all environment variables. Key ones:

| Variable | Purpose |
|----------|---------|
| `OPENROUTER_API_KEY` | Cloud vision, grounding, and gui_agent supervisor |
| `ACU_OPENROUTER_LIVE_MODEL` | Gemini model for gui_agent supervisor (default: `google/gemini-3-flash-preview`) |
| `ACU_UITARS_OPENROUTER_MODEL` | UI-TARS model for grounding (default: `bytedance/ui-tars-1.5-7b`) |
| `ACU_VISION_BACKEND` | SmartWait backend: ollama, openrouter, claude, vllm, passthrough |
| `ACU_DEBUG` | Enable verbose debug logging |
| `ACU_DATA_DIR` | Data directory (default: `~/.agentic-computer-use`) |
| `DISPLAY` | X11 display (default: `:99`) |

## Releasing Updates

### For Docker users

1. Make changes and test locally
2. Rebuild: `./docker/detm-docker.sh build`
3. Restart: `./docker/detm-docker.sh restart`

### For bare-metal users

1. `git pull` in the repo
2. `.venv/bin/pip install -e .` (if deps changed)
3. `sudo systemctl restart detm-daemon`

### For OpenClaw skill updates

Skill changes (`skill/SKILL.md`) propagate automatically if symlinked. Signal OpenClaw to reload:
```bash
kill -HUP $(pgrep -f openclaw-gateway)
```
