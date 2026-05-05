"""MCP server entry point — thin proxy to the persistent daemon."""
import asyncio
import json
import logging
import os
import httpx
from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import Tool, TextContent, ImageContent

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DAEMON_URL = os.environ.get("ACU_DAEMON_URL", "http://127.0.0.1:18790")

app = Server("agentic-computer-use")

# ─── Tool definitions ───────────────────────────────────────────

TOOLS = [
    # ── Smart Wait ─────────────────────────────────────────────
    Tool(
        name="smart_wait",
        description="Delegate waiting to a local vision model. Monitors one visual target and wakes you when a condition is met. Prefer a specific window target for precise waits (e.g., window:<id_or_name>). Your current run can end — you'll get a system event when the condition is satisfied or times out.",
        inputSchema={
            "type": "object",
            "properties": {
                "target": {"type": "string", "description": "What to watch. Prefer window:<name_or_id> so the model sees only one app (use desktop_look first to find window names). Use screen to watch the entire desktop."},
                "wake_when": {"type": "string", "description": "Natural language condition to watch for (e.g., 'download completes or error dialog appears')"},
                "timeout": {"type": "integer", "description": "Seconds before giving up. Default: 300", "default": 300},
                "task_id": {"type": "string", "description": "Link this wait to a task (auto-posts update on resolution)"},
                "poll_interval": {"type": "number", "description": "Base polling interval in seconds. Default: 2.0", "default": 2.0},
            },
            "required": ["target", "wake_when"],
        },
    ),
    Tool(
        name="wait_status",
        description="Check the status of active wait jobs.",
        inputSchema={
            "type": "object",
            "properties": {
                "wait_id": {"type": "string", "description": "Specific wait job to check (optional — omit for all)"},
            },
        },
    ),
    Tool(
        name="wait_update",
        description="Resume or refine an existing wait job. Use after being woken too early — update the condition or extend the timeout.",
        inputSchema={
            "type": "object",
            "properties": {
                "wait_id": {"type": "string", "description": "The wait job to update"},
                "wake_when": {"type": "string", "description": "Updated condition (replaces original)"},
                "timeout": {"type": "integer", "description": "New timeout in seconds (resets clock)"},
                "message": {"type": "string", "description": "Note to attach (logged in job history)"},
            },
            "required": ["wait_id"],
        },
    ),
    Tool(
        name="wait_cancel",
        description="Cancel an active wait job.",
        inputSchema={
            "type": "object",
            "properties": {
                "wait_id": {"type": "string", "description": "The wait job to cancel"},
                "reason": {"type": "string", "description": "Why it's being cancelled"},
            },
            "required": ["wait_id"],
        },
    ),

    # ── Task Management (Hierarchical) ─────────────────────────
    Tool(
        name="task_register",
        description="Register a new task with a plan. Creates plan items from the checklist. Returns task_id and plan item details.",
        inputSchema={
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Human-readable task name"},
                "plan": {"type": "array", "items": {"type": "string"}, "description": "Ordered checklist of plan items"},
                "metadata": {"type": "object", "description": "Arbitrary key-value pairs. Use display_width/display_height to override the default 1280x720 task display resolution."},
            },
            "required": ["name", "plan"],
        },
    ),
    Tool(
        name="task_update",
        description="Report progress on a task: post a message, change status, or query state.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task to update/query"},
                "message": {"type": "string", "description": "Progress update (logged as an action under the active plan item)"},
                "query": {"type": "string", "description": "Ask a question about the task (AI-answered from history)"},
                "status": {"type": "string", "enum": ["active", "paused", "completed", "failed", "cancelled"], "description": "Set task status"},
            },
            "required": ["task_id"],
        },
    ),
    Tool(
        name="task_item_update",
        description="Update a specific plan item's status. Automatically logs actions and durations.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "ordinal": {"type": "integer", "description": "Plan item index (0-based)"},
                "status": {"type": "string", "enum": ["pending", "active", "completed", "failed", "skipped", "scrapped"], "description": "New status for this plan item. Use 'scrapped' to abandon an item and replace it with new ones via task_plan_append."},
                "note": {"type": "string", "description": "Optional note about this status change"},
            },
            "required": ["task_id", "ordinal", "status"],
        },
    ),
    Tool(
        name="task_plan_append",
        description="Append new plan items to an existing task. Use after scrapping items to replace them with revised steps.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "items": {"type": "array", "items": {"type": "string"}, "description": "New plan items to append"},
                "note": {"type": "string", "description": "Optional reason for the plan change"},
            },
            "required": ["task_id", "items"],
        },
    ),
    Tool(
        name="task_log_action",
        description="Log a discrete action under a task's active plan item (e.g., 'ran ffmpeg', 'clicked Export').",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "action_type": {"type": "string", "description": "Action category: cli, gui, wait, vision, reasoning, other"},
                "summary": {"type": "string", "description": "What was done"},
                "input_data": {"type": "string", "description": "Command/input (optional)"},
                "output_data": {"type": "string", "description": "Result/output (optional)"},
                "status": {"type": "string", "enum": ["started", "completed", "failed"], "default": "completed"},
                "ordinal": {"type": "integer", "description": "Plan item index (optional — defaults to active item)"},
            },
            "required": ["task_id", "action_type", "summary"],
        },
    ),
    Tool(
        name="task_summary",
        description="Get a summary of a task at item level (default). Shows plan items with status and action counts.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "detail_level": {"type": "string", "enum": ["items", "actions", "full", "focused"], "default": "items", "description": "items=plan overview, actions=expand actions per item, full=include logs, focused=expand only active item"},
            },
            "required": ["task_id"],
        },
    ),
    Tool(
        name="task_drill_down",
        description="Drill into a specific plan item to see its actions and logs.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID"},
                "ordinal": {"type": "integer", "description": "Plan item index (0-based)"},
            },
            "required": ["task_id", "ordinal"],
        },
    ),
    Tool(
        name="task_thread",
        description="Get the legacy chat thread for a task (messages, progress). Use task_summary for structured view.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The task to read"},
                "limit": {"type": "integer", "description": "Max messages to return", "default": 50},
            },
            "required": ["task_id"],
        },
    ),
    Tool(
        name="task_list",
        description="List active (or all) tasks with progress summaries.",
        inputSchema={
            "type": "object",
            "properties": {
                "status": {"type": "string", "default": "active", "description": "Filter: active, completed, failed, cancelled, paused, all"},
                "limit": {"type": "integer", "default": 10},
            },
        },
    ),

    # ── GUI Agent (unified: Gemini supervisor + UI-TARS grounding) ─
    Tool(
        name="gui_agent",
        description=(
            "AI-powered GUI agent that autonomously completes multi-step UI tasks. "
            "Uses vision AI to monitor the screen and a specialized grounding model "
            "for precise cursor placement. Handles clicks, typing, scrolling, drag-and-drop, "
            "and keyboard shortcuts. Use for any GUI interaction — from single clicks to "
            "complex multi-step workflows like form filling, navigation, or file management. "
            "Returns a dict with a 'status' field: "
            "'complete' (fully done), "
            "'partial' (progress made, more work left — see 'remaining' field; reinvoke referencing current state), "
            "'failed' (blocked by specific issue — see 'tried' field), "
            "'escalated' (needs human), "
            "'timeout' (hard timeout — state unknown, desktop_look first), "
            "'busy' (another gui_agent is already running on this task — await it before retrying; do NOT fire parallel gui_agent calls on the same task), "
            "'error' (infrastructure problem). "
            "Always check 'status' before acting. Issue one gui_agent per task at a time — await each result before dispatching the next."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Link to active task for logging"},
                "instruction": {
                    "type": "string",
                    "description": (
                        "What to accomplish. Be specific and complete. "
                        "Examples: 'Click the search bar and type hello', "
                        "'Navigate to Settings > Export and click Export as PDF', "
                        "'Find the contact named Eddy and send a message saying hello'"
                    ),
                },
                "timeout": {"type": "integer", "description": "Max seconds (default 180, max 300)"},
                "context": {"type": "string", "description": "Extra context (credentials, URLs, prior state)"},
            },
            "required": ["instruction"],
        },
    ),

    Tool(
        name="gui_agent_cancel",
        description=(
            "Stop an in-flight gui_agent at its next checkpoint (within ~100ms). "
            "Use when: the user changes priorities and you need to start a new gui_agent on a different task; "
            "you receive a 'busy' status from gui_agent and decide preemption is the right move; "
            "or you want to abort a gui_agent that's stuck in a screenshot loop. "
            "After cancelling, the original caller's gui_agent invocation returns with status='cancelled'. "
            "Pass exactly one of task_id (cancel that task's gui_agent) or all=true (cancel whatever is running). "
            "Returns {cancelled: bool, count: int}."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Cancel the gui_agent attached to this task. Omit to cancel by 'all'."},
                "all":     {"type": "boolean", "description": "Cancel every active gui_agent. Use when you don't know the active task_id."},
            },
        },
    ),

    # ── Vision & Recording ─────────────────────────────────────
    Tool(
        name="desktop_look",
        description="Take a screenshot and return it directly to you as an image. Use this to observe the current screen state before deciding your next action. You interpret the image yourself — no local vision model is involved.",
        inputSchema={
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "Task ID — targets the task's virtual display"},
                "window_name": {"type": "string", "description": "Focus this window before capturing (optional)"},
            },
        },
    ),
    Tool(
        name="video_record",
        description="Record a short video clip of the screen or a window.",
        inputSchema={
            "type": "object",
            "properties": {
                "duration": {"type": "integer", "default": 10},
                "target": {"type": "string", "default": "screen"},
                "fps": {"type": "integer", "default": 5},
            },
        },
    ),
    Tool(
        name="health_check",
        description=(
            "End-to-end DETM health report. Returns a structured list of checks across "
            "daemon, display (Xvfb/VNC/noVNC), dashboard, backends (OpenRouter/Anthropic/"
            "Ollama), API keys, MCP registration, storage (SQLite + disk), dependencies, "
            "and workspace. Each row has status=ok|warn|fail|skip and a detail string. "
            "After calling, narrate the result via task_update: state which subsystems "
            "are green/yellow/red and list any missing keys the user must provide."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="humanize_status",
        description=(
            "Check whether GUI actions (clicks, mouse moves, typing) run with "
            "human-like timing and paths. Default is ON for all tasks. Returns "
            "{enabled, since, reason, defaults}. Call this at the start of a task "
            "to know whether you're being stealthy (safe for LinkedIn/TikTok/"
            "Instagram) or fast (safe for local dev automation)."
        ),
        inputSchema={"type": "object", "properties": {}},
    ),
    Tool(
        name="humanize_set",
        description=(
            "Toggle GUI humanization. Pass enabled=false when you need raw speed "
            "on a task that doesn't touch anti-bot-monitored sites (scripting, "
            "local dev, terminal work). Pass enabled=true to restore human timing "
            "for web platforms that fingerprint behavior. The change persists "
            "across tool calls until the daemon restarts or this tool is called "
            "again. Include a short reason for audit."
        ),
        inputSchema={
            "type": "object",
            "properties": {
                "enabled": {"type": "boolean"},
                "reason":  {"type": "string", "description": "Short rationale for the change"},
            },
            "required": ["enabled"],
        },
    ),

    # ── Memory ─────────────────────────────────────────────────
    Tool(
        name="memory_search",
        description="Full-text search across MEMORY.md and memory/*.md files in the workspace.",
        inputSchema={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Search terms (space-separated, all must match)"},
                "max_results": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    ),
    Tool(
        name="memory_read",
        description="Read a memory file from the workspace.",
        inputSchema={
            "type": "object",
            "properties": {
                "path": {"type": "string", "default": "MEMORY.md"},
                "offset": {"type": "integer", "default": 1},
                "limit": {"type": "integer", "default": 100},
            },
        },
    ),
    Tool(
        name="memory_append",
        description="Append a note to today's daily memory file or a specific file.",
        inputSchema={
            "type": "object",
            "properties": {
                "note": {"type": "string", "description": "The note to append"},
                "path": {"type": "string", "description": "Optional: specific file path"},
            },
            "required": ["note"],
        },
    ),
]


# ─── Route map ──────────────────────────────────────────────────

ROUTE_MAP = {
    "smart_wait": ("POST", "/smart_wait"),
    "wait_status": ("POST", "/wait_status"),
    "wait_update": ("POST", "/wait_update"),
    "wait_cancel": ("POST", "/wait_cancel"),
    "task_register": ("POST", "/task_register"),
    "task_update": ("POST", "/task_update"),
    "task_item_update": ("POST", "/task_item_update"),
    "task_plan_append": ("POST", "/task_plan_append"),
    "task_log_action": ("POST", "/task_log_action"),
    "task_summary": ("POST", "/task_summary"),
    "task_drill_down": ("POST", "/task_drill_down"),
    "task_thread": ("POST", "/task_thread"),
    "task_list": ("POST", "/task_list"),
    "health_check": ("GET", "/doctor"),
    "humanize_status": ("GET", "/humanize"),
    "humanize_set": ("POST", "/humanize"),
    "gui_agent": ("POST", "/gui_agent"),
    "gui_agent_cancel": ("POST", "/gui_agent/cancel"),
    "desktop_look": ("POST", "/desktop_look"),
    "video_record": ("POST", "/video_record"),
    "memory_search": ("POST", "/memory_search"),
    "memory_read": ("POST", "/memory_read"),
    "memory_append": ("POST", "/memory_append"),
}


# ─── Proxy handler ──────────────────────────────────────────────

_DISABLED_TOOLS = {
    name.strip().lower()
    for name in os.environ.get("ACU_DISABLED_TOOLS", "").split(",")
    if name.strip()
}


@app.list_tools()
async def list_tools():
    if not _DISABLED_TOOLS:
        return TOOLS
    return [t for t in TOOLS if t.name.lower() not in _DISABLED_TOOLS]


@app.call_tool()
async def call_tool(name: str, arguments: dict):
    if name.lower() in _DISABLED_TOOLS:
        return [TextContent(type="text", text=json.dumps({
            "error": f"Tool '{name}' is disabled via ACU_DISABLED_TOOLS",
            "disabled_tools": sorted(_DISABLED_TOOLS),
        }))]
    if name not in ROUTE_MAP:
        return [TextContent(type="text", text=json.dumps({"error": f"Unknown tool: {name}"}))]

    method, path = ROUTE_MAP[name]
    try:
        async with httpx.AsyncClient(timeout=300.0) as client:
            try:
                if method == "GET":
                    resp = await client.get(f"{DAEMON_URL}{path}")
                else:
                    resp = await client.post(f"{DAEMON_URL}{path}", json=arguments)
            except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout, asyncio.TimeoutError) as te:
                # Caller-side timeout on a long-running tool (gui_agent is the usual culprit).
                # Tell the daemon to stop the in-flight session so it doesn't wedge the event loop.
                if name == "gui_agent":
                    task_id = arguments.get("task_id")
                    cancel_body = {"task_id": task_id} if task_id else {"all": True}
                    try:
                        async with httpx.AsyncClient(timeout=5.0) as cc:
                            await cc.post(f"{DAEMON_URL}/gui_agent/cancel", json=cancel_body)
                    except Exception:
                        pass
                return [TextContent(type="text", text=json.dumps({
                    "error": f"Tool '{name}' timed out on MCP proxy ({type(te).__name__}).",
                    "hint": "Daemon was asked to cancel the in-flight session. Check task status / desktop_look before retrying.",
                }))]

            data = resp.json()
            if "image_b64" in data:
                isz = data.get("image_size", data.get("screen_size", {}))
                sz = data.get("screen_size", {})
                img_w, img_h = isz.get("width", "?"), isz.get("height", "?")
                scr_w, scr_h = sz.get("width", "?"), sz.get("height", "?")
                meta = (
                    f"Screenshot: {img_w}×{img_h}px image (actual display: {scr_w}×{scr_h}px). "
                    f"Specify x/y coordinates as pixel positions in this image "
                    f"(0,0 = top-left corner, {img_w},{img_h} = bottom-right). "
                    f"Coordinates are automatically scaled to screen space."
                )
                return [
                    ImageContent(type="image", data=data["image_b64"], mimeType=data.get("mime_type", "image/jpeg")),
                    TextContent(type="text", text=meta),
                ]
            return [TextContent(type="text", text=json.dumps(data))]
    except httpx.ConnectError:
        return [TextContent(type="text", text=json.dumps({
            "error": "DETM daemon is not running. Start it with: detm-daemon",
            "hint": "Run: detm-daemon  (or: python -m agentic_computer_use.daemon)"
        }))]
    except Exception as e:
        return [TextContent(type="text", text=json.dumps({"error": str(e)}))]


def main():
    log.info("Starting agentic-computer-use MCP server (proxy mode)")
    asyncio.run(_run())


async def _run():
    async with stdio_server() as (read_stream, write_stream):
        await app.run(read_stream, write_stream, app.create_initialization_options())


if __name__ == "__main__":
    main()
