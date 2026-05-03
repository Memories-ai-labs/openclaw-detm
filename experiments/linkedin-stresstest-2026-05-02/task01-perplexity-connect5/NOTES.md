# Task 01 — Perplexity AI 5-connect

**Status:** FAILED (harness, not model)
**Wallclock:** ~190s before manual cancel (would have run ~3 more min on orphan)
**Connections sent:** 0/5
**Cost:** ~$0.05 (38 bash calls × ~$0.012 each)

## What happened

1. Main agent (gpt-5.4 in OpenClaw TUI) registered task, called `gui_agent` (session `1fefe3df`)
2. **OpenClaw MCP gateway timed out at 60s** — returned `MCP error -32001: Request timed out` to the main agent
3. Main agent retried `gui_agent` → rejected: `"another gui_agent is already running on this task"` — daemon's concurrent-session guard
4. Main agent gave up: "I could not safely complete the 5 connection requests"
5. **Daemon kept executing session `1fefe3df` for ~3 minutes after the main agent gave up** — 38 bash calls total before I manually cancelled. Each `xdotool` call mutated screen state with no consumer.

## Root cause

Two architectural mismatches in the harness, both already documented as known issues:

- OpenClaw MCP gateway timeout: 60s
- DETM daemon `gui_agent` cap: 240s
- **No bridge between the two** — when MCP times out, daemon doesn't know and keeps running
- Concurrent-session guard then blocks all subsequent `gui_agent` calls until the orphan's 240s cap expires

## Was the model failing?

No. The bash trace shows gpt-5.4 was making sensible progress: pressing Ctrl+L, typing the LinkedIn company-people URL, pressing Return. It tried search, then direct URL. Just slow on a cold-start cycle (Firefox tab still on stale Runway page from prior bake-off; needed full URL navigation).

If we'd raised the `gui_agent(timeout=...)` parameter to 180s and bridged the MCP/daemon timeout properly, this might have completed.

## Production hazards exposed

**P0** — MCP timeout never triggers daemon cancel. Orphan sessions burn API spend, mutate screen, and block subsequent calls.
**P0** — `bash_backend._run_bash` uses `subprocess.run(timeout=...)` which raises but doesn't kill the child. Orphan session continued running `xdotool` even though it should have been stopped.
**P1** — Default MCP `gui_agent(instruction)` doesn't pass an explicit timeout — agent would need to know to ask for `timeout=180`.

## Artifacts in this dir

- `prompt.txt` — the task instruction
- `conversation.tui.txt` — full TUI dump
- `daemon.session-1fefe3df.log` — daemon log slice for the orphan session
- `screenshot.jpg` — final state of `:99` after cancel
- `task_started_at.txt`, `task_ended_at.txt`, `wallclock.txt` — timing
