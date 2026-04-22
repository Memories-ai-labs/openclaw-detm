---
name: agentic-computer-use
description: Desktop Environment Task Manager (DETM) — hierarchical task tracking, smart visual waiting, GUI automation with NL grounding, and screen recording. Use for multi-step desktop workflows, visual app control, and long-running operations.
---

# agentic-computer-use — DETM

Local daemon on `127.0.0.1:18790` with hierarchical task persistence, async wait engine, GUI grounding, and pluggable vision.

## Architecture — who owns what

Three layers, each with a clear responsibility:

**You (OpenClaw LLM)** — own the plan and the user. You decide what needs to happen, break it into steps, create the DETM task, and talk to the user. You have access to everything: CLI, web search, file system, DETM tools, and any other OpenClaw tools. During a DETM task, you are free to mix GUI actions with CLI commands, web searches, file writes, or any other tool — DETM handles visual GUI interaction, but that is not the only way to get things done. Use whatever is fastest and most reliable.

**Supervisor (Gemini Flash)** — owns each action loop. When you call `gui_agent`, the supervisor takes over the screen. It sees screenshots, decides what to click/type/scroll, and uses the grounding model to place the cursor precisely. It verifies each action landed correctly before proceeding. It does NOT plan, does NOT know the broader task, and has NO access to CLI, files, or web search. It executes one concrete instruction and reports back.

**Grounding model (UI-TARS)** — owns cursor placement. Given a screenshot and a target description ("the Submit button"), it predicts pixel coordinates. The supervisor verifies the placement visually and decides whether to click or retry. UI-TARS does no reasoning — it just points.

```
You (plan, decide, talk to user)
  → gui_agent("click search bar, type hello, press Enter")
    → Supervisor (screenshot → decide action → verify)
      → UI-TARS (find element → return coordinates)
    → Supervisor returns {success, summary, actions_log}
  → You read result, take desktop_look if needed, decide next step
```

**Key principle:** DETM is one of your tools, not your only tool. If a website is broken and the fix is `pkill firefox && firefox <url> &`, do that via CLI. If you need to check whether a URL is up before navigating to it, use `curl`. The supervisor is fast at GUI manipulation but blind to everything else — you are the one with the full picture. All GUI interaction (clicking, typing, scrolling, keyboard shortcuts) goes through `gui_agent`. For CLI commands, use your native `Bash`/`exec` tools directly.

## When to use DETM vs. other tools

**Default to faster tools first.** DETM is powerful but slow. Always prefer built-in tools when they can do the job.

| Situation | Use instead of DETM |
|---|---|
| General web research, finding facts, news, documentation | `web_search` or `WebFetch` |
| Public APIs with known endpoints | Direct HTTP calls via `Bash` or `httpx` |
| File operations, data processing, code execution | `Bash`, Python, CLI tools |
| Publicly accessible URLs with no login | `WebFetch` or `curl` |
| Simple web page with no bot detection | OpenClaw `browser` tool (faster, DOM-level) |

**Use DETM when:**
- The site requires **authentication** (login, session cookies) — DETM uses a real Firefox session that the user can log into via VNC
- The site has **bot detection** (TikTok, LinkedIn, Instagram, most social media) — OpenClaw's `browser` tool uses headless Chrome with CDP, which many sites detect and block. DETM uses a real Firefox window on a real X11 display, which is much harder to fingerprint
- You need to interact with **non-browser apps** (DaVinci Resolve, LibreOffice, file manager, etc.)
- The user needs to **see what's happening** in real time (VNC + dashboard)
- You need the user to **take over** at any point (login, CAPTCHA, 2FA) — DETM runs on a visible desktop the user can VNC into

**DETM vs. OpenClaw browser tool:**
| | DETM (`gui_agent`) | OpenClaw `browser` |
|---|---|---|
| How it works | Screenshots + visual grounding (pixel-level) | CDP + accessibility tree (DOM-level) |
| Browser | Firefox on real X11 display | Headless Chromium |
| Bot detection | Hard to detect — real browser, real display | Often detected — headless Chrome fingerprint |
| Auth / login | User logs in via VNC, session persists | Requires cookies/credentials in config |
| Non-browser apps | Yes — any app on the desktop | No — Chrome only |
| Speed | Slower (screenshot per action) | Faster (DOM operations) |
| User takeover | Yes — VNC into display :99 | No |

**Rule of thumb:** If the site might block bots, or the user might need to log in or solve a CAPTCHA, use DETM. If it's a simple public page with no bot detection, use the `browser` tool or `WebFetch`.

**NEVER use the `browser` tool for these platforms — always use DETM `gui_agent`:**
- **TikTok** — aggressive bot detection, blocks headless Chrome
- **LinkedIn** — requires auth, blocks headless Chrome
- **Instagram** — requires auth, blocks headless Chrome
- **Twitter/X** — aggressive bot detection
- **Any site where the user says "use DETM"** — respect the explicit request

This is a hard rule. Even if the `browser` tool seems faster or easier, these platforms will block it or return incomplete data. Use DETM `gui_agent` which runs a real Firefox on a real display.

**When DETM hits a login wall or CAPTCHA:** tell the user via `task_update`, then use `smart_wait` to poll until they resolve it via VNC. Never try to log in yourself or bypass auth.

## Desktop environment

DETM runs on a bare Linux desktop (XFCE on a headless VM). The desktop is minimal — no curated dock, no pinned apps, sparse icons.

**Launch apps via CLI, not gui_agent.** Every app can be launched instantly from the command line. gui_agent wastes 30-60s hunting for icons on a bare desktop.

```bash
firefox https://example.com &
sleep 3
DISPLAY=:99 xdotool windowsize --sync $(xdotool getactivewindow) 1920 1080
DISPLAY=:99 xdotool windowmove --sync $(xdotool getactivewindow) 0 0
```

**Always maximize windows after launching.** Windows open at ~1280x792 on the 1920x1080 desktop. gui_agent and desktop_look will miss content that's off-screen.

**Close popups and overlays** (cookie banners, login modals) before reading the screen — they block content.

## HARD RULES

These are not guidelines. Violating them breaks observability and cancellation.

**1. Always create a task before touching the desktop.**
`task_register` MUST be your first call for any work that involves `desktop_look`, `gui_agent`, or `smart_wait`. Without a task, the human cannot cancel you and the dashboard shows nothing.

```
task_register(name="Find reporters on LinkedIn", plan=["Search LinkedIn", "Collect profiles", "Write to sheet"])
task_item_update(task_id=<id>, ordinal=0, status="active")
```

**2. Log desktop actions with `task_log_action`.**
Before each `gui_agent` or `desktop_look` call, log what you're about to do. After, report what you observed via `task_update`. This is how the dashboard shows your progress.

```
task_log_action(task_id=<id>, action_type="gui", summary="Searching LinkedIn for reporters")
gui_agent(instruction="Click the search bar, type TechCrunch AI reporters, press Enter", task_id=<id>)
task_update(task_id=<id>, message="Search submitted, results loading.")
```

You do NOT need to log non-desktop tool calls (web_search, file reads, bash commands). Only log DETM tool calls.

**3. Pass `task_id` to every desktop/GUI/wait call.**

**4. Verify visually — don't assume failure.**
`gui_agent` calls can take 30-120 seconds. If a call times out or returns an unclear result, **do NOT assume it failed.** Always take a `desktop_look` to see the actual screen state before deciding what to do next. The task often succeeded — the response just took too long to arrive.

```
# gui_agent timed out or returned empty:
# WRONG — blindly retry:
gui_agent(instruction="same thing again", task_id=<id>)  # wastes time, page already loaded

# RIGHT — look first, then decide:
desktop_look(task_id=<id>)  # see what actually happened
# If the page loaded → continue to next step
# If it genuinely failed → try a different approach
```

**`desktop_look` is your ground truth.** When in doubt about what happened, look at the screen. Don't reason from error messages alone — the screen shows the real state.

**Retry once with a different approach before giving up.** If gui_agent fails:
1. `desktop_look` to see the screen
2. If the screen shows the task actually succeeded → continue
3. If it genuinely failed → retry **once** with a different instruction (e.g. use a URL instead of clicking, try a keyboard shortcut, simplify the instruction)
4. If the retry also fails → try solving it via CLI (`exec`) if possible, or escalate to the user
5. **Never retry the exact same instruction** — if it failed once, the same instruction will fail again. Change your approach.
6. **Never retry more than once** — two failures means the approach is wrong, not that it needs a third try

If a step genuinely fails after retrying, do NOT create a new task. Use `task_plan_append` to add corrective steps and `task_item_update(status="scrapped")` on stale items.

**5. Check task status after each plan item.**
```
task_item_update(task_id=<id>, ordinal=N, status="completed")
status = task_update(task_id=<id>, query="status")
if status is cancelled or paused → stop immediately
```
This is how the human cancels you from the dashboard.

## Choosing the right tool

| Situation | Tool |
|---|---|
| Launch an app | CLI via `Bash` (`firefox &`, `thunar &`) |
| See the current screen state | `desktop_look` |
| Any GUI interaction (click, type, scroll, keyboard shortcuts) | `gui_agent` |
| Wait for something to appear/finish | `smart_wait` |
| Read content from a public URL | `curl` or `WebFetch` (faster than screenshotting) |
| File operations, shell commands | `Bash` / `exec` (OpenClaw native) |

### gui_agent — deterministic GUI steps

`gui_agent` is a fast vision-based executor. It handles the screenshot→action→verify loop using a vision model (Gemini Flash) and a grounding model (UI-TARS). It does NOT plan or reason about the broader task — that's your job.

**Give it concrete, deterministic instructions (3-8 GUI steps).** Don't dump a whole task on it — break your work into small, verifiable chunks and check in between each.

```
# Good — concrete, verifiable:
gui_agent(instruction="Click the search bar on Google, type 'flights NYC to London', press Enter", task_id=<id>)
# → check results with desktop_look → decide next step

gui_agent(instruction="Click the 'Nonstop' filter checkbox in the left sidebar", task_id=<id>)
# → check results with desktop_look → decide next step

# Bad — too broad, supervisor can't recover if anything goes wrong:
gui_agent(instruction="Search for flights, filter nonstop, select the cheapest, and proceed to booking", task_id=<id>)
```

**After each gui_agent call, take a `desktop_look`** to see what actually happened. The gui_agent result includes `{success, summary, actions_taken, actions_log}` — read the `actions_log` to understand what was tried, especially on failure.

**When gui_agent fails:** read the `actions_log` and `summary` carefully. It tells you what the supervisor tried and why it gave up. Use that to decide:
- Retry with a different instruction (e.g. keyboard shortcut instead of clicking)
- Fix the underlying problem via CLI (restart browser, navigate via URL bar)
- Take a `desktop_look` to see the current state and replan

**gui_agent cannot access CLI, files, or web search.** If the problem requires anything outside the GUI (checking a URL, clearing cache, restarting an app), you must do it yourself, then send gui_agent back in.

### desktop_look — observe and decide

Take a screenshot and reason about it yourself. No model is invoked — you interpret the image directly. Use this to read content from the screen, check results, or understand the current state before deciding what to do next.

## Browser interaction

**Prefer visual interaction** for authenticated sites and interactive web apps — `gui_agent` for clicking/typing, `desktop_look` for reading.

**Be pragmatic.** If a CLI approach is faster and more reliable, use it:
- `curl`/`wget` to fetch a public URL
- `firefox <url>` to navigate directly instead of clicking through menus
- CLI tools to process downloaded data

**The goal is task completion, not visual purity.**

## Task lifecycle

```
1. task_register → create task with plan items
2. task_item_update(ordinal=0, status="active")
3. Do the work (gui_agent, desktop_look, CLI, etc.)
4. task_item_update(ordinal=0, status="completed")
5. task_update(query="status") → check for cancellation
6. Repeat for each plan item
7. task_update(status="completed")
```

If reality diverges from the plan, use `task_item_update(status="scrapped")` + `task_plan_append` to revise.

## Tool reference

### Task Management
- `task_register` — create task with plan items
- `task_update` — post message, change status, query state
- `task_item_update` — update plan item status (pending/active/completed/failed/skipped/scrapped)
- `task_plan_append` — append new plan items to an existing task
- `task_log_action` — log action under a plan item (cli/gui/wait/vision/reasoning)
- `task_summary` — task overview (items/actions/full/focused)
- `task_drill_down` — expand one plan item's actions and logs
- `task_list` — list tasks by status

### Smart Wait
- `smart_wait` — delegate visual monitoring (polls screen with vision model)
- `wait_status` / `wait_update` / `wait_cancel`

### GUI Agent
- `gui_agent` — autonomous GUI agent (Gemini Flash + UI-TARS). Handles clicks, typing, scrolling, navigation, form filling. Returns `{success, summary, escalated, actions_taken}`.

### Desktop
- `desktop_look` — screenshot returned as image (you interpret it)
- `video_record` — record screen/window clip

### Video Intelligence
- `mavi_understand` — record screen, ask a question about the video (for audio/motion content like TikTok sounds, animations, live feeds). **Requires `MAVI_API_KEY` to be set** — will fail without it.

### System
- `health_check` — full DETM diagnostic (see **Doctor & narration** below)
- `humanize_status`, `humanize_set` — GUI humanization flag (see **Humanization** below)
- `memory_search`, `memory_read`, `memory_append` — workspace memory files

## Humanization

GUI actions (click, mouse move, typing) run with **human-like timing and
motion by default**. This matters because every sensitive platform you
drive via DETM — LinkedIn, Instagram, TikTok, Outlook, Canva — runs bot
detection that fingerprints the mouse path, typing cadence, and click
timing in the JS event stream. Mechanical timing is the single biggest
tell, and it doesn't matter how clever your LLM reasoning is if the
events themselves look robotic.

What humanization does (all reliability-preserving):
- **Typing**: per-key delay is lognormally distributed around ~115 ms
  with ~22% chance of a 180–600 ms pause at word boundaries. Text
  content is unchanged.
- **Mouse moves**: quadratic Bezier arc with Fitts-scaled duration;
  endpoint is exactly the target (no overshoot), path stays on-screen.
- **Clicks**: 40–120 ms dwell between mousedown/mouseup; ±1–2 px jitter
  clamped inside the target bbox.
- **Pre-action thinking pause**: available but not auto-applied; call
  `humanize.apply_thinking_pause()` in the daemon side if a path wants
  it. (Your SKILL.md narration of what you're about to do already
  provides visible "thinking" to the dashboard viewer.)

**When to turn it off.** Pass `humanize_set(enabled=false, reason=...)`
when the task is **not** touching a fingerprinted web surface — examples:
- Running a shell/tmux command on :99 for local automation
- Driving DaVinci Resolve, Blender, VS Code, or other local desktop apps
- Speed-sensitive batch operations where no web platform is watching

Remember to flip it back on before the next sensitive task, or just let
it persist off for the session if you're staying local. The daemon also
resets to env default (`ACU_HUMANIZE=1`) on restart.

**When in doubt, leave it on.** The speed cost is ~5× typing and ~1.5–3×
total time per task — never a correctness cost. A correctness cost would
be if humanization caused a click to miss its target; by design (Bezier
endpoint invariant + bbox-clamped jitter) it can't.

Quick checklist:
```
humanize_status()                               # check current state
humanize_set(enabled=false, reason="local ffmpeg loop")   # opt out for one task
humanize_set(enabled=true, reason="resuming LinkedIn")    # opt back in
```

## Doctor & narration

`health_check` returns a structured report: one row per subsystem (daemon,
display, services, dashboard, backends, keys, MCP registration, storage,
deps, workspace). Each row has `status` = `ok` / `warn` / `fail` / `skip`.

**After calling `health_check`, you MUST post a `task_update` summarizing
what is green, what is yellow, and what is red.** If there are any `fail`
rows, name them specifically and tell the user what they need to do (e.g.
"Your OpenRouter key is invalid — get a fresh one at https://openrouter.ai/keys
and run `detm-configure keys --set OPENROUTER_API_KEY=...`").

The user can also run diagnostics and reconfiguration directly from a
shell — these are the commands to mention when escalating:

```
detm-doctor                               # full report (exit 0/1/2)
detm-doctor --json                        # machine-readable
detm-doctor --quiet                       # only warnings/failures
detm-configure                            # interactive wizard (all sections)
detm-configure <section>                  # just one section
detm-configure <section> --show           # read-only
detm-configure <section> --set KEY=VAL    # non-interactive write
detm-configure <section> --dry-run        # compute diff, don't write
```

Sections: `vision`, `gui-agent`, `keys`, `display`, `services`,
`workspace`, `runtime`, `mcp`, `dashboard`. Writes to both the systemd
unit (`/etc/systemd/system/detm-daemon.service`) and `~/.openclaw/openclaw.json`
with `.bak` rotation; restarts the daemon automatically when it has sudo.

## Escalation scenarios

- **Login / auth wall** — tell the user, use `smart_wait` to poll until they log in via VNC
- **CAPTCHA** — tell the user, wait for them to solve it
- **2FA prompt** — tell the user, wait for them to enter the code
- **gui_agent escalated** — relay `escalation_reason` to the user

## Stuck detection and automatic resumption

If an active task has no updates for 5+ minutes, the daemon sends a `[task_stuck_resume]` event with a resume packet containing task state, plan items, and recent actions. Use it to orient yourself and continue.

If the resume packet contains `agent_id`, spawn that sub-agent to continue:
```
/subagents spawn <agent_id> "Resume DETM task <task_id>. <resume context>"
```

## Dashboard

The daemon serves a web dashboard at `http://127.0.0.1:18790/dashboard`. The human can see tasks, plan items, screenshots, live screen stream, and cancel/pause tasks.

## Lifecycle: install / update / debug / uninstall

This section is the single source of truth for managing the DETM install
end-to-end. When the user asks you to install/update/diagnose/remove
DETM, follow these recipes — don't improvise. Every command here is
idempotent (safe to re-run).

### Repository

- **Source:** https://github.com/Memories-ai-labs/openclaw-detm.git
- **Default checkout:** `~/openclaw-memoriesai/`  (use this unless the
  user specifies otherwise)
- **Branch:** `master`

### Install (first time)

```
git clone https://github.com/Memories-ai-labs/openclaw-detm.git ~/openclaw-memoriesai
cd ~/openclaw-memoriesai
OPENROUTER_API_KEY=sk-or-... ./install.sh
```

`install.sh` is interactive — it'll prompt for the OpenRouter key if
unset, ask for sudo to write systemd units, and print a summary at the
end. It's safe to re-run (it tears down stale services first). Takes
~30s on a warm machine, ~3min on a fresh box (apt installs).

What it does (in order): cleans previous install → validates OpenRouter
key → installs Python 3.11+ → installs system deps (xdotool, ffmpeg,
xfce4, xvfb, x11vnc, novnc, …) → ensures a browser is present →
sets up the virtual display + VNC + noVNC services on `:99` →
configures XFCE → installs the Python venv at `.venv/` → registers
the `detm-daemon` systemd service → registers the OpenClaw MCP server
+ skill symlink + plugin → final health check.

### Update (day-to-day)

```
cd ~/openclaw-memoriesai
./update.sh                  # fast: git pull + pip + restart + verify
./update.sh --check          # show what's new on origin/master, don't pull
./update.sh --no-restart     # pull + pip but leave the daemon running stale code
```

Takes <5s when there are no dep changes. Falls back to ./install.sh
if anything looks broken (working tree dirty, venv missing, etc.).

When in doubt, `./install.sh` does the same thing — slower but more
thorough (re-runs apt installs, recreates services from scratch).

### Status / health check

```
./bin/detm-doctor                  # human-readable, exit 0/1/2
./bin/detm-doctor --quiet          # only warnings + failures
./bin/detm-doctor --json           # for piping
curl http://127.0.0.1:18790/health # lightweight (just daemon + vision)
curl http://127.0.0.1:18790/doctor # full diagnostic JSON
```

Or call `health_check` via MCP — same result, narrate per the
**Doctor & narration** section above.

### Reconfigure

```
./bin/detm-configure                       # interactive wizard, all sections
./bin/detm-configure <section>             # one section
./bin/detm-configure <section> --show      # read-only
./bin/detm-configure <section> --set K=V   # non-interactive write
./bin/detm-configure <section> --dry-run   # compute diff, don't write
```

Sections: `vision`, `gui-agent`, `keys`, `display`, `services`,
`workspace`, `runtime`, `mcp`, `dashboard`. Writes are atomic with
`.bak` rotation; the daemon is restarted automatically when sudo is
available, otherwise the exact commands are printed for the user to
run.

### Debug

When something's wrong:

| Symptom | First check |
|---|---|
| `health_check` returns FAIL on `daemon` | `sudo systemctl status detm-daemon`, then `journalctl -u detm-daemon -n 100` |
| `gui_agent` returns escalated/error repeatedly | OpenRouter key (rotate via `detm-configure keys`); also check `~/.agentic-computer-use/logs/debug.log` |
| Live dashboard screen is black | Xvfb died — `sudo systemctl restart detm-xvfb detm-desktop` |
| Doctor flags `.env drift` warning | A stale `OPENROUTER_API_KEY` in repo's `.env` differs from the systemd unit. Either delete the stale line from `.env` or run `detm-configure keys` to sync everything |
| Daemon won't start after update | Check `journalctl -u detm-daemon -n 50`; if Python error, re-run `./install.sh` to rebuild venv |
| MCP tools missing in OpenClaw | Restart the gateway: `systemctl --user restart openclaw-gateway` |

Live debug log:
```
tail -f ~/.agentic-computer-use/logs/debug.log     # only populated when ACU_DEBUG=1
journalctl -u detm-daemon -f                       # systemd journal (always)
./bin/detm-doctor --quiet                          # surfaces last 200 lines of error tail
```

To enable verbose logging: `./bin/detm-configure runtime --set ACU_DEBUG=1`
(restarts daemon).

### Restart services

```
sudo systemctl restart detm-daemon                 # just the daemon (most common)
sudo systemctl restart detm-xvfb detm-desktop      # virtual display + XFCE
sudo systemctl restart detm-vnc detm-novnc         # VNC export
systemctl --user restart openclaw-gateway          # OpenClaw side (no sudo)
```

### Uninstall

```
cd ~/openclaw-memoriesai
./uninstall.sh                                     # reverses install.sh
rm -rf ~/openclaw-memoriesai                       # finally, drop the repo
```

`uninstall.sh` removes: all 5 systemd services + unit files, the MCP
server registration, the OpenClaw plugin, deployed sub-agents, the
skill symlink, the DETM section in MEMORY.md, the data dir
(`~/.agentic-computer-use`), and the venv. It does NOT uninstall
system packages (xdotool, ffmpeg, etc.) since other software may
depend on them.

### Paths reference

| Path | Purpose |
|---|---|
| `~/openclaw-memoriesai/` | Source repo + venv |
| `/etc/systemd/system/detm-*.service` | 5 systemd units (daemon, xvfb, desktop, vnc, novnc) |
| `~/.agentic-computer-use/` | Runtime data: `data.db`, `screenshots/`, `recordings/`, `logs/`, `live_sessions/` |
| `~/.openclaw/openclaw.json` | OpenClaw config (MCP registration, channels, plugins) |
| `~/.openclaw/workspace/skills/agentic-computer-use` | Symlink → `~/openclaw-memoriesai/skill/` |
| `~/.openclaw/workspace/MEMORY.md` | OpenClaw long-term memory (DETM injects a section here) |
| `http://127.0.0.1:18790/dashboard` | Web dashboard (loopback only; SSH-tunnel for remote) |
| `http://127.0.0.1:6080/vnc.html` | noVNC web client (when display is virtual) |
