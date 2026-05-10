"""Kimi K2.5 GUI agent backend (standard function calling via OpenRouter).

Kimi K2.5 / K2.6 is trained to use a unified `computer` function with
discrete action sub-types. There's no special tool type at the API
layer (no `computer_20251124` / `{"type":"computer"}`); we just expose a
JSON-Schema function with `action`/`x`/`y`/`text`/`direction` fields and
the model emits clean tool_calls. This is the open-source CUA tier
(Kimi K2.5 reports 63.3% on OSWorld-Verified using exactly this style).

Model selection: `KIMI_FC_MODEL` env var, default `moonshotai/kimi-k2.5`.
Endpoint: OpenRouter chat-completions (`OPENROUTER_API_KEY`).

Reference: https://www.kimi.com/blog/kimi-k2-5
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import time as _time
import uuid as _uuid

import httpx

from .. import config
from .. import debug as _dbg
from .actions import execute_action_logged
from .base import LiveUIProvider
from .openrouter import (
    _capture_jpeg_b64,
    _extract_model_text,
    _get_display_size,
    register_cancel_event,
    unregister_cancel_event,
)


_DEFAULT_MODEL = os.environ.get("KIMI_FC_MODEL", "moonshotai/kimi-k2.5")
_OPENROUTER_BASE = "https://openrouter.ai/api/v1"

_SYSTEM_PROMPT = """\
You are a GUI agent driving a Linux desktop via screenshots and structured actions.

Each turn you'll see a screenshot of the current screen state. Use the `computer` tool \
to interact: click at pixel coordinates, type text, press keys, scroll, etc. After each \
action you'll see an updated screenshot.

When you have completed the user's request, call `done` with a brief summary. If the \
task cannot be completed, call `failed` with the reason. Do not loop on screenshot \
without doing useful work.

Be precise with coordinates — they refer to the screenshot in actual pixel space \
(not normalized).
"""

# Unified computer function schema. The action sub-type determines what the
# rest of the args mean. Kimi K2.5 is trained on schemas in this shape.
_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "computer",
            "description": "Perform a single GUI action on the desktop.",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "click", "double_click", "right_click",
                            "type", "key", "scroll",
                            "move", "drag",
                            "wait", "screenshot",
                        ],
                        "description": "Which action to perform.",
                    },
                    "x": {"type": "integer", "description": "Pixel x for click/move/scroll/drag start."},
                    "y": {"type": "integer", "description": "Pixel y for click/move/scroll/drag start."},
                    "end_x": {"type": "integer", "description": "Pixel end x for drag."},
                    "end_y": {"type": "integer", "description": "Pixel end y for drag."},
                    "text": {"type": "string", "description": "Text for type, or key combo for key (e.g. 'ctrl+s', 'Return')."},
                    "direction": {"type": "string", "enum": ["up", "down", "left", "right"], "description": "For scroll."},
                    "amount": {"type": "integer", "description": "Scroll clicks (1-10)."},
                    "ms": {"type": "integer", "description": "For wait, milliseconds."},
                },
                "required": ["action"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Task complete. Provide a short summary.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "failed",
            "description": "Task cannot be completed.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}, "reason": {"type": "string"}},
                "required": ["summary"],
            },
        },
    },
]


def _translate_kimi_action(args: dict, display: str) -> tuple[str, str]:
    """Execute one Kimi `computer(...)` call. Returns (label, exec_result)."""
    action = args.get("action", "")

    if action == "screenshot":
        return ("screenshot", "ok")

    if action in ("click", "double_click", "right_click"):
        x, y = int(args.get("x", 0)), int(args.get("y", 0))
        if action == "double_click":
            return (f"double_click({x},{y})", execute_action_logged("double_click", {"x": x, "y": y}, display))
        if action == "right_click":
            return (f"right_click({x},{y})", execute_action_logged("click", {"x": x, "y": y, "button": "right"}, display))
        return (f"click({x},{y})", execute_action_logged("click", {"x": x, "y": y, "button": "left"}, display))

    if action == "move":
        x, y = int(args.get("x", 0)), int(args.get("y", 0))
        return (f"move({x},{y})", execute_action_logged("move_mouse", {"x": x, "y": y}, display))

    if action == "type":
        text = str(args.get("text", ""))
        return (f"type({text[:40]!r})", execute_action_logged("type_text", {"text": text}, display))

    if action == "key":
        key = str(args.get("text", ""))
        return (f"key({key})", execute_action_logged("key_press", {"key": key}, display))

    if action == "scroll":
        x, y = int(args.get("x", 0)), int(args.get("y", 0))
        direction = args.get("direction", "down")
        amount = max(1, min(20, int(args.get("amount", 3))))
        return (
            f"scroll({direction},{amount}@{x},{y})",
            execute_action_logged("scroll", {"x": x, "y": y, "direction": direction, "amount": amount}, display),
        )

    if action == "drag":
        sx, sy = int(args.get("x", 0)), int(args.get("y", 0))
        ex, ey = int(args.get("end_x", 0)), int(args.get("end_y", 0))
        return (
            f"drag(({sx},{sy})→({ex},{ey}))",
            execute_action_logged("drag", {"start_x": sx, "start_y": sy, "end_x": ex, "end_y": ey}, display),
        )

    if action == "wait":
        ms = int(args.get("ms", 1000))
        _time.sleep(min(ms / 1000.0, 5.0))
        return (f"wait({ms}ms)", "ok")

    return (f"unknown({action})", f"error: unknown action {action!r}")


class KimiFcProvider(LiveUIProvider):
    """Kimi K2.5 with a standard function-calling computer tool, via OpenRouter."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self._api_key = api_key or os.environ.get("OPENROUTER_API_KEY") or config.OPENROUTER_API_KEY
        self._model = model or _DEFAULT_MODEL
        self.provider = "kimi-fc"

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
            err = "OPENROUTER_API_KEY not set — kimi_fc backend cannot run"
            return {"error": err, "status": "error", "summary": err, "success": False,
                    "actions_taken": 0, "actions_log": []}

        cancel_key = session.id if session else f"no-session-{_uuid.uuid4().hex[:8]}"
        cancel_event = register_cancel_event(cancel_key, task_id)
        sid = session.id[:8] if session else "--------"
        disp_w, disp_h = await asyncio.get_running_loop().run_in_executor(None, _get_display_size, display)
        _dbg.log("LIVE", f"[{sid}] kimi_fc model={self._model} {disp_w}x{disp_h} task={task_id} timeout={timeout}s")

        t_start = _time.time()
        actions_taken = 0
        actions_log: list[str] = []
        usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}

        screenshot_b64, _ = await asyncio.get_running_loop().run_in_executor(
            None, _capture_jpeg_b64, display, session
        )
        if not screenshot_b64:
            return {"error": "screenshot failed", "status": "error", "summary": "screenshot failed",
                    "success": False, "actions_taken": 0, "actions_log": []}

        prompt_text = (
            f"Instruction: {instruction}\n\n"
            f"The desktop is {disp_w}×{disp_h} pixels. "
            f"Coordinates in tool calls are absolute pixel positions on this screen."
            + (f"\n\nContext:\n{context}" if context else "")
        )

        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "text", "text": prompt_text},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{screenshot_b64}"}},
            ]},
        ]

        try:
          async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
            try:
                async with asyncio.timeout(timeout):
                    while actions_taken < int(os.environ.get('ACU_GUI_AGENT_PER_CALL_MAX_TURNS', '5')):
                        if cancel_event.is_set():
                            return _result_cancelled(t_start, sid, session, actions_taken, actions_log, usage_total)

                        # Sliding context window: keep system + last 30 turns; strip stale images
                        if len(messages) > 32:
                            messages = messages[:1] + messages[-30:]
                        for m in messages[:-1]:
                            if m.get("role") == "user" and isinstance(m.get("content"), list):
                                m["content"] = [c for c in m["content"] if c.get("type") != "image_url"]

                        # Retry on transient 429/5xx with backoff.
                        resp = None
                        for retry in range(5):
                            try:
                                resp = await client.post(
                                    f"{_OPENROUTER_BASE}/chat/completions",
                                    headers={
                                        "Authorization": f"Bearer {self._api_key}",
                                        "Content-Type": "application/json",
                                        "HTTP-Referer": "https://github.com/openclaw/detm",
                                        "X-Title": f"DETM gui_agent (kimi_fc:{self._model})",
                                    },
                                    json={
                                        "model": self._model,
                                        "messages": messages,
                                        "tools": _TOOLS,
                                        "tool_choice": "auto",
                                        "max_tokens": 4096,
                                    },
                                )
                            except httpx.HTTPError as e:
                                err = f"API transport: {type(e).__name__}: {e}"[:300]
                                return _result_error(err, t_start, sid, session, actions_taken, actions_log, usage_total)
                            if resp.status_code in (429, 500, 502, 503, 504) and retry < 4:
                                wait_s = 2 ** retry
                                _dbg.log("LIVE", f"[{sid}] {resp.status_code} transient (retry {retry+1}/5 after {wait_s}s)")
                                await asyncio.sleep(wait_s)
                                if cancel_event.is_set():
                                    return _result_cancelled(t_start, sid, session, actions_taken, actions_log, usage_total)
                                continue
                            break
                        if resp is None or resp.status_code != 200:
                            status = resp.status_code if resp is not None else "no-response"
                            text = resp.text[:400] if resp is not None else "no body"
                            err = f"API HTTP {status}: {text}"
                            return _result_error(err, t_start, sid, session, actions_taken, actions_log, usage_total)

                        data = resp.json()
                        u = data.get("usage") or {}
                        usage_total["prompt_tokens"] += int(u.get("prompt_tokens", 0))
                        usage_total["completion_tokens"] += int(u.get("completion_tokens", 0))
                        usage_total["cached_tokens"] += int((u.get("prompt_tokens_details") or {}).get("cached_tokens", 0))
                        choice = data.get("choices", [{}])[0]
                        msg = choice.get("message", {})
                        tool_calls = msg.get("tool_calls") or []

                        narration = _extract_model_text(msg)
                        if narration:
                            _dbg.log("LIVE", f"[{sid}] thought: {narration[:160]}")
                            if session:
                                session.record_model_text(narration)

                        if not tool_calls:
                            # Model returned plain text — nudge it.
                            messages.append({"role": "assistant", "content": narration or ""})
                            messages.append({"role": "user", "content": "Call exactly one tool: `computer`, `done`, or `failed`."})
                            continue

                        tc = tool_calls[0]
                        fn_name = tc["function"]["name"]
                        tc_id = tc.get("id") or f"call_{actions_taken+1}"
                        try:
                            fn_args = json.loads(tc["function"]["arguments"])
                        except Exception:
                            messages.append({"role": "assistant", "content": narration or "", "tool_calls": [tc]})
                            messages.append({"role": "tool", "tool_call_id": tc_id,
                                             "content": "error: tool arguments must be valid JSON"})
                            continue

                        messages.append({"role": "assistant", "content": narration or "", "tool_calls": [tc]})

                        if fn_name == "done":
                            summary = str(fn_args.get("summary", ""))
                            if session:
                                session.record_done(True, summary)
                            messages.append({"role": "tool", "tool_call_id": tc_id, "content": "ok"})
                            return {
                                "success": True, "status": "complete", "summary": summary,
                                "escalated": False, "escalation_reason": "",
                                "actions_taken": actions_taken, "actions_log": actions_log[-10:],
                                "session_id": session.id if session else "",
                                "elapsed_s": round(_time.time() - t_start, 1),
                                "usage": usage_total,
                            }

                        if fn_name == "failed":
                            summary = str(fn_args.get("summary", ""))
                            reason = str(fn_args.get("reason", ""))
                            if session:
                                session.record_done(False, f"FAILED: {summary} | {reason}")
                            messages.append({"role": "tool", "tool_call_id": tc_id, "content": "ok"})
                            return {
                                "success": False, "status": "failed", "summary": summary,
                                "tried": [reason] if reason else [],
                                "escalated": False, "escalation_reason": "",
                                "actions_taken": actions_taken, "actions_log": actions_log[-10:],
                                "session_id": session.id if session else "",
                                "elapsed_s": round(_time.time() - t_start, 1),
                                "usage": usage_total,
                            }

                        if fn_name == "computer":
                            label, exec_result = _translate_kimi_action(fn_args, display)
                            actions_log.append(label)
                            actions_taken += 1
                            _dbg.log("LIVE", f"[{sid}] kimi#{actions_taken} {label} → {str(exec_result)[:80]}")
                            if session:
                                session.record_tool_call("computer", fn_args, tc_id)
                            await asyncio.sleep(0.4)

                            new_b64, _ = await asyncio.get_running_loop().run_in_executor(
                                None, _capture_jpeg_b64, display, session
                            )
                            messages.append({"role": "tool", "tool_call_id": tc_id, "content": str(exec_result)[:200]})
                            if new_b64:
                                messages.append({"role": "user", "content": [
                                    {"type": "text", "text": "[updated screenshot]"},
                                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{new_b64}"}},
                                ]})
                            continue

                        messages.append({"role": "tool", "tool_call_id": tc_id,
                                         "content": f"error: unknown function {fn_name!r}"})

                    _dbg.log("LIVE", f"[{sid}] hit max actions ({actions_taken})")
                    return {
                        "success": True, "status": "partial",
                        "summary": f"Reached per-call action cap ({actions_taken} turns). Sub-task may still be in progress.",
                        "remaining": "If the high-level instruction needs more steps, dispatch another gui_agent with the next concrete sub-step.",
                        "actions_taken": actions_taken, "actions_log": actions_log[-10:],
                        "session_id": session.id if session else "",
                        "elapsed_s": round(_time.time() - t_start, 1),
                        "usage": usage_total,
                    }
            except asyncio.TimeoutError:
                _dbg.log("LIVE", f"[{sid}] hard timeout after {timeout}s actions={actions_taken}")
                return {
                    "success": False, "status": "timeout",
                    "summary": f"Timed out after {timeout}s, actions={actions_taken}",
                    "actions_taken": actions_taken, "actions_log": actions_log[-10:],
                    "session_id": session.id if session else "",
                    "elapsed_s": round(_time.time() - t_start, 1),
                    "usage": usage_total,
                }
        finally:
            unregister_cancel_event(cancel_key, task_id)


def _result_cancelled(t_start, sid, session, actions_taken, actions_log, usage=None):
    if session:
        session.record_error("cancelled externally")
    return {
        "success": False, "status": "cancelled",
        "summary": f"Cancelled, actions={actions_taken}",
        "actions_taken": actions_taken, "actions_log": actions_log[-10:],
        "session_id": session.id if session else "",
        "elapsed_s": round(_time.time() - t_start, 1),
        "usage": usage or {},
    }


def _result_error(err, t_start, sid, session, actions_taken, actions_log, usage=None):
    if session:
        session.record_error(err)
    return {
        "error": err, "status": "error", "success": False,
        "summary": f"kimi_fc API error: {err[:200]}",
        "actions_taken": actions_taken, "actions_log": actions_log[-10:],
        "session_id": session.id if session else "",
        "elapsed_s": round(_time.time() - t_start, 1),
        "usage": usage or {},
    }


def kimi_fc_provider() -> KimiFcProvider:
    return KimiFcProvider()
