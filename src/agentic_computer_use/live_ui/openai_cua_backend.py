"""OpenAI native Computer Use backend.

Uses the Responses API with `tools=[{"type": "computer"}]` against
`api.openai.com`. The model (gpt-5.4 / gpt-5.5) emits a stream of
`computer_call` items, each containing an `actions[]` array of structured
actions: click, double_click, drag, move, scroll, keypress, type, wait,
screenshot. We dispatch each to the daemon's existing xdotool layer and
feed the resulting screenshot back via `previous_response_id` +
`computer_call_output`.

This is the configuration OpenAI uses to claim 75% on OSWorld-Verified.

Model selection: `OPENAI_CUA_MODEL` env var, default `gpt-5.4`.
API key: `OPENAI_API_KEY` (sk-proj-* or sk-*).

Reference: https://learn.microsoft.com/en-us/azure/ai-foundry/openai/how-to/computer-use
"""
from __future__ import annotations

import asyncio
import base64
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
    _get_display_size,
    register_cancel_event,
    unregister_cancel_event,
)


_DEFAULT_MODEL = os.environ.get("OPENAI_CUA_MODEL", "gpt-5.4")
_OPENAI_BASE = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")

_INSTRUCTIONS = (
    "You are a GUI agent driving a Linux desktop. You see screenshots "
    "and take actions: click, type, scroll, keypress, drag, etc. After "
    "each action you'll see an updated screenshot. When you have "
    "completed the user's request, return a final assistant message "
    "summarizing what you did — DO NOT keep emitting computer actions "
    "after the task is done."
)


# ── Action translation: OpenAI computer_call action → daemon execute_action ──

def _translate_openai_action(action: dict, display: str) -> tuple[str, str]:
    """Execute one OpenAI CUA action on the desktop. Returns (action_label, exec_result)."""
    atype = action.get("type")

    if atype == "screenshot":
        return ("screenshot", "ok")  # Caller handles the actual capture afterwards

    if atype == "click":
        x, y = int(action.get("x", 0)), int(action.get("y", 0))
        button = action.get("button", "left")
        if button == "back":
            # No native daemon action — emulate via xdotool key Alt+Left
            return ("nav_back", execute_action_logged("key_press", {"key": "alt+Left"}, display))
        if button == "forward":
            return ("nav_forward", execute_action_logged("key_press", {"key": "alt+Right"}, display))
        if button == "wheel":
            return ("wheel", execute_action_logged("scroll", {"x": x, "y": y, "direction": "down", "amount": 3}, display))
        return (
            f"click({button},{x},{y})",
            execute_action_logged("click", {"x": x, "y": y, "button": button}, display),
        )

    if atype == "double_click":
        x, y = int(action.get("x", 0)), int(action.get("y", 0))
        return (f"double_click({x},{y})", execute_action_logged("double_click", {"x": x, "y": y}, display))

    if atype == "move":
        x, y = int(action.get("x", 0)), int(action.get("y", 0))
        return (f"move({x},{y})", execute_action_logged("move_mouse", {"x": x, "y": y}, display))

    if atype == "scroll":
        x, y = int(action.get("x", 0)), int(action.get("y", 0))
        sx = int(action.get("scroll_x", 0))
        sy = int(action.get("scroll_y", 0))
        # OpenAI returns pixel offsets; daemon takes direction + amount (clicks).
        # Translate to direction + abs amount (clamped).
        direction = "down"
        amount = 3
        if abs(sy) > abs(sx):
            direction = "down" if sy > 0 else "up"
            amount = max(1, min(10, abs(sy) // 100))
        elif sx != 0:
            direction = "right" if sx > 0 else "left"
            amount = max(1, min(10, abs(sx) // 100))
        return (
            f"scroll({direction},{amount}@{x},{y})",
            execute_action_logged("scroll", {"x": x, "y": y, "direction": direction, "amount": amount}, display),
        )

    if atype == "keypress":
        keys = action.get("keys", []) or []
        if not keys:
            return ("keypress(empty)", "ok")
        # OpenAI returns canonical key names like ["Control", "C"]. Normalize to xdotool format.
        # For combos, join with "+". xdotool understands "ctrl+c", "Return", etc.
        norm = [str(k).strip() for k in keys if str(k).strip()]
        # Lowercase modifier prefixes; keep Letters/Digits as-is
        MODIFIERS = {"control", "ctrl", "shift", "alt", "meta", "cmd", "command", "super", "win"}
        parts = []
        for k in norm:
            kl = k.lower()
            if kl in MODIFIERS:
                # canonicalize to xdotool: control→ctrl, command/cmd/meta/super/win→super
                if kl in ("control", "ctrl"):
                    parts.append("ctrl")
                elif kl in ("command", "cmd", "meta", "super", "win"):
                    parts.append("super")
                else:
                    parts.append(kl)
            else:
                parts.append(k)
        combo = "+".join(parts)
        return (f"keypress({combo})", execute_action_logged("key_press", {"key": combo}, display))

    if atype == "type":
        text = str(action.get("text", ""))
        return (f"type({text[:40]!r})", execute_action_logged("type_text", {"text": text}, display))

    if atype == "wait":
        ms = int(action.get("ms", 1000))
        _time.sleep(min(ms / 1000.0, 5.0))
        return (f"wait({ms}ms)", "ok")

    if atype == "drag":
        path = action.get("path", []) or []
        if len(path) < 2:
            return (f"drag(invalid path len={len(path)})", "error: drag requires >=2 points")
        sx, sy = int(path[0].get("x", 0)), int(path[0].get("y", 0))
        ex, ey = int(path[-1].get("x", 0)), int(path[-1].get("y", 0))
        waypoints = [{"x": int(p.get("x", 0)), "y": int(p.get("y", 0))} for p in path[1:-1]]
        return (
            f"drag(({sx},{sy})→({ex},{ey}) via {len(waypoints)} pts)",
            execute_action_logged("drag", {"start_x": sx, "start_y": sy, "end_x": ex, "end_y": ey, "waypoints": waypoints}, display),
        )

    return (f"unknown({atype})", f"error: unknown action type {atype!r}")


class OpenAICuaProvider(LiveUIProvider):
    """OpenAI native Computer Use via Responses API."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self._api_key = api_key or os.environ.get("OPENAI_API_KEY", "")
        self._model = model or _DEFAULT_MODEL
        self._base = _OPENAI_BASE.rstrip("/")
        self.provider = "openai-cua"

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
            err = "OPENAI_API_KEY not set — openai_cua backend cannot run"
            return {"error": err, "status": "error", "summary": err, "success": False,
                    "actions_taken": 0, "actions_log": []}

        cancel_key = session.id if session else f"no-session-{_uuid.uuid4().hex[:8]}"
        cancel_event = register_cancel_event(cancel_key, task_id)
        sid = session.id[:8] if session else "--------"
        disp_w, disp_h = await asyncio.get_running_loop().run_in_executor(None, _get_display_size, display)
        _dbg.log("LIVE", f"[{sid}] openai_cua model={self._model} {disp_w}x{disp_h} task={task_id} timeout={timeout}s")

        t_start = _time.time()
        actions_taken = 0
        actions_log: list[str] = []
        # Token / cost accumulators across all turns of this gui_agent call.
        usage_total = {"input_tokens": 0, "output_tokens": 0, "reasoning_tokens": 0,
                       "input_cached_tokens": 0}

        # 1) Initial screenshot + first request.
        screenshot_b64, _ = await asyncio.get_running_loop().run_in_executor(
            None, _capture_jpeg_b64, display, session
        )
        if not screenshot_b64:
            err = f"Failed to capture screenshot from display {display}"
            return {"error": err, "status": "error", "summary": err, "success": False,
                    "actions_taken": 0, "actions_log": []}

        prompt_text = (
            f"Instruction: {instruction}\n\n"
            f"The desktop is {disp_w}×{disp_h} pixels. "
            f"You see the current state in the attached screenshot."
            + (f"\n\nContext:\n{context}" if context else "")
        )

        body = {
            "model": self._model,
            "tools": [{"type": "computer"}],
            "instructions": _INSTRUCTIONS,
            "reasoning": {"summary": "concise"},
            "input": [{
                "role": "user",
                "content": [
                    {"type": "input_text", "text": prompt_text},
                    {"type": "input_image", "image_url": f"data:image/jpeg;base64,{screenshot_b64}", "detail": "original"},
                ],
            }],
        }

        previous_response_id = None
        try:
          async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
            try:
                async with asyncio.timeout(timeout):
                    while actions_taken < int(os.environ.get('ACU_GUI_AGENT_PER_CALL_MAX_TURNS', '5')):  # action ceiling
                        if cancel_event.is_set():
                            return _result_cancelled(t_start, sid, session, actions_taken, actions_log, usage_total)

                        # Send request — with retry on transient 403 (permission propagation flakiness on
                        # newly-granted projects) and 429 (rate limit). Retries up to 4 times with
                        # exponential backoff before giving up.
                        resp = None
                        for retry in range(5):
                            try:
                                resp = await client.post(
                                    f"{self._base}/responses",
                                    headers={
                                        "Authorization": f"Bearer {self._api_key}",
                                        "Content-Type": "application/json",
                                    },
                                    json=body,
                                )
                            except httpx.HTTPError as e:
                                err = f"API transport: {type(e).__name__}: {e}"[:300]
                                _dbg.log("LIVE", f"[{sid}] {err}")
                                return _result_error(err, t_start, sid, session, actions_taken, actions_log, usage_total)
                            if resp.status_code in (403, 429, 500, 502, 503, 504) and retry < 4:
                                wait_s = 2 ** retry  # 1, 2, 4, 8 seconds
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
                            _dbg.log("LIVE", f"[{sid}] {err}")
                            return _result_error(err, t_start, sid, session, actions_taken, actions_log, usage_total)
                        data = resp.json()
                        previous_response_id = data.get("id")
                        # Accumulate token usage per turn.
                        u = data.get("usage") or {}
                        usage_total["input_tokens"] += int(u.get("input_tokens", 0))
                        usage_total["output_tokens"] += int(u.get("output_tokens", 0))
                        usage_total["reasoning_tokens"] += int((u.get("output_tokens_details") or {}).get("reasoning_tokens", 0))
                        usage_total["input_cached_tokens"] += int((u.get("input_tokens_details") or {}).get("cached_tokens", 0))

                        output = data.get("output") or []
                        # Extract: text messages, computer_calls, reasoning summaries.
                        computer_calls = [it for it in output if it.get("type") == "computer_call"]
                        text_msgs = [it for it in output if it.get("type") == "message"]

                        for tm in text_msgs:
                            for c in (tm.get("content") or []):
                                if c.get("type") == "output_text" and c.get("text"):
                                    if session:
                                        session.record_model_text(c["text"])
                                    _dbg.log("LIVE", f"[{sid}] model_text: {c['text'][:160]}")

                        if not computer_calls:
                            # Final message — model is done.
                            final_text = ""
                            for tm in text_msgs:
                                for c in (tm.get("content") or []):
                                    if c.get("type") == "output_text":
                                        final_text += c.get("text", "")
                            _dbg.log("LIVE", f"[{sid}] done: actions={actions_taken} -- {final_text[:80]}")
                            if session:
                                session.record_done(True, final_text or "task complete (no further actions)")
                            return {
                                "success": True, "status": "complete",
                                "summary": final_text or "Task complete.",
                                "escalated": False, "escalation_reason": "",
                                "actions_taken": actions_taken,
                                "actions_log": actions_log[-10:],
                                "session_id": session.id if session else "",
                                "elapsed_s": round(_time.time() - t_start, 1),
                                "usage": usage_total,
                            }

                        # Process the first computer_call (model usually emits one)
                        cc = computer_calls[0]
                        call_id = cc.get("call_id") or cc.get("id")
                        actions = cc.get("actions") or []
                        if not actions and cc.get("action"):
                            # Older single-action format
                            actions = [cc["action"]]
                        pending_safety = cc.get("pending_safety_checks") or []

                        for action in actions:
                            if cancel_event.is_set():
                                return _result_cancelled(t_start, sid, session, actions_taken, actions_log, usage_total)
                            label, exec_result = _translate_openai_action(action, display)
                            actions_log.append(label)
                            actions_taken += 1
                            _dbg.log("LIVE", f"[{sid}] cua#{actions_taken} {label} → {str(exec_result)[:80]}")
                            if session:
                                session.record_tool_call(action.get("type", "?"), action, call_id or "")
                            # Brief pause for UI to settle
                            await asyncio.sleep(0.4)

                        # 2) Capture new screenshot, send back as computer_call_output.
                        new_b64, _ = await asyncio.get_running_loop().run_in_executor(
                            None, _capture_jpeg_b64, display, session
                        )
                        if not new_b64:
                            new_b64 = screenshot_b64  # reuse if capture failed

                        ack = []
                        if pending_safety:
                            ack = [{"id": s.get("id"), "code": s.get("code"), "message": s.get("message")}
                                   for s in pending_safety]

                        next_input = [{
                            "type": "computer_call_output",
                            "call_id": call_id,
                            "output": {
                                "type": "computer_screenshot",
                                "image_url": f"data:image/jpeg;base64,{new_b64}",
                                "detail": "original",
                            },
                        }]
                        if ack:
                            next_input[0]["acknowledged_safety_checks"] = ack

                        body = {
                            "model": self._model,
                            "tools": [{"type": "computer"}],
                            "previous_response_id": previous_response_id,
                            "input": next_input,
                        }

                    # max actions exceeded
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
        # If we somehow fall through (shouldn't happen — every branch returns above),
        # produce an explicit fallback so the daemon never sees None.
        return {
            "success": False, "status": "error",
            "summary": "openai_cua: fell through main loop unexpectedly",
            "actions_taken": actions_taken, "actions_log": actions_log[-10:],
            "session_id": session.id if session else "",
            "elapsed_s": round(_time.time() - t_start, 1),
            "usage": usage_total,
        }


def _result_cancelled(t_start, sid, session, actions_taken, actions_log, usage=None):
    _dbg.log("LIVE", f"[{sid}] cancelled actions={actions_taken}")
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
        "summary": f"openai_cua API error: {err[:200]}",
        "actions_taken": actions_taken, "actions_log": actions_log[-10:],
        "session_id": session.id if session else "",
        "elapsed_s": round(_time.time() - t_start, 1),
        "usage": usage or {},
    }


def openai_cua_provider() -> OpenAICuaProvider:
    return OpenAICuaProvider()
