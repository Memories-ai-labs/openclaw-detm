## Desktop & GUI Automation — DETM Workflow (Always Use This)

Any task involving desktop apps, GUI interaction, visual monitoring, or the desktop display **must** use the `agentic-computer-use` (DETM) skill. Do not use raw shell X11 tricks or improvise — DETM handles display capture, vision, and dashboarding.

**Mandatory workflow for desktop tasks:**
1. `task_register(name="...", plan=[...])` — always start with a plan
2. `task_item_update(ordinal=0, status="active")` — activate the first item
3. Before each action: `task_log_action(action_type=<type>, summary="About to do X", status="started")`
4. Execute the tool (`desktop_look`, `gui_agent`, `smart_wait`, etc.)
5. After each observation: `task_update(message="I see X, therefore I will do Y")`
6. `task_item_update(ordinal=N, status="completed")` when item is done
7. `task_update(status="completed")` to close the task

**Never skip narration.** The human watches the dashboard in real time. Every action must be announced before it happens.

**Revising the plan mid-task:** If the approach changes, scrap stale items and append revised ones — do not leave obsolete items as "pending".
```
task_item_update(ordinal=N, status="scrapped", note="reason")
task_plan_append(task_id=<id>, items=["revised step 1", "revised step 2"], note="reason for change")
```
Use `scrapped` when abandoning an approach. Use `skipped` only when intentionally bypassing a still-valid step.

**`desktop_look` returns the image directly to you** — you interpret it yourself. After calling it, always narrate what you see via `task_update(message=...)`.

**`gui_agent` is the unified GUI tool** — it uses Gemini Flash for reasoning and UI-TARS for precise cursor placement. Give it a natural language instruction describing what to do. It handles clicks, typing, scrolling, navigation, and multi-step workflows autonomously. It returns a dict — **always branch on the `status` field** (`complete`, `partial`, `failed`, `escalated`, `timeout`, `error`), not just `success`. See the DETM skill for the full decision tree.

**`smart_wait` is for long-running visual checks** (downloads, renders, builds). Use `window:<name>` not `screen` when possible. A vision model (Gemini Flash Lite via OpenRouter by default) polls every 1s with binary YES/NO — you just get woken up when done or when the timeout fires.

**Adapt to what's installed.** Never assume specific apps exist (e.g. "Open Firefox"). Use generic instructions ("Open a web browser") and let `gui_agent` figure it out.

Dashboard: `http://127.0.0.1:18790/dashboard` — Alex monitors this.
