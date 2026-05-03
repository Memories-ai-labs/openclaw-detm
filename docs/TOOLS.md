# Tool Reference

## Task Management (Hierarchical)

### `task_register`

Create a task with plan items.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | yes | Task name |
| `plan` | string[] | yes | Ordered checklist of plan items |
| `metadata` | object | no | Arbitrary key-value pairs. Use `display_width`/`display_height` to override the default 1280x720 task display resolution. |

Returns: `task_id`, plan items with ordinals.

### `task_update`

Post progress, change status, or query a task.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `task_id` | string | yes | Task ID |
| `message` | string | no | Logged as action under active plan item |
| `query` | string | no | AI-answered question about task state |
| `status` | string | no | active, paused, completed, failed, cancelled |

### `task_item_update`

Update a specific plan item's status.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `task_id` | string | yes | Task ID |
| `ordinal` | int | yes | Plan item index (0-based) |
| `status` | string | yes | pending, active, completed, failed, skipped |
| `note` | string | no | Note about the status change |

### `task_log_action`

Log a discrete action under a task's active plan item.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `task_id` | string | yes | Task ID |
| `action_type` | string | yes | cli, gui, wait, vision, reasoning, other |
| `summary` | string | yes | What was done |
| `input_data` | string | no | Command/input |
| `output_data` | string | no | Result/output |
| `status` | string | no | started, completed, failed (default: completed) |
| `ordinal` | int | no | Plan item index (defaults to active item) |

### `task_summary`

Get a summary of a task. Default view is item-level.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `task_id` | string | yes | Task ID |
| `detail_level` | string | no | items (default), actions, full, focused (expand only active item) |

### `task_drill_down`

Drill into a specific plan item to see its actions and logs.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `task_id` | string | yes | Task ID |
| `ordinal` | int | yes | Plan item index (0-based) |

### `task_thread`

Legacy message thread view. Use `task_summary` for the structured view.

### `task_list`

List tasks by status (default: active).

---

## Smart Wait

### `smart_wait`

Delegate visual monitoring to the daemon. Your run can end — you'll get a system event when the condition is met or times out.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `target` | string | yes | `window:<name_or_id>` or `screen` |
| `wake_when` | string | yes | NL condition to watch for |
| `timeout` | int | no | Seconds before giving up (default: 300) |
| `task_id` | string | no | Link wait to a task |
| `poll_interval` | float | no | Base interval in seconds (default: 2.0) |

### `wait_status`

Check active wait jobs. Optionally filter by `wait_id`.

### `wait_update`

Refine an active wait — update the condition or extend the timeout.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `wait_id` | string | yes | Wait job to update |
| `wake_when` | string | no | Updated condition |
| `timeout` | int | no | New timeout (resets clock) |
| `message` | string | no | Note to attach |

### `wait_cancel`

Cancel an active wait job.

---

## GUI Agent (NL Grounding)

### `gui_agent`

Delegate a multi-step UI workflow to a live vision model. The model sees the screen in real-time and performs GUI actions autonomously until the task is done or it escalates.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `instruction` | string | yes | What to accomplish (NL, multi-step OK) |
| `task_id` | string | no | Link session to a task |
| `timeout` | int | no | Max seconds (default: 60, max: 300) |
| `context` | string | no | Additional context for the model |

Returns a dict with:
- `status` — one of `complete`, `partial`, `failed`, `escalated`, `timeout`, `max_turns`, `format_error`, `error`. **Branch on this before acting.**
- `success` — boolean. `complete` and `partial` are success=true; everything else is success=false.
- `summary` — what the supervisor reports. Always populated.
- `remaining` — present when `status=partial`. Describes what's left so the caller can continue.
- `tried` — present when `status=failed`. List of approaches the supervisor attempted.
- `escalation_reason` — present when `status=escalated`.
- `actions_taken` — count of executed actions.
- `actions_log` — last ~10 executed actions (strings like `"click(button=left)"`).
- `session_id`, `elapsed_s` — for dashboard replay and timing.

The session is recorded to disk (frames + events + audio) and viewable in the dashboard via the "Live" button or replay viewer.

**Active provider** (production, May 2026): `live_ui/bash_backend.py` — single LLM via OpenRouter chat-completions with raw shell access. Default model `openai/gpt-5.4` (configurable via `ACU_OPENROUTER_GUI_DIRECT_MODEL`). The model writes `xdotool`/`wmctrl`/`scrot` commands directly; the harness executes them and feeds back stdout/stderr/exit_code + a fresh screenshot. Selected via `ACU_LIVE_UI_BACKEND=bash` (the default).

The legacy supervised provider (Gemini Flash supervisor + UI-TARS grounder, default model `google/gemini-3-flash-preview`) is still available via `ACU_LIVE_UI_BACKEND=supervised` for benchmark reproducibility. Returned-shape (`status`/`success`/`summary`/...) is identical between backends.

See `docs/GUI-AGENT-BACKENDS.md` for the full backend reference.

---

## Desktop Control

All GUI interaction (clicking, typing, scrolling, keyboard shortcuts) goes through `gui_agent`. Raw xdotool control via `desktop_action` was removed — the main LLM shouldn't guess pixel coordinates. For window management and other X11 tasks, use OpenClaw's native `exec` tool with `xdotool`/`wmctrl` directly.

### `desktop_look`

Screenshot + vision description of the screen.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `prompt` | string | no | What to describe (default: everything visible) |
| `window_name` | string | no | Focus a window first |

### `video_record`

Record a short screen clip.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `target` | string | no | `screen` or `window:<name>` |
| `duration` | int | no | Seconds (1-120, default: 10) |
| `fps` | int | no | Frames per second (default: 5) |

Returns the file path of the recording.

---

## System

### `health_check`

Check daemon, vision backend, and system health.

### `memory_search`, `memory_read`, `memory_append`

Workspace memory files (MEMORY.md and memory/*.md).

---

## GUI Agent

See `gui_agent` under **GUI Agent (NL Grounding)** above.

---

## MAVI

### `mavi_understand`

Record a short screen clip, upload to Memories.AI, and ask a question about it.

| Param | Type | Required | Description |
|-------|------|----------|-------------|
| `prompt` | string | yes | Question about what's on screen |
| `duration_seconds` | int | no | Recording length (default: 10) |
| `task_id` | string | no | Link to a task |

Returns `answer`. Requires `MAVI_API_KEY`.
