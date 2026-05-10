"""Anthropic native Computer Use backend.

Uses the Messages API with `tools=[{"type":"computer_20251124", ...}]` and
the `computer-use-2025-11-24` beta header against `api.anthropic.com`.
The model (claude-opus-4-7 / claude-sonnet-4-6 / claude-opus-4-6 /
claude-opus-4-5) emits structured `tool_use` blocks for the `computer`
tool with action types: screenshot, left_click, type, key, scroll,
mouse_move, left_click_drag, double_click, hold_key, wait, etc.

This is the configuration Anthropic uses to claim 72.5% on OSWorld-Verified.

Coordinate scaling:
  - claude-opus-4-7: native up to 2576px on long edge — no scaling needed
  - older models: max 1568px long edge, ~1.15MP total. Screenshots are
    downscaled before send; click coords from the model are scaled back up
    to original screen pixels.

Model selection: `ANTHROPIC_CUA_MODEL` env var, default `claude-opus-4-7`.
API key: `ANTHROPIC_API_KEY` (sk-ant-api03-*).

Reference: https://platform.claude.com/docs/en/agents-and-tools/tool-use/computer-use-tool
"""
from __future__ import annotations

import asyncio
import base64
import io
import math
import os
import time as _time
import uuid as _uuid

import httpx

from .. import debug as _dbg
from .actions import execute_action_logged
from .base import LiveUIProvider
from .openrouter import (
    _capture_jpeg_b64,
    _get_display_size,
    register_cancel_event,
    unregister_cancel_event,
)


_DEFAULT_MODEL = os.environ.get("ANTHROPIC_CUA_MODEL", "claude-opus-4-7")
_API_BASE = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com/v1")

# claude-opus-4-7 supports native res up to 2576px on the long edge.
# Other 4.x models follow the older 1568px long-edge / 1.15MP cap.
_NATIVE_HIGH_RES_MODELS = {"claude-opus-4-7"}

_MAX_LONG_EDGE_LEGACY = 1568
_MAX_PIXELS_LEGACY = 1_150_000


def _scale_factor(w: int, h: int, model: str) -> float:
    if model in _NATIVE_HIGH_RES_MODELS:
        # Long edge cap of 2576; honor it but most desktops are well under.
        long_edge = max(w, h)
        if long_edge <= 2576:
            return 1.0
        return 2576.0 / long_edge
    long_edge = max(w, h)
    pixels = w * h
    return min(1.0, _MAX_LONG_EDGE_LEGACY / long_edge, math.sqrt(_MAX_PIXELS_LEGACY / pixels))


def _resize_jpeg_b64(b64: str, scale: float) -> tuple[str, int, int]:
    """Decode JPEG b64, resize, return (resized_b64, new_w, new_h). If scale==1.0,
    decode just to get dims and re-encode to JPEG quality 85 to be safe."""
    try:
        from PIL import Image
        raw = base64.b64decode(b64)
        img = Image.open(io.BytesIO(raw))
        img.load()
        w, h = img.size
        if scale != 1.0:
            new_w, new_h = int(w * scale), int(h * scale)
            img = img.resize((new_w, new_h), Image.LANCZOS)
        else:
            new_w, new_h = w, h
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode("ascii"), new_w, new_h
    except Exception:
        # Fall back: assume no scaling and return original
        return b64, 0, 0


# ── Action translation: Anthropic computer action → daemon execute_action ──

def _xdotool_modifier(text: str | None) -> list[str]:
    """Anthropic 'text' on click/scroll is a modifier hold (shift|ctrl|alt|super).
    Returns list of xdotool modifier names."""
    if not text:
        return []
    parts = []
    for tok in text.lower().split("+"):
        tok = tok.strip()
        if tok in ("ctrl", "control"):
            parts.append("ctrl")
        elif tok == "shift":
            parts.append("shift")
        elif tok == "alt":
            parts.append("alt")
        elif tok in ("super", "meta", "cmd", "command", "win"):
            parts.append("super")
    return parts


def _translate_anthropic_action(tool_input: dict, display: str, scale: float) -> tuple[str, str]:
    """Execute one Anthropic CUA action. Returns (label, exec_result)."""
    action = tool_input.get("action", "")
    coord = tool_input.get("coordinate")  # [x, y] in scaled image space

    def unscale(c):
        if not c:
            return None
        return [int(c[0] / scale), int(c[1] / scale)]

    if action == "screenshot":
        return ("screenshot", "ok")  # Caller re-captures.

    if action == "left_click":
        c = unscale(coord) or [0, 0]
        mods = _xdotool_modifier(tool_input.get("text"))
        if mods:
            # combo: hold modifiers, click
            from .actions import _smooth_mousemove, _xdotool
            _smooth_mousemove(c[0], c[1], display)
            cmd = ["click", "--clearmodifiers", "1"] if not mods else None
            # xdotool keydown mods, click, keyup mods
            for m in mods:
                _xdotool(["keydown", m], display)
            r = _xdotool(["click", "1"], display)
            for m in reversed(mods):
                _xdotool(["keyup", m], display)
            return (f"left_click({c[0]},{c[1]},mods={'+'.join(mods)})", r)
        return (f"left_click({c[0]},{c[1]})", execute_action_logged("click", {"x": c[0], "y": c[1], "button": "left"}, display))

    if action == "right_click":
        c = unscale(coord) or [0, 0]
        return (f"right_click({c[0]},{c[1]})", execute_action_logged("click", {"x": c[0], "y": c[1], "button": "right"}, display))

    if action == "middle_click":
        c = unscale(coord) or [0, 0]
        return (f"middle_click({c[0]},{c[1]})", execute_action_logged("click", {"x": c[0], "y": c[1], "button": "middle"}, display))

    if action == "double_click":
        c = unscale(coord) or [0, 0]
        return (f"double_click({c[0]},{c[1]})", execute_action_logged("double_click", {"x": c[0], "y": c[1]}, display))

    if action == "triple_click":
        c = unscale(coord) or [0, 0]
        # Implement as click + click + click with brief delays
        execute_action_logged("click", {"x": c[0], "y": c[1], "button": "left", "clicks": 3}, display)
        return (f"triple_click({c[0]},{c[1]})", "ok")

    if action == "mouse_move":
        c = unscale(coord) or [0, 0]
        return (f"mouse_move({c[0]},{c[1]})", execute_action_logged("move_mouse", {"x": c[0], "y": c[1]}, display))

    if action == "left_mouse_down":
        c = unscale(coord) or [0, 0]
        return (f"mouse_down({c[0]},{c[1]})", execute_action_logged("mouse_down", {"x": c[0], "y": c[1], "button": "left"}, display))

    if action == "left_mouse_up":
        c = unscale(coord) or [0, 0]
        return (f"mouse_up({c[0]},{c[1]})", execute_action_logged("mouse_up", {"button": "left"}, display))

    if action == "type":
        text = str(tool_input.get("text", ""))
        return (f"type({text[:40]!r})", execute_action_logged("type_text", {"text": text}, display))

    if action == "key":
        # Anthropic key strings are like "ctrl+s", "Return", "Page_Down"
        key = str(tool_input.get("text", ""))
        return (f"key({key})", execute_action_logged("key_press", {"key": key}, display))

    if action == "hold_key":
        from .actions import _xdotool
        key = str(tool_input.get("text", ""))
        duration = float(tool_input.get("duration", 1.0))
        # xdotool keydown + sleep + keyup
        _xdotool(["keydown", key], display)
        _time.sleep(min(duration, 5.0))
        _xdotool(["keyup", key], display)
        return (f"hold_key({key},{duration}s)", "ok")

    if action == "scroll":
        c = unscale(coord) or [0, 0]
        direction = tool_input.get("scroll_direction", "down")
        amount = max(1, min(20, int(tool_input.get("scroll_amount", 3))))
        return (
            f"scroll({direction},{amount}@{c[0]},{c[1]})",
            execute_action_logged("scroll", {"x": c[0], "y": c[1], "direction": direction, "amount": amount}, display),
        )

    if action == "left_click_drag":
        start = unscale(tool_input.get("start_coordinate")) or [0, 0]
        end = unscale(tool_input.get("coordinate")) or [0, 0]
        return (
            f"drag(({start[0]},{start[1]})→({end[0]},{end[1]}))",
            execute_action_logged("drag", {"start_x": start[0], "start_y": start[1], "end_x": end[0], "end_y": end[1]}, display),
        )

    if action == "wait":
        duration = float(tool_input.get("duration", 1.0))
        _time.sleep(min(duration, 5.0))
        return (f"wait({duration}s)", "ok")

    if action == "cursor_position":
        from .actions import _current_mouse_pos
        pos = _current_mouse_pos(display) or (0, 0)
        return (f"cursor_position", f"{{'x': {pos[0]}, 'y': {pos[1]}}}")

    return (f"unknown({action})", f"error: unknown action {action!r}")


class AnthropicCuaProvider(LiveUIProvider):
    """Anthropic native Computer Use via Messages API + computer_20251124."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY", "")
        self._model = model or _DEFAULT_MODEL
        self._base = _API_BASE.rstrip("/")
        self.provider = "anthropic-cua"

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
            err = "ANTHROPIC_API_KEY not set — anthropic_cua backend cannot run"
            return {"error": err, "status": "error", "summary": err, "success": False,
                    "actions_taken": 0, "actions_log": []}

        cancel_key = session.id if session else f"no-session-{_uuid.uuid4().hex[:8]}"
        cancel_event = register_cancel_event(cancel_key, task_id)
        sid = session.id[:8] if session else "--------"
        disp_w, disp_h = await asyncio.get_running_loop().run_in_executor(None, _get_display_size, display)
        scale = _scale_factor(disp_w, disp_h, self._model)
        scaled_w, scaled_h = int(disp_w * scale), int(disp_h * scale)
        _dbg.log("LIVE", f"[{sid}] anthropic_cua model={self._model} {disp_w}x{disp_h} (scaled {scaled_w}x{scaled_h}) scale={scale:.3f} task={task_id} timeout={timeout}s")

        t_start = _time.time()
        actions_taken = 0
        actions_log: list[str] = []
        usage_total = {"input_tokens": 0, "output_tokens": 0,
                       "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}

        # Initial screenshot, scaled.
        screenshot_b64, _ = await asyncio.get_running_loop().run_in_executor(
            None, _capture_jpeg_b64, display, session
        )
        if not screenshot_b64:
            err = f"Failed to capture screenshot from display {display}"
            return {"error": err, "status": "error", "summary": err, "success": False,
                    "actions_taken": 0, "actions_log": []}
        scaled_b64, _, _ = _resize_jpeg_b64(screenshot_b64, scale)

        prompt_text = (
            f"Instruction: {instruction}\n\n"
            f"The desktop is {disp_w}×{disp_h} pixels. "
            f"You see the current state in the attached screenshot. "
            f"Use the `computer` tool to interact. Report a final assistant "
            f"message when the task is complete (do NOT keep emitting "
            f"computer actions after you're done)."
            + (f"\n\nContext:\n{context}" if context else "")
        )

        messages = [{
            "role": "user",
            "content": [
                {"type": "text", "text": prompt_text},
                {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": scaled_b64}},
            ],
        }]

        tool_def = {
            "type": "computer_20251124",
            "name": "computer",
            "display_width_px": scaled_w,
            "display_height_px": scaled_h,
        }

        try:
          async with httpx.AsyncClient(timeout=httpx.Timeout(120.0)) as client:
            try:
                async with asyncio.timeout(timeout):
                    while actions_taken < int(os.environ.get('ACU_GUI_AGENT_PER_CALL_MAX_TURNS', '5')):
                        if cancel_event.is_set():
                            return _result_cancelled(t_start, sid, session, actions_taken, actions_log, usage_total)

                        # Retry on transient 429/5xx with exponential backoff.
                        resp = None
                        for retry in range(5):
                            try:
                                resp = await client.post(
                                    f"{self._base}/messages",
                                    headers={
                                        "x-api-key": self._api_key,
                                        "anthropic-version": "2023-06-01",
                                        "anthropic-beta": "computer-use-2025-11-24",
                                        "content-type": "application/json",
                                    },
                                    json={
                                        "model": self._model,
                                        "max_tokens": 4096,
                                        "tools": [tool_def],
                                        "messages": messages,
                                    },
                                )
                            except httpx.HTTPError as e:
                                err = f"API transport: {type(e).__name__}: {e}"[:300]
                                _dbg.log("LIVE", f"[{sid}] {err}")
                                return _result_error(err, t_start, sid, session, actions_taken, actions_log, usage_total)
                            if resp.status_code in (429, 500, 502, 503, 504, 529) and retry < 4:
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
                            _dbg.log("LIVE", f"[{sid}] {err}")
                            return _result_error(err, t_start, sid, session, actions_taken, actions_log, usage_total)
                        data = resp.json()
                        # Accumulate usage.
                        u = data.get("usage") or {}
                        usage_total["input_tokens"] += int(u.get("input_tokens", 0))
                        usage_total["output_tokens"] += int(u.get("output_tokens", 0))
                        usage_total["cache_creation_input_tokens"] += int(u.get("cache_creation_input_tokens", 0))
                        usage_total["cache_read_input_tokens"] += int(u.get("cache_read_input_tokens", 0))

                        # Append assistant message to history.
                        content = data.get("content") or []
                        messages.append({"role": "assistant", "content": content})

                        stop_reason = data.get("stop_reason", "")
                        text_blocks = [b for b in content if b.get("type") == "text"]
                        tool_uses = [b for b in content if b.get("type") == "tool_use"]

                        for tb in text_blocks:
                            if tb.get("text") and session:
                                session.record_model_text(tb["text"])
                            if tb.get("text"):
                                _dbg.log("LIVE", f"[{sid}] model_text: {tb['text'][:160]}")

                        if stop_reason == "end_turn" or not tool_uses:
                            # Model stopped emitting tool calls — task complete.
                            final_text = "\n".join(b.get("text", "") for b in text_blocks)
                            _dbg.log("LIVE", f"[{sid}] done: actions={actions_taken} stop={stop_reason}")
                            if session:
                                session.record_done(True, final_text or "task complete")
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

                        # Process tool_uses, build tool_result content for next message.
                        tool_results = []
                        for tu in tool_uses:
                            if cancel_event.is_set():
                                return _result_cancelled(t_start, sid, session, actions_taken, actions_log, usage_total)
                            tu_id = tu.get("id")
                            tu_input = tu.get("input") or {}

                            label, exec_result = _translate_anthropic_action(tu_input, display, scale)
                            actions_log.append(label)
                            actions_taken += 1
                            _dbg.log("LIVE", f"[{sid}] cua#{actions_taken} {label} → {str(exec_result)[:80]}")
                            if session:
                                session.record_tool_call(tu_input.get("action", "?"), tu_input, tu_id or "")
                            await asyncio.sleep(0.4)

                            # Always send back a fresh screenshot as image content.
                            new_b64, _ = await asyncio.get_running_loop().run_in_executor(
                                None, _capture_jpeg_b64, display, session
                            )
                            new_scaled, _, _ = _resize_jpeg_b64(new_b64 or scaled_b64, scale)
                            tool_results.append({
                                "type": "tool_result",
                                "tool_use_id": tu_id,
                                "content": [
                                    {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": new_scaled}},
                                ],
                            })

                        messages.append({"role": "user", "content": tool_results})

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
        "summary": f"anthropic_cua API error: {err[:200]}",
        "actions_taken": actions_taken, "actions_log": actions_log[-10:],
        "session_id": session.id if session else "",
        "elapsed_s": round(_time.time() - t_start, 1),
        "usage": usage or {},
    }


def anthropic_cua_provider() -> AnthropicCuaProvider:
    return AnthropicCuaProvider()
