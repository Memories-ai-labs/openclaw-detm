"""Bash GUI backend — minimal harness, model uses raw shell commands.

Premise: a frontier LLM that's "supposed to be able to use a GUI" doesn't need a
structured action schema. Give it `bash(command)`, give it screenshots between
commands, get out of the way. This matches the model's training distribution
(shell + xdotool sequences) instead of asking it to translate intent into our
specific schema and then babying it through the translation.

Tools exposed to the model:
  - bash(command, timeout?)   → stdout/stderr/exit + post-execution screenshot
  - screenshot()              → fresh screenshot, no exec
  - done(summary)             → terminal: task complete (subject to verification)
  - partial(summary, remaining)  → terminal: progress made, work remains
  - failed(summary, tried)    → terminal: blocked
  - escalate(reason)          → terminal: needs human

Compared with `live_ui.direct.DirectGuiProvider` (structured tool wrapper):
  - No click/type_text/key_press/scroll schema — model writes xdotool itself
  - Tool result is real stdout/stderr, not "ok" — actual diagnostic feedback
  - Can compose multi-step actions in one call: `xdotool key ctrl+l ; xdotool type 'foo' ; xdotool key Return`
  - Loses centralized --clearmodifiers enforcement; model has to remember
    (system prompt suggests it; the model figures out the rest)

This backend is intentionally NOT a drop-in for `direct.py` — different philosophy.
"""
from __future__ import annotations

import asyncio
import json
import os
import time as _time
import uuid as _uuid

import httpx

from .. import config
from .. import debug as _dbg
from .base import LiveUIProvider
from .openrouter import (
    _capture_jpeg_b64,
    _extract_model_text,
    _get_display_size,
    _record_usage,
    register_cancel_event,
    unregister_cancel_event,
)


_SYSTEM_PROMPT = """\
You are operating a desktop GUI on a Linux machine via shell commands. You see screenshots and execute bash commands (typically xdotool, wmctrl, scrot, etc.) to interact with the screen.

## Loop
Each turn you receive a screenshot of the current state. You decide what shell command to run. After execution you receive stdout/stderr + a fresh screenshot.

## Tools
- `bash(command)` — run shell command on DISPLAY=:99. Returns stdout, stderr, exit_code, and a post-execution screenshot. Compose sequences with `;` or `&&`.
- `screenshot()` — re-capture the screen without executing anything. Use after waiting for slow page loads.
- `done(summary)` — task fully complete. Triggers verification. Don't call for partial work.
- `partial(summary, remaining)` — progress made, work remains. Caller will continue.
- `failed(summary, tried)` — blocked on a specific issue.
- `escalate(reason)` — needs human (login, CAPTCHA, 2FA).

## Tips
- DISPLAY is already exported in the bash env you receive — you can call `xdotool` etc. directly.
- Use `xdotool --clearmodifiers` on click/key actions to avoid latched-modifier bugs.
- **xdotool `type` is greedy** — per `man xdotool`, it "consumes the remainder of the arguments and types them. That is, no commands can chain after 'type'." So `xdotool key ctrl+a type 'foo' key Return` types literally `"foo key Return"` (the words `key` and `Return` get typed as text, NOT pressed). To press Return after typing, use a SEPARATE invocation, joined by `;`: `xdotool key --clearmodifiers ctrl+a; xdotool type 'foo'; xdotool key --clearmodifiers Return`. The same applies to any other subcommand you want to run after `type`.
- After actions that trigger UI updates (clicks on menus, page loads), the post-execution screenshot is taken ~0.4s after your command finishes — for slower changes call `screenshot()` after waiting.
- If a command produces unexpected output or no visible change, vary your approach instead of repeating the same command.
- You can use any standard tool: `wmctrl` for window listing, `xdotool getmouselocation`, `scrot` for ad-hoc screenshots, `xclip` for clipboard.

## Completion
Pick exactly one terminal tool when finished. The summary you return is the only thing the caller sees — be specific.
"""


_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "Run a shell command on DISPLAY=:99. Returns stdout, stderr, exit_code, and a post-execution screenshot. Compose multiple xdotool/wmctrl/scrot etc. with ; or &&.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run."},
                    "timeout": {"type": "number", "description": "Max seconds before killing the command. Default 10, max 30."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "screenshot",
            "description": "Re-capture the screen without running any command. Useful after waiting for slow loads.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Task FULLY complete. Triggers verification. Do NOT call for partial work — use 'partial' instead.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string", "description": "What was accomplished and what the screen shows now."},
                },
                "required": ["summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "partial",
            "description": "Real progress was made but work remains. Caller will continue with a follow-up.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "remaining": {"type": "string"},
                },
                "required": ["summary", "remaining"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "failed",
            "description": "Blocked by a specific issue. Not for time pressure — use 'partial' for that.",
            "parameters": {
                "type": "object",
                "properties": {
                    "summary": {"type": "string"},
                    "tried": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["summary", "tried"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "escalate",
            "description": "Needs human (login wall, CAPTCHA, 2FA).",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}},
                "required": ["reason"],
            },
        },
    },
]


async def _run_bash(
    command: str,
    display: str,
    timeout: float = 10.0,
    cancel_event: asyncio.Event | None = None,
) -> dict:
    """Run a shell command with DISPLAY pre-exported. Returns dict with stdout/stderr/exit_code.

    Async + cancel-aware: races subprocess completion against `cancel_event` and the
    per-command timeout. On either, sends SIGKILL to the process group so backgrounded
    xdotool/sleep dies too. Decoder uses errors="replace" so binary stdout doesn't crash.

    exit_code conventions:
       0..255   normal subprocess exit
       -1       killed after per-command timeout
       -2       killed by cancel_event
    """
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "DISPLAY": display},
            start_new_session=True,  # so killpg reaches grandchildren too
        )
    except Exception as e:
        return {"stdout": "", "stderr": f"{type(e).__name__}: {e}", "exit_code": -1}

    def _kill_group() -> None:
        try:
            os.killpg(os.getpgid(proc.pid), 9)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    comm_task = asyncio.ensure_future(proc.communicate())
    waiters: list[asyncio.Future] = [comm_task]
    cancel_task: asyncio.Task | None = None
    if cancel_event is not None:
        cancel_task = asyncio.ensure_future(cancel_event.wait())
        waiters.append(cancel_task)

    try:
        done, _ = await asyncio.wait(waiters, timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        _kill_group()
        for w in waiters:
            if not w.done():
                w.cancel()
        raise

    cancelled = cancel_task is not None and cancel_task in done
    timed_out = comm_task not in done and not cancelled

    if cancelled or timed_out:
        _kill_group()
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=2)
        except (asyncio.TimeoutError, Exception):
            stdout, stderr = b"", b""
        if cancel_task is not None and not cancel_task.done():
            cancel_task.cancel()
        if not comm_task.done():
            comm_task.cancel()
        marker = "[cancelled]" if cancelled else f"[killed after {timeout}s timeout]"
        return {
            "stdout": (stdout or b"").decode("utf-8", errors="replace")[:2000],
            "stderr": ((stderr or b"").decode("utf-8", errors="replace") + f"\n{marker}")[:1000],
            "exit_code": -2 if cancelled else -1,
        }

    if cancel_task is not None and not cancel_task.done():
        cancel_task.cancel()
    try:
        stdout, stderr = comm_task.result()
    except Exception as e:
        return {"stdout": "", "stderr": f"{type(e).__name__}: {e}", "exit_code": -1}
    return {
        "stdout": (stdout or b"").decode("utf-8", errors="replace")[:2000],
        "stderr": (stderr or b"").decode("utf-8", errors="replace")[:1000],
        "exit_code": proc.returncode if proc.returncode is not None else -1,
    }


async def _race_against_cancel(awaitable, cancel_event: asyncio.Event):
    """Race `awaitable` against `cancel_event.wait()`.

    Returns (result, cancelled). If cancel fired first, the underlying task is cancelled
    and (None, True) is returned. If the awaitable completed (or raised), (result, False)
    is returned — the caller can call .result() to get the value or re-raise the error.
    """
    main_task = asyncio.ensure_future(awaitable)
    cancel_task = asyncio.ensure_future(cancel_event.wait())
    try:
        done, _ = await asyncio.wait({main_task, cancel_task}, return_when=asyncio.FIRST_COMPLETED)
    except BaseException:
        main_task.cancel()
        cancel_task.cancel()
        raise
    if cancel_task in done:
        main_task.cancel()
        try:
            await main_task
        except (asyncio.CancelledError, Exception):
            pass
        return None, True
    if not cancel_task.done():
        cancel_task.cancel()
    return main_task, False


class BashGuiProvider(LiveUIProvider):
    """Bash-only GUI agent. Model writes shell commands; we execute and feed back."""

    def __init__(self, base_url: str, api_key: str, model: str, label: str = "bash"):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model = model
        self._label = label

    async def run(
        self,
        instruction: str,
        timeout: int,
        task_id: str | None,
        display: str,
        context: str = "",
        session=None,
    ) -> dict:
        if not self._api_key:
            err = f"{self._label} provider missing API key"
            return {"error": err, "status": "error", "summary": err, "success": False,
                    "actions_taken": 0, "actions_log": []}

        # Use a uuid-suffixed key when there's no session, so concurrent untagged calls
        # don't collide on a shared "no-session" entry in the cancel registry.
        cancel_key = session.id if session else f"no-session-{_uuid.uuid4().hex[:8]}"
        cancel_event = register_cancel_event(cancel_key, task_id)
        sid = session.id[:8] if session else "--------"
        disp_w, disp_h = _get_display_size(display)
        _dbg.log("LIVE", f"[{sid}] bash[{self._label}] model={self._model} {disp_w}x{disp_h} task={task_id} timeout={timeout}s")

        system_text = _SYSTEM_PROMPT + (f"\n\nContext:\n{context}" if context else "")
        system_text += (
            f"\n\nThe screen is {disp_w}×{disp_h} pixels. "
            f"The display is {display} — already exported in your bash env."
        )

        t_start = _time.time()
        MAX_TURNS = 60
        MAX_FORMAT_RETRIES = 6
        CONTEXT_WINDOW = 30

        actions_taken = 0
        actions_log: list[str] = []
        action_turns = 0
        format_retries = 0

        screenshot_b64, _ = await asyncio.get_running_loop().run_in_executor(
            None, _capture_jpeg_b64, display, session
        )
        if not screenshot_b64:
            err = f"Failed to capture screenshot from display {display}"
            return {"error": err, "status": "error", "summary": err, "success": False,
                    "actions_taken": 0, "actions_log": [],
                    "session_id": session.id if session else "",
                    "elapsed_s": round(_time.time() - t_start, 1)}

        messages: list[dict] = [
            {"role": "system", "content": system_text},
            {"role": "user", "content": [
                {"type": "text", "text": f"Instruction: {instruction} [current screenshot]"},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"}},
            ]},
        ]

        try:
          async with httpx.AsyncClient(timeout=httpx.Timeout(60.0)) as client:
            try:
                async with asyncio.timeout(timeout):
                    while action_turns < MAX_TURNS and format_retries < MAX_FORMAT_RETRIES:
                        turn_no = action_turns + format_retries + 1

                        if cancel_event.is_set():
                            _dbg.log("LIVE", f"[{sid}] cancelled at turn {turn_no}")
                            if session:
                                session.record_error("cancelled externally")
                            return {"success": False, "status": "cancelled",
                                    "summary": f"Cancelled at turn {turn_no}, actions={actions_taken}",
                                    "actions_taken": actions_taken, "actions_log": actions_log[-10:],
                                    "session_id": session.id if session else "",
                                    "elapsed_s": round(_time.time() - t_start, 1)}

                        # Sliding context: keep system + last N msgs, strip stale images
                        if len(messages) > CONTEXT_WINDOW + 2:
                            messages = messages[:1] + messages[-(CONTEXT_WINDOW + 1):]
                        for m in messages[:-1]:
                            if m.get("role") == "system":
                                continue
                            if isinstance(m.get("content"), list):
                                m["content"] = [c for c in m["content"] if c.get("type") != "image_url"]

                        t0 = asyncio.get_running_loop().time()
                        api_coro = client.post(
                            f"{self._base_url}/chat/completions",
                            headers={
                                "Authorization": f"Bearer {self._api_key}",
                                "Content-Type": "application/json",
                                "HTTP-Referer": "https://github.com/openclaw/detm",
                                "X-Title": f"DETM gui_agent (bash:{self._model})",
                            },
                            json={
                                "model": self._model,
                                "messages": messages,
                                "tools": _TOOLS,
                                "tool_choice": "auto",
                                "max_tokens": 5000,
                            },
                        )
                        # Race the API call against cancel so a click on cancel doesn't
                        # have to wait the full httpx timeout (60s) before taking effect.
                        api_task, was_cancelled = await _race_against_cancel(api_coro, cancel_event)
                        api_ms = (asyncio.get_running_loop().time() - t0) * 1000
                        if was_cancelled:
                            _dbg.log("LIVE", f"[{sid}] cancelled mid-API-call ({api_ms:.0f}ms)")
                            if session:
                                session.record_error("cancelled externally (mid-API)")
                            return {"success": False, "status": "cancelled",
                                    "summary": f"Cancelled mid-API at turn {turn_no}, actions={actions_taken}",
                                    "actions_taken": actions_taken, "actions_log": actions_log[-10:],
                                    "session_id": session.id if session else "",
                                    "elapsed_s": round(_time.time() - t_start, 1)}
                        try:
                            resp = api_task.result()
                        except (httpx.HTTPError, asyncio.TimeoutError) as e:
                            err = f"API transport error: {type(e).__name__}: {e!s}"[:300]
                            _dbg.log("LIVE", f"[{sid}] {err} ({api_ms:.0f}ms)")
                            if session:
                                session.record_error(err)
                            return {"error": err, "status": "error",
                                    "summary": f"GUI agent API error: {err[:200]}",
                                    "success": False, "actions_taken": actions_taken,
                                    "actions_log": actions_log[-10:],
                                    "session_id": session.id if session else "",
                                    "elapsed_s": round(_time.time() - t_start, 1)}

                        if resp.status_code != 200:
                            err = f"API HTTP {resp.status_code}: {resp.text[:300]}"
                            _dbg.log("LIVE", f"[{sid}] {err}")
                            if session:
                                session.record_error(err)
                            return {"error": err, "status": "error",
                                    "summary": f"GUI agent API error: {err[:200]}",
                                    "success": False, "actions_taken": actions_taken,
                                    "actions_log": actions_log[-10:],
                                    "session_id": session.id if session else "",
                                    "elapsed_s": round(_time.time() - t_start, 1)}

                        try:
                            data = resp.json()
                        except (json.JSONDecodeError, ValueError) as e:
                            err = f"API non-JSON response: {type(e).__name__}: {resp.text[:200]}"
                            _dbg.log("LIVE", f"[{sid}] {err}")
                            if session:
                                session.record_error(err)
                            return {"error": err, "status": "error",
                                    "summary": f"GUI agent API error: {err[:200]}",
                                    "success": False, "actions_taken": actions_taken,
                                    "actions_log": actions_log[-10:],
                                    "session_id": session.id if session else "",
                                    "elapsed_s": round(_time.time() - t_start, 1)}
                        # Record token usage for THIS turn — must be per-turn so multi-turn
                        # sessions don't silently drop everything except the final response.
                        _record_usage(self._model, task_id, data)
                        _dbg.log("LIVE", f"[{sid}] raw_response: {json.dumps(data)[:2000]}")
                        _usage = data.get("usage", {}) or {}
                        _ptd = _usage.get("prompt_tokens_details", {}) or {}
                        _dbg.log(
                            "LIVE",
                            f"[{sid}] cache: prompt={_usage.get('prompt_tokens', 0)} "
                            f"cached={_ptd.get('cached_tokens', 0)} "
                            f"completion={_usage.get('completion_tokens', 0)} "
                            f"finish={data.get('choices', [{}])[0].get('finish_reason', '?')} "
                            f"api_ms={api_ms:.0f}",
                        )

                        choice = data["choices"][0]
                        msg = choice["message"]
                        tool_calls = msg.get("tool_calls") or []

                        if not tool_calls:
                            finish_reason = choice.get("finish_reason", "")
                            _dbg.log("LIVE", f"[{sid}] no tool call ({api_ms:.0f}ms) finish={finish_reason}")
                            if finish_reason == "length":
                                messages.append({"role": "user", "content":
                                    "Your response was truncated. Produce a tool call IMMEDIATELY with brief reasoning."})
                            else:
                                messages.append({"role": "assistant", "content": _extract_model_text(msg) or ""})
                                messages.append({"role": "user", "content": "Call exactly one tool."})
                            format_retries += 1
                            continue

                        tc = tool_calls[0]
                        # NOTE: don't reset format_retries here — reset only when an action
                        # actually executes successfully. Otherwise a model that alternates
                        # parseable-but-broken tool calls with unknown-tool errors can burn
                        # the entire timeout without ever hitting MAX_FORMAT_RETRIES.
                        narration = _extract_model_text(msg)
                        messages.append({
                            "role": "assistant",
                            "content": narration or "",
                            "tool_calls": [tc],
                        })

                        fn_name = tc["function"]["name"]
                        # Some providers return null tool_call IDs — fall back to a synthesized
                        # one so subsequent role:tool messages don't break the schema.
                        tc_id = tc.get("id") or f"call_{turn_no}"
                        try:
                            fn_args = json.loads(tc["function"]["arguments"])
                        except Exception:
                            messages.append({"role": "tool", "tool_call_id": tc_id,
                                             "content": "error: could not parse tool arguments — return valid JSON."})
                            format_retries += 1
                            continue
                        if not isinstance(fn_args, dict):
                            messages.append({"role": "tool", "tool_call_id": tc_id,
                                             "content": "error: tool arguments must be a JSON object, not an array or scalar."})
                            format_retries += 1
                            continue

                        if narration:
                            _dbg.log("LIVE", f"[{sid}] thought: {narration[:160]}")
                            if session:
                                session.record_model_text(narration)

                        if session:
                            session.record_tool_call(fn_name, fn_args, tc_id)

                        # ── Terminal actions ─────────────────────────────
                        if fn_name == "done":
                            summary = str(fn_args.get("summary", ""))
                            if session:
                                session.record_done(True, summary)
                            messages.append({"role": "tool", "tool_call_id": tc_id, "content": "ok"})
                            _dbg.log("LIVE", f"[{sid}] done: actions={actions_taken} -- {summary[:80]}")
                            return {"success": True, "status": "complete", "summary": summary,
                                    "escalated": False, "escalation_reason": "",
                                    "actions_taken": actions_taken, "actions_log": actions_log[-10:],
                                    "session_id": session.id if session else "",
                                    "elapsed_s": round(_time.time() - t_start, 1)}

                        if fn_name == "partial":
                            summary = str(fn_args.get("summary", ""))
                            remaining = str(fn_args.get("remaining", ""))
                            if session:
                                session.record_done(True, f"PARTIAL: {summary} | remaining: {remaining}")
                            messages.append({"role": "tool", "tool_call_id": tc_id, "content": "ok"})
                            _dbg.log("LIVE", f"[{sid}] partial: {summary[:80]} | remaining: {remaining[:80]}")
                            return {"success": True, "status": "partial", "summary": summary,
                                    "remaining": remaining,
                                    "escalated": False, "escalation_reason": "",
                                    "actions_taken": actions_taken, "actions_log": actions_log[-10:],
                                    "session_id": session.id if session else "",
                                    "elapsed_s": round(_time.time() - t_start, 1)}

                        if fn_name == "failed":
                            summary = str(fn_args.get("summary", ""))
                            tried_raw = fn_args.get("tried", [])
                            if isinstance(tried_raw, str):
                                tried = [tried_raw]
                            elif isinstance(tried_raw, list):
                                tried = [str(x) for x in tried_raw]
                            else:
                                tried = []
                            if session:
                                session.record_done(False, f"FAILED: {summary} | tried: {'; '.join(tried)}")
                            messages.append({"role": "tool", "tool_call_id": tc_id, "content": "ok"})
                            _dbg.log("LIVE", f"[{sid}] failed: {summary[:80]}")
                            return {"success": False, "status": "failed", "summary": summary, "tried": tried,
                                    "escalated": False, "escalation_reason": "",
                                    "actions_taken": actions_taken, "actions_log": actions_log[-10:],
                                    "session_id": session.id if session else "",
                                    "elapsed_s": round(_time.time() - t_start, 1)}

                        if fn_name == "escalate":
                            reason = str(fn_args.get("reason", ""))
                            if session:
                                session.record_escalate(reason)
                            messages.append({"role": "tool", "tool_call_id": tc_id, "content": "ok"})
                            _dbg.log("LIVE", f"[{sid}] escalate: {reason[:120]}")
                            return {"success": False, "status": "escalated",
                                    "escalated": True, "escalation_reason": reason,
                                    "summary": f"Escalated: {reason}",
                                    "actions_taken": actions_taken, "actions_log": actions_log[-10:],
                                    "session_id": session.id if session else "",
                                    "elapsed_s": round(_time.time() - t_start, 1)}

                        # ── screenshot: re-capture without executing ──
                        if fn_name == "screenshot":
                            shot_b64, _ = await asyncio.get_running_loop().run_in_executor(
                                None, _capture_jpeg_b64, display, session
                            )
                            messages.append({"role": "tool", "tool_call_id": tc_id, "content": "ok"})
                            if shot_b64:
                                messages.append({"role": "user", "content": [
                                    {"type": "text", "text": "[fresh screenshot]"},
                                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{shot_b64}"}},
                                ]})
                            # Count screenshots toward action_turns so a model that loops on
                            # screenshot() can't burn the whole timeout budget without ever
                            # executing a real action.
                            action_turns += 1
                            format_retries = 0
                            continue

                        # ── bash: run command, return stdout/stderr/exit + screenshot ──
                        if fn_name == "bash":
                            cmd_raw = str(fn_args.get("command", ""))
                            CMD_LIMIT = 4000
                            if len(cmd_raw) > CMD_LIMIT:
                                # Don't silently truncate — return an error so the model can split.
                                err_text = (
                                    f"error: command is {len(cmd_raw)} chars (max {CMD_LIMIT}). "
                                    "Split into multiple bash() calls or write to a temp file with heredoc + bash $tmp."
                                )
                                messages.append({"role": "tool", "tool_call_id": tc_id, "content": err_text})
                                format_retries += 1
                                continue
                            cmd = cmd_raw
                            cmd_timeout = min(max(float(fn_args.get("timeout", 10)), 1), 30)
                            action_turns += 1
                            actions_log.append(f"bash: {cmd[:80]}")
                            _dbg.log("LIVE", f"[{sid}] bash#{action_turns} (timeout={cmd_timeout}s): {cmd[:200]}")

                            # Pass cancel_event so a dashboard cancel kills any in-flight
                            # subprocess (e.g. `sleep 30`, hung X command) instead of waiting
                            # for cmd_timeout. exit_code -2 indicates cancel.
                            result = await _run_bash(cmd, display, cmd_timeout, cancel_event)
                            if result["exit_code"] == -2:
                                _dbg.log("LIVE", f"[{sid}] bash cancelled mid-execution")
                                if session:
                                    session.record_error("cancelled externally (mid-bash)")
                                return {"success": False, "status": "cancelled",
                                        "summary": f"Cancelled mid-bash at turn {turn_no}, actions={actions_taken}",
                                        "actions_taken": actions_taken, "actions_log": actions_log[-10:],
                                        "session_id": session.id if session else "",
                                        "elapsed_s": round(_time.time() - t_start, 1)}
                            actions_taken += 1
                            _dbg.log(
                                "LIVE",
                                f"[{sid}] bash exit={result['exit_code']} "
                                f"stdout={len(result['stdout'])}b stderr={len(result['stderr'])}b",
                            )

                            tool_text = (
                                f"exit_code: {result['exit_code']}\n"
                                f"stdout: {result['stdout'] or '(empty)'}\n"
                                f"stderr: {result['stderr'] or '(empty)'}"
                            )
                            messages.append({"role": "tool", "tool_call_id": tc_id, "content": tool_text})

                            await asyncio.sleep(0.4)
                            shot_b64, _ = await asyncio.get_running_loop().run_in_executor(
                                None, _capture_jpeg_b64, display, session
                            )
                            if shot_b64:
                                messages.append({"role": "user", "content": [
                                    {"type": "text", "text": "[updated screenshot]"},
                                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{shot_b64}"}},
                                ]})
                            if session:
                                session.record_tool_response(fn_name, tc_id, tool_text)
                            format_retries = 0  # successful action — reset the format-retry counter
                            continue

                        # Unknown tool
                        messages.append({"role": "tool", "tool_call_id": tc_id,
                                         "content": f"error: unknown tool {fn_name!r}. Use bash, screenshot, done, partial, failed, or escalate."})
                        format_retries += 1
                        continue

                    _dbg.log("LIVE", f"[{sid}] turn budget exhausted (turns={turn_no} actions={actions_taken})")
                    return {"success": False, "status": "partial",
                            "summary": f"Turn budget exhausted after {actions_taken} bash calls",
                            "remaining": "model never called a terminal tool",
                            "actions_taken": actions_taken, "actions_log": actions_log[-10:],
                            "session_id": session.id if session else "",
                            "elapsed_s": round(_time.time() - t_start, 1)}

            except asyncio.TimeoutError:
                _dbg.log("LIVE", f"[{sid}] hard timeout after {timeout}s actions={actions_taken}")
                return {"success": False, "status": "partial",
                        "summary": f"Hit {timeout}s timeout after {actions_taken} bash calls",
                        "remaining": (actions_log[-1] if actions_log else "") + " — caller should resume",
                        "escalated": False, "escalation_reason": "",
                        "actions_taken": actions_taken, "actions_log": actions_log[-10:],
                        "session_id": session.id if session else "",
                        "elapsed_s": round(_time.time() - t_start, 1)}
        finally:
            # Always release the cancel-event registry entry so it doesn't leak across runs.
            unregister_cancel_event(cancel_key, task_id)


def openrouter_bash_provider() -> BashGuiProvider:
    return BashGuiProvider(
        base_url="https://openrouter.ai/api/v1",
        api_key=config.OPENROUTER_API_KEY,
        model=config.OPENROUTER_GUI_DIRECT_MODEL,
        label="openrouter",
    )
