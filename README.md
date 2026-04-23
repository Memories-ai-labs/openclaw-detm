# agentic-computer-use

Desktop Environment Task Manager (DETM) — an MCP server that gives AI agents the ability to see and control a desktop. Hierarchical task tracking, autonomous GUI agent with visual grounding, smart visual waiting, and a live web dashboard. All local-first.

## Quick Install

```bash
git clone https://github.com/Memories-ai-labs/openclaw-detm.git
cd openclaw-memoriesai
OPENROUTER_API_KEY=sk-or-... ./install.sh
```

The installer handles everything:
- Python venv + dependencies
- System packages (xdotool, ffmpeg, XFCE4, Xvfb, scrot, x11vnc, noVNC)
- Virtual display + VNC (headless servers) or real display detection
- DETM daemon as a systemd service (auto-start on boot)
- OpenClaw MCP server registration (auto-discovers DETM tools)
- Browser installation if none found

You need an [OpenRouter API key](https://openrouter.ai/keys) for the GUI agent (Gemini Flash + UI-TARS grounding).

## Update

```bash
cd openclaw-memoriesai && git pull && ./install.sh
```

The installer is idempotent — safe to re-run anytime.

## Uninstall

```bash
# Run the uninstall script (reverses everything install.sh did)
./uninstall.sh

# Then remove the repo itself
rm -rf ~/openclaw-memoriesai
```

## Dashboard

The daemon serves a web dashboard at `http://127.0.0.1:18790/dashboard`. It shows:
- Task list with status and progress
- Expandable plan items with nested actions and screenshots
- Live desktop viewer (see what the agent is doing in real time)
- Message feed (agent narration, wait events, system messages)
- Cancel/pause tasks from the UI
- AI API cost tracking

### Accessing from a remote server (e.g. DigitalOcean)

The daemon only listens on localhost. To access from your laptop or phone:

**SSH tunnel (recommended):**
```bash
ssh -L 18790:localhost:18790 -L 6080:localhost:6080 user@your-server-ip
# Then open http://localhost:18790/dashboard in your browser
# VNC: http://localhost:6080/vnc.html
```

**Cloudflare tunnel (no SSH needed):**
```bash
# On the server:
cloudflared tunnel --url http://localhost:18790
# Opens a public https URL you can access from any device
```

## VNC (see the desktop)

On headless servers, `install.sh` sets up a virtual display with VNC:

| Service | Port | Description |
|---------|------|-------------|
| `detm-xvfb` | -- | Virtual display `:99` at 1920x1080 |
| `detm-vnc` | 5901 | Native VNC (use any VNC client) |
| `detm-novnc` | 6080 | Browser-based VNC (works on mobile) |

**Access via browser:**
```
http://localhost:6080/vnc.html
```

**Access via native VNC client:**
```
localhost:5901
```

Both require an SSH tunnel if accessing remotely (see above).

## Architecture

```
OpenClaw LLM → DETM (task hierarchy) → GUI Agent (Gemini + UI-TARS) → Desktop/Xvfb
```

Five layers:
1. **Task Management** — hierarchical: Task → Plan Items → Actions → Logs
2. **Smart Wait** — vision-based async monitoring with adaptive polling
3. **GUI Agent** — Gemini Flash supervisor + UI-TARS grounding (precise cursor placement)
4. **Vision** — cloud vision via OpenRouter (default), or local backends (Ollama, vLLM)
5. **Display** — shared system display visible in VNC and the dashboard

## GUI Agent

The `gui_agent` is the main tool for desktop interaction. It uses:
- **Gemini Flash** as the supervisor (decides what to do, two-pass thinking)
- **UI-TARS-7B** as the grounding model (finds where on screen to click)
- **Multi-View Point ensembling** (MVP) for more accurate cursor placement
- **Independent verification** — checks task completion before signaling done

**Launch apps via CLI, use gui_agent for interaction.** DETM runs on a bare Linux desktop (XFCE on a headless VM) — no curated dock or app menus. gui_agent is slow at finding and opening apps visually. Since it's just Linux, launch everything from the command line:

```python
# Good — launch browser via CLI, then delegate UI interaction to gui_agent
# (terminal): firefox https://www.google.com/travel/flights &
gui_agent(instruction="Search for flights from NYC to London on March 28", timeout=120)

# Bad — gui_agent wastes 30-60s hunting for browser icons on a bare desktop
gui_agent(instruction="Open Chrome, navigate to Google Flights, search for flights from NYC to London on March 28", timeout=120)
```

## Tools

### Core Tools
| Tool | Description |
|------|-------------|
| `gui_agent` | Autonomous GUI agent for any desktop interaction (clicks, typing, navigation, forms). Handles 10-30 step workflows. |
| `desktop_look` | Screenshot — you interpret the image yourself |
| `smart_wait` | Vision-based async monitoring ("wake when export finishes") |

### Task Management
| Tool | Description |
|------|-------------|
| `task_register` | Create task with plan items |
| `task_update` | Post message, change status, query state |
| `task_item_update` | Update plan item status |
| `task_plan_append` | Add new plan items mid-task |
| `task_log_action` | Log action under a plan item |
| `task_summary` | Task overview at various detail levels |

### Other
| Tool | Description |
|------|-------------|
| `mavi_understand` | Record screen + ask a question about the video |
| `video_record` | Record screen/window clip |
| `health_check` | Daemon + vision + system status |

## Sub-Agents

DETM includes specialized sub-agents for platform-specific tasks. Each sub-agent has its own instructions, tips, and workflow optimized for a specific platform.

| Agent | Platform | What it does |
|-------|----------|-------------|
| `linkedin` | LinkedIn | Profile research, outreach, connection management, lead prospecting |
| `tiktok` | TikTok | Trend research, sound identification, creator discovery, content analysis |

### How to use sub-agents

Spawn a sub-agent from Discord or the OpenClaw TUI with the `/subagents spawn` command:

```
/subagents spawn linkedin "Find Eddy Wu from Memories AI on LinkedIn and tell me his headline and current role"
/subagents spawn tiktok "Search TikTok for trending AI hashtags and give me the top 5 with post counts"
```

The sub-agent runs in the background and announces results back to your conversation when done. On Discord with thread bindings enabled, it gets its own thread.

### What happens when you spawn

1. You type `/subagents spawn <agent> "task description"`
2. Sub-agent starts in an isolated session with platform-specific instructions + DETM tools
3. Sub-agent launches Firefox via CLI, navigates the platform, completes the task
4. Sub-agent announces results back to your conversation
5. If the sub-agent hits a login wall or CAPTCHA, it will ask you to resolve it via VNC

### Monitoring and managing

```
/subagents list              # see running sub-agents
/subagents log <id>          # inspect a sub-agent's progress
/subagents kill <id>         # stop a stuck sub-agent
/subagents kill all          # stop all sub-agents
```

Watch the DETM dashboard (`http://127.0.0.1:18790/dashboard`) to see the sub-agent's task, plan items, screenshots, and progress in real time.

### Deploy sub-agents
```bash
DETM_DEPLOY_AGENTS=1 ./install.sh
```

### Discord thread bindings (recommended)

Enable thread bindings so each sub-agent run gets its own Discord thread:
```json
{
  "channels": {
    "discord": {
      "threadBindings": {
        "enabled": true,
        "spawnSubagentSessions": true,
        "idleHours": 24
      }
    }
  }
}
```

### Creating a new sub-agent
1. Create `openclaw/agents/<agent-name>/`
2. Add `SOUL.md` (identity — who the agent is) and `AGENTS.md` (instructions — platform rules, tips, workflow)
3. Run `DETM_DEPLOY_AGENTS=1 ./install.sh`

## Using DETM with OpenClaw

`install.sh` handles all DETM-side setup, including MCP server registration. After install:

1. **Restart the OpenClaw gateway** to pick up the new MCP server:
```bash
openclaw gateway restart
```
2. **Verify** DETM tools are available — ask the agent:
```
"What DETM tools do you have?"
# Should list: task_register, gui_agent, desktop_look, etc.
```

That's it. OpenClaw will now have access to all DETM tools.

**Important**: DETM is an MCP server. Do **not** manually add a `"detm"` entry to `plugins.entries` in `openclaw.json` — that is a different integration path and will cause errors. If `install.sh` ran successfully, no manual OpenClaw configuration is needed for DETM.

## Common Issues

### Daemon won't start / port already in use
```bash
# Kill stale processes on the daemon port
fuser -k 18790/tcp
# Then restart
sudo systemctl restart detm-daemon
# Or use dev.sh
./dev.sh
```

### Virtual display not working
```bash
# Check all display services
sudo systemctl status detm-xvfb detm-vnc detm-novnc detm-desktop

# Restart display stack
sudo systemctl restart detm-xvfb detm-desktop detm-vnc detm-novnc

# Verify display is working
DISPLAY=:99 xdpyinfo | head -5
```

### Black screen in VNC
The XFCE screensaver can blank the display. The installer disables it, but on older installs:
```bash
DISPLAY=:99 killall xfce4-screensaver
sudo systemctl restart detm-desktop
```

### GUI agent times out
- Default timeout is 180s. For complex tasks, set `timeout=300`
- Check if OpenRouter API key is valid: `curl http://127.0.0.1:18790/health`
- The two-pass thinking architecture uses ~2 API calls per action step

### Dashboard shows no tasks
- Make sure you're calling `task_register` before any desktop tools
- Check the daemon is running: `curl http://127.0.0.1:18790/health`

### OpenClaw doesn't see DETM tools
```bash
# Re-run the installer (it's idempotent)
./install.sh

# Restart the OpenClaw gateway
openclaw gateway restart

# Ask the agent to confirm
openclaw agent --agent main -m "What DETM tools do you have?"
```

If you see DETM configured as a plugin in `openclaw.json` (`"detm": {"enabled": true}` under `plugins.entries`), remove that entry — DETM should only be used as an MCP server.

### Docker volumes filling disk
OSWorld benchmark runs create Docker volumes. Clean them periodically:
```bash
docker volume prune -f
docker container prune -f
```

## Manage Services

```bash
# Restart everything
sudo systemctl restart detm-daemon detm-xvfb detm-vnc detm-novnc detm-desktop

# Just the daemon (picks up code changes)
sudo systemctl restart detm-daemon

# Check status
sudo systemctl status detm-daemon
journalctl -u detm-daemon -f    # tail logs

# Development mode (runs from repo, debug logging)
./dev.sh                         # start in tmux
./dev.sh stop                    # stop
./dev.sh logs                    # tail logs
./dev.sh status                  # health check
```

## Configuration

Everything runs through OpenRouter by default — no local GPU or models needed. The installer validates the API key at install time.

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENROUTER_API_KEY` | -- | **Required.** Used by GUI agent, Smart Wait, and grounding. Validated at install. |
| `DISPLAY` | `:99` | X11 display |
| `ACU_OPENROUTER_LIVE_MODEL` | `google/gemini-3-flash-preview` | GUI agent supervisor model. For caching, set `GEMINI_API_KEY` to route via Google AI Studio instead. |
| `GEMINI_API_KEY` | -- | Optional. If set, supervisor calls go direct to Google AI Studio (bypasses OpenRouter, enables native Gemini caching). Get one at [aistudio.google.com](https://aistudio.google.com/apikey). |
| `ACU_GEMINI_BACKEND` | `auto` | Which backend for Gemini models: `auto` picks AI Studio if `GEMINI_API_KEY` is set, else OpenRouter. Force with `openrouter` or `google_ai`. |
| `ACU_UITARS_OPENROUTER_MODEL` | `bytedance/ui-tars-1.5-7b` | Grounding model (cursor placement) |
| `ACU_OPENROUTER_VISION_MODEL` | `google/gemini-2.0-flash-lite-001` | Smart Wait vision model |
| `ACU_VISION_BACKEND` | `openrouter` | Vision backend for Smart Wait. Change to `ollama` only if you have a local GPU. |
| `ACU_DEBUG` | `0` | Verbose debug logging |
| `MAVI_API_KEY` | -- | Memories.AI API key for `mavi_understand` |

## Logs

| Log | Location | How to view |
|-----|----------|-------------|
| Daemon log | `~/.agentic-computer-use/logs/debug.log` | `tail -f ~/.agentic-computer-use/logs/debug.log` |
| Systemd journal | journalctl | `journalctl -u detm-daemon -f` |
| Dev mode | tmux | `./dev.sh logs` |
| GUI agent sessions | `~/.agentic-computer-use/live_sessions/` | `PYTHONPATH=src .venv/bin/python3 scripts/inspect_session.py --list` |

## Requirements

| Component | Minimum | Recommended |
|-----------|---------|-------------|
| Python | 3.11+ | 3.12+ |
| RAM | 4 GB | 8 GB |
| OS | Linux (X11) | Ubuntu 22.04+ |
| GPU | Not needed | All vision runs through OpenRouter |
| API Key | OpenRouter | Free at https://openrouter.ai/keys |

## Storage

| Path | Contents | Growth |
|------|----------|--------|
| `~/.agentic-computer-use/data.db` | SQLite database (tasks, actions, logs) | Slow |
| `~/.agentic-computer-use/screenshots/` | Action screenshots (JPEG per GUI action) | Fast |
| `~/.agentic-computer-use/live_sessions/` | GUI agent session logs and frames | Fast |
| `~/.agentic-computer-use/recordings/` | Screen recordings (auto-pruned at 1GB) | Medium |
| `~/.agentic-computer-use/logs/` | Debug logs (auto-rotated at 10MB each) | Medium |

### Automatic cleanup

- **Recordings**: Oldest task recordings are pruned when total exceeds `ACU_MAX_RECORDINGS_MB` (default: 1000MB). Set to `0` for unlimited.
- **Debug logs**: Rotated automatically when a log exceeds 10MB.
- **Screenshots**: Deleted automatically when a task is deleted.

### Manual cleanup

```bash
# Check disk usage
du -sh ~/.agentic-computer-use/*/

# Delete old screenshots (older than 7 days)
find ~/.agentic-computer-use/screenshots/ -type f -mtime +7 -delete

# Delete old session logs (older than 7 days)
find ~/.agentic-computer-use/live_sessions/ -type d -mtime +7 -exec rm -rf {} +

# Delete old debug logs (keeps current)
find ~/.agentic-computer-use/logs/ -name "debug.*.log" -mtime +7 -delete

# Nuclear option: wipe all data and start fresh (stops daemon first)
sudo systemctl stop detm-daemon
rm -rf ~/.agentic-computer-use
sudo systemctl start detm-daemon
```

## License

MIT
