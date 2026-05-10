"""Gemini native Computer Use backend (AI Studio, NOT Vertex).

Uses google-genai SDK with `Tool(computer_use=ComputerUse(environment=BROWSER))`
against `generativelanguage.googleapis.com` (the AI Studio endpoint, no
GCP project / Vertex required). Default model is
`gemini-2.5-computer-use-preview-10-2025`. The model emits
`function_call` blocks matching the 13 predefined Computer Use actions
(click_at, scroll_at, type_text_at, key_combination, navigate, etc.).

This is the configuration Google uses to claim leadership on the
Online-Mind2Web / WebArena / Online-OSWorld browser benchmarks.

Coordinate space: Gemini Computer Use returns coords in **0–999
normalized** range — multiply by screen_dim/1000 to get actual pixels.

Model selection: `GEMINI_CUA_MODEL` env var, default
`gemini-2.5-computer-use-preview-10-2025`.
API key: `GEMINI_API_KEY` (from aistudio.google.com/apikey).

Reference: https://ai.google.dev/gemini-api/docs/computer-use
"""
from __future__ import annotations

import asyncio
import os
import time as _time
import uuid as _uuid

from .. import debug as _dbg
from .actions import execute_action_logged
from .base import LiveUIProvider
from .openrouter import (
    _capture_jpeg_b64,
    _get_display_size,
    register_cancel_event,
    unregister_cancel_event,
)


_DEFAULT_MODEL = os.environ.get("GEMINI_CUA_MODEL", "gemini-2.5-computer-use-preview-10-2025")


def _jpeg_b64_to_png_bytes(b64: str) -> bytes:
    """Gemini Computer Use requires PNG; convert from our JPEG capture."""
    import base64, io
    from PIL import Image
    raw = base64.b64decode(b64)
    img = Image.open(io.BytesIO(raw))
    img.load()
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="PNG", optimize=False)
    return buf.getvalue()


# ── Action translation: Gemini computer_use function call → daemon execute_action ──

def _translate_gemini_action(name: str, args: dict, display: str, screen_w: int, screen_h: int) -> tuple[str, str]:
    """Execute one Gemini Computer Use action. Returns (label, exec_result).

    Coordinates from Gemini are normalized 0–999. Convert to actual screen px.
    """
    def to_px(nx, ny):
        x = max(0, min(int((int(nx) / 1000) * screen_w), screen_w - 1))
        y = max(0, min(int((int(ny) / 1000) * screen_h), screen_h - 1))
        return x, y

    if name == "open_web_browser":
        # Best-effort: the daemon doesn't own a browser launcher here. Try to
        # surface the existing Chrome via xdotool or fall back to no-op.
        from .actions import _xdotool
        # Activate or launch chromium-style window
        return ("open_web_browser", "ok (browser already open on display)")

    if name == "wait_5_seconds":
        _time.sleep(5)
        return ("wait_5s", "ok")

    if name == "go_back":
        return ("go_back", execute_action_logged("key_press", {"key": "alt+Left"}, display))

    if name == "go_forward":
        return ("go_forward", execute_action_logged("key_press", {"key": "alt+Right"}, display))

    if name == "search":
        # Search via address bar — Ctrl+L then leave to model to type.
        return ("search", execute_action_logged("key_press", {"key": "ctrl+l"}, display))

    if name == "navigate":
        url = str(args.get("url", ""))
        # Activate address bar, type URL, Enter.
        execute_action_logged("key_press", {"key": "ctrl+l"}, display)
        _time.sleep(0.2)
        execute_action_logged("type_text", {"text": url}, display)
        _time.sleep(0.2)
        execute_action_logged("key_press", {"key": "Return"}, display)
        return (f"navigate({url})", "ok")

    if name == "click_at":
        x, y = to_px(args.get("x", 0), args.get("y", 0))
        return (f"click_at({x},{y})", execute_action_logged("click", {"x": x, "y": y, "button": "left"}, display))

    if name == "hover_at":
        x, y = to_px(args.get("x", 0), args.get("y", 0))
        return (f"hover_at({x},{y})", execute_action_logged("move_mouse", {"x": x, "y": y}, display))

    if name == "type_text_at":
        x, y = to_px(args.get("x", 0), args.get("y", 0))
        text = str(args.get("text", ""))
        clear = bool(args.get("clear_before_typing", False))
        press_enter = bool(args.get("press_enter", False))
        # Click first to focus, optionally clear with Ctrl+A then type
        execute_action_logged("click", {"x": x, "y": y, "button": "left"}, display)
        _time.sleep(0.15)
        if clear:
            execute_action_logged("key_press", {"key": "ctrl+a"}, display)
            _time.sleep(0.05)
            execute_action_logged("key_press", {"key": "Delete"}, display)
            _time.sleep(0.05)
        execute_action_logged("type_text", {"text": text}, display)
        if press_enter:
            _time.sleep(0.1)
            execute_action_logged("key_press", {"key": "Return"}, display)
        return (f"type_text_at({x},{y}, {text[:30]!r})", "ok")

    if name == "key_combination":
        keys = str(args.get("keys", ""))
        # Gemini format like "Control+A"; xdotool wants "ctrl+a"
        canon = "+".join(_canonical_key(k) for k in keys.split("+"))
        return (f"key_combination({canon})", execute_action_logged("key_press", {"key": canon}, display))

    if name == "scroll_document":
        direction = str(args.get("direction", "down")).lower()
        # Scroll the whole page from the center
        x, y = screen_w // 2, screen_h // 2
        return (f"scroll_document({direction})", execute_action_logged("scroll", {"x": x, "y": y, "direction": direction, "amount": 5}, display))

    if name == "scroll_at":
        x, y = to_px(args.get("x", 0), args.get("y", 0))
        direction = str(args.get("direction", "down")).lower()
        magnitude = int(args.get("magnitude", 800))
        # Convert magnitude (0-999, default 800 ≈ a "screen") to xdotool clicks
        amount = max(1, min(20, magnitude // 100))
        return (f"scroll_at({direction},{amount}@{x},{y})", execute_action_logged("scroll", {"x": x, "y": y, "direction": direction, "amount": amount}, display))

    if name == "drag_and_drop":
        sx, sy = to_px(args.get("x", 0), args.get("y", 0))
        ex, ey = to_px(args.get("destination_x", 0), args.get("destination_y", 0))
        return (
            f"drag(({sx},{sy})→({ex},{ey}))",
            execute_action_logged("drag", {"start_x": sx, "start_y": sy, "end_x": ex, "end_y": ey}, display),
        )

    return (f"unknown({name})", f"error: unknown action {name!r}")


def _canonical_key(k: str) -> str:
    """Map Gemini key names to xdotool canonical form."""
    kl = k.strip().lower()
    return {
        "control": "ctrl", "ctrl": "ctrl",
        "shift": "shift", "alt": "alt",
        "meta": "super", "command": "super", "cmd": "super", "super": "super", "win": "super",
        "enter": "Return", "return": "Return",
        "esc": "Escape", "escape": "Escape",
        "tab": "Tab",
        "backspace": "BackSpace",
        "delete": "Delete",
        "space": "space",
        "pageup": "Page_Up", "pagedown": "Page_Down",
        "home": "Home", "end": "End",
        "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    }.get(kl, k)  # Pass through F1..F12, single letters, etc.


class GeminiCuaProvider(LiveUIProvider):
    """Gemini Computer Use via AI Studio (genai SDK)."""

    def __init__(self, api_key: str | None = None, model: str | None = None):
        self._api_key = api_key or os.environ.get("GEMINI_API_KEY", "")
        self._model = model or _DEFAULT_MODEL
        self.provider = "gemini-cua"

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
            err = "GEMINI_API_KEY not set — gemini_cua backend cannot run"
            return {"error": err, "status": "error", "summary": err, "success": False,
                    "actions_taken": 0, "actions_log": []}

        try:
            from google import genai  # type: ignore
            from google.genai import types  # type: ignore
        except ImportError:
            err = "google-genai SDK not installed"
            return {"error": err, "status": "error", "summary": err, "success": False,
                    "actions_taken": 0, "actions_log": []}

        cancel_key = session.id if session else f"no-session-{_uuid.uuid4().hex[:8]}"
        cancel_event = register_cancel_event(cancel_key, task_id)
        sid = session.id[:8] if session else "--------"
        disp_w, disp_h = await asyncio.get_running_loop().run_in_executor(None, _get_display_size, display)
        _dbg.log("LIVE", f"[{sid}] gemini_cua model={self._model} {disp_w}x{disp_h} task={task_id} timeout={timeout}s")

        t_start = _time.time()
        actions_taken = 0
        actions_log: list[str] = []
        usage_total = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0}

        client = genai.Client(api_key=self._api_key)
        cu_config = types.GenerateContentConfig(
            tools=[types.Tool(
                computer_use=types.ComputerUse(
                    environment=types.Environment.ENVIRONMENT_BROWSER,
                ),
            )],
        )

        # Initial screenshot.
        screenshot_b64, _ = await asyncio.get_running_loop().run_in_executor(
            None, _capture_jpeg_b64, display, session
        )
        if not screenshot_b64:
            return {"error": "Failed to capture screenshot", "status": "error",
                    "summary": "screenshot failed", "success": False,
                    "actions_taken": 0, "actions_log": []}
        initial_bytes = _jpeg_b64_to_png_bytes(screenshot_b64)

        prompt = (
            f"Instruction: {instruction}\n\n"
            f"The desktop is {disp_w}×{disp_h} pixels. Use the computer_use "
            f"tool to interact via clicks, typing, scrolling, and navigation. "
            f"When the task is complete, return a final text response — DO NOT "
            f"keep emitting computer actions after you're done."
            + (f"\n\nContext:\n{context}" if context else "")
        )

        contents = [types.Content(
            role="user",
            parts=[
                types.Part.from_text(text=prompt),
                types.Part.from_bytes(data=initial_bytes, mime_type="image/png"),
            ],
        )]

        try:
            try:
                async with asyncio.timeout(timeout):
                    while actions_taken < int(os.environ.get('ACU_GUI_AGENT_PER_CALL_MAX_TURNS', '5')):
                        if cancel_event.is_set():
                            return _result_cancelled(t_start, sid, session, actions_taken, actions_log, usage_total)

                        # Retry on transient genai errors (5xx, 429).
                        response = None
                        last_err = None
                        for retry in range(5):
                            try:
                                response = await asyncio.get_running_loop().run_in_executor(
                                    None,
                                    lambda: client.models.generate_content(
                                        model=self._model,
                                        contents=contents,
                                        config=cu_config,
                                    ),
                                )
                                break
                            except Exception as e:
                                msg = str(e)
                                last_err = f"genai API error: {type(e).__name__}: {msg}"[:300]
                                if retry < 4 and any(s in msg for s in ("503", "502", "504", "429", "RESOURCE_EXHAUSTED", "UNAVAILABLE")):
                                    wait_s = 2 ** retry
                                    _dbg.log("LIVE", f"[{sid}] genai transient (retry {retry+1}/5 after {wait_s}s): {msg[:120]}")
                                    await asyncio.sleep(wait_s)
                                    if cancel_event.is_set():
                                        return _result_cancelled(t_start, sid, session, actions_taken, actions_log, usage_total)
                                    continue
                                break
                        if response is None:
                            _dbg.log("LIVE", f"[{sid}] {last_err}")
                            return _result_error(last_err or "genai unknown failure", t_start, sid, session, actions_taken, actions_log, usage_total)

                        candidates = response.candidates or []
                        # Accumulate usage from response.usage_metadata.
                        um = getattr(response, 'usage_metadata', None)
                        if um is not None:
                            usage_total["input_tokens"] += int(getattr(um, 'prompt_token_count', 0) or 0)
                            usage_total["output_tokens"] += int(getattr(um, 'candidates_token_count', 0) or 0)
                            usage_total["cached_tokens"] += int(getattr(um, 'cached_content_token_count', 0) or 0)
                        if not candidates:
                            err = "genai returned no candidates"
                            return _result_error(err, t_start, sid, session, actions_taken, actions_log, usage_total)

                        cand = candidates[0]
                        cand_content = cand.content
                        contents.append(cand_content)

                        # Extract: text parts and function_call parts.
                        function_calls = []
                        text_parts = []
                        for part in (cand_content.parts or []):
                            if getattr(part, "function_call", None):
                                function_calls.append(part.function_call)
                            elif getattr(part, "text", None):
                                text_parts.append(part.text)

                        for txt in text_parts:
                            if txt.strip() and session:
                                session.record_model_text(txt)
                            if txt.strip():
                                _dbg.log("LIVE", f"[{sid}] model_text: {txt[:160]}")

                        if not function_calls:
                            # Done.
                            final_text = "\n".join(text_parts).strip()
                            _dbg.log("LIVE", f"[{sid}] done: actions={actions_taken}")
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

                        # Process function_calls and build function_responses.
                        function_responses = []
                        for fc in function_calls:
                            if cancel_event.is_set():
                                return _result_cancelled(t_start, sid, session, actions_taken, actions_log, usage_total)
                            fc_name = fc.name
                            fc_args = dict(fc.args) if fc.args else {}

                            # Safety acknowledgement passthrough
                            extra_response_fields = {}
                            sd = fc_args.pop("safety_decision", None)
                            if sd is not None:
                                extra_response_fields["safety_acknowledgement"] = "true"

                            label, exec_result = _translate_gemini_action(fc_name, fc_args, display, disp_w, disp_h)
                            actions_log.append(label)
                            actions_taken += 1
                            _dbg.log("LIVE", f"[{sid}] cua#{actions_taken} {label} → {str(exec_result)[:80]}")
                            if session:
                                session.record_tool_call(fc_name, fc_args, "")
                            await asyncio.sleep(0.4)

                            # Capture fresh screenshot per Gemini docs (must be PNG).
                            new_b64, _ = await asyncio.get_running_loop().run_in_executor(
                                None, _capture_jpeg_b64, display, session
                            )
                            new_bytes = _jpeg_b64_to_png_bytes(new_b64) if new_b64 else initial_bytes
                            response_payload = {"url": "(unknown)"}
                            response_payload.update(extra_response_fields)
                            function_responses.append(types.FunctionResponse(
                                name=fc_name,
                                response=response_payload,
                                parts=[types.FunctionResponsePart(
                                    inline_data=types.FunctionResponseBlob(
                                        mime_type="image/png",
                                        data=new_bytes,
                                    ),
                                )],
                            ))

                        contents.append(types.Content(
                            role="user",
                            parts=[types.Part(function_response=fr) for fr in function_responses],
                        ))

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
        "summary": f"gemini_cua API error: {err[:200]}",
        "actions_taken": actions_taken, "actions_log": actions_log[-10:],
        "session_id": session.id if session else "",
        "elapsed_s": round(_time.time() - t_start, 1),
        "usage": usage or {},
    }


def gemini_cua_provider() -> GeminiCuaProvider:
    return GeminiCuaProvider()
