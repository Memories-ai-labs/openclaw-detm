"""Family A runner: LLM (via OpenRouter) + Playwright MCP server.

Pipeline:
  1. Spawn `npx @playwright/mcp` over stdio with a persistent Chromium
     user-data-dir (so cookies survive across runs and the user can log
     into LinkedIn once).
  2. Discover its tools via MCP `list_tools`. Convert each to an OpenAI
     tool-calling schema.
  3. Loop:
       - Send the message history + tool list to OpenRouter
         /chat/completions.
       - If the assistant returns tool_calls: dispatch each via MCP
         `tools/call`, append results, continue.
       - If the assistant returns plain content with no tool_calls:
         that's the final answer. Take one final screenshot and stop.
  4. Hard caps: max_actions tool calls, max_duration_s wall clock.

Usage notes:
  - Set BENCH_CHROMIUM_PROFILE to point at a dir; default
    ~/.bench-chromium-profile. User must log in to LinkedIn manually
    in that profile once before running tier-1 tasks.
  - We require OPENROUTER_API_KEY in the environment.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Optional

import httpx

from .base import RunResult, RunnerBase, TaskSpec, extract_json_block, save_run_artifacts


OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
PROFILE_DIR = Path(os.environ.get(
    "BENCH_CHROMIUM_PROFILE",
    os.path.expanduser("~/.bench-chromium-profile"),
))
DISPLAY = os.environ.get("BENCH_DISPLAY", ":99")
# Headed by default so the user can watch and so screenshots reflect a
# realistic viewport. Set BENCH_PLAYWRIGHT_HEADLESS=1 for tier-3 tasks
# where you don't need a visible window (faster, no display contention
# with DETM's browser on :99).
HEADLESS = os.environ.get("BENCH_PLAYWRIGHT_HEADLESS", "0") == "1"


# ── MCP client glue (async) ──────────────────────────────────────────────

def _mcp_tool_to_openai(mcp_tool) -> dict:
    """Convert an MCP tool description to OpenAI-compatible tool format."""
    return {
        "type": "function",
        "function": {
            "name": mcp_tool.name,
            "description": (mcp_tool.description or "")[:1024],
            "parameters": mcp_tool.inputSchema or {"type": "object", "properties": {}},
        },
    }


def _summarize_tool_result(result) -> str:
    """Compact text summary of an MCP tool result for the LLM message log."""
    parts = []
    for c in (result.content or []):
        if getattr(c, "type", None) == "text":
            parts.append(c.text)
        elif getattr(c, "type", None) == "image":
            parts.append("[image]")
        else:
            parts.append(str(c))
    txt = "\n".join(parts)
    if len(txt) > 4000:
        txt = txt[:4000] + f"\n…[truncated, full length {len(txt)} chars]"
    return txt or "(empty result)"


# ── Runner ───────────────────────────────────────────────────────────────

# Once the conversation grows beyond this many entries (system + user +
# assistant turns + tool results), we replace the OLDEST tool results with
# "[truncated]" placeholders to keep the prompt within OpenRouter's per-
# request token budget. We never drop assistant turns (model needs its
# own history to stay coherent).
_MESSAGE_HISTORY_HARD_CAP = 80


class PlaywrightMCPRunner(RunnerBase):
    family = "playwright_mcp"
    # One-shot guard so we sync cookies once per orchestrator process,
    # not once per task.
    _cookies_synced = False

    def __init__(self, model: str):
        super().__init__(model=model)
        self.api_key = os.environ.get("OPENROUTER_API_KEY")
        if not self.api_key:
            raise RuntimeError("OPENROUTER_API_KEY env var is required")
        self._maybe_sync_cookies()

    def _maybe_sync_cookies(self) -> None:
        """Copy cookies from the user's main Chrome profile (the one DETM
        uses on :99) into the bench Chromium profile so both families
        start from the same logged-in state. One-shot per process."""
        if PlaywrightMCPRunner._cookies_synced:
            return
        if os.environ.get("BENCH_SKIP_COOKIE_SYNC") == "1":
            print("  (skipping cookie sync — BENCH_SKIP_COOKIE_SYNC=1)")
            PlaywrightMCPRunner._cookies_synced = True
            return
        try:
            from . import cookie_sync
            print("  syncing Chrome cookies → bench Chromium profile...")
            cookie_sync.sync(verbose=True)
        except RuntimeError as e:
            # Bench Chromium running, or source profile missing — warn but
            # don't fail; LinkedIn tasks may still hit login wall.
            print(f"  ⚠ cookie sync failed: {e}")
        except Exception as e:
            print(f"  ⚠ cookie sync raised: {e}")
        PlaywrightMCPRunner._cookies_synced = True

    def _check_linkedin_login(self) -> str | None:
        """Return None if the bench Chromium profile has a LinkedIn
        session cookie (li_at), else a human-readable error message.
        Used as a preflight for tier-1 tasks that declare
        needs_logged_in_linkedin: true."""
        cookies_db = PROFILE_DIR / "Default" / "Cookies"
        if not cookies_db.exists():
            return (
                f"Bench Chromium profile {PROFILE_DIR} has no Cookies file — "
                f"never been logged in. Run cookie sync (auto-runs at "
                f"orchestrator start) or log in manually."
            )
        import sqlite3 as _sql
        try:
            tmp = Path(f"/tmp/_bench_cookies_check_{os.getpid()}.db")
            shutil.copy2(cookies_db, tmp)
            con = _sql.connect(str(tmp))
            n = con.execute(
                "SELECT COUNT(*) FROM cookies "
                "WHERE host_key LIKE '%linkedin%' AND name='li_at'"
            ).fetchone()[0]
            con.close()
            tmp.unlink()
            if n == 0:
                return (
                    f"No LinkedIn 'li_at' session cookie in bench profile. "
                    f"Cookie sync may have run but LinkedIn-side cookie wasn't "
                    f"picked up. Log in to LinkedIn manually in the source "
                    f"Chrome (the one on :99) and re-run."
                )
        except Exception as e:
            return f"Couldn't read bench Cookies DB: {e}"
        return None

    def run(self, task: TaskSpec, run_dir: Path) -> RunResult:
        # Tier-1 tasks declare needs_logged_in_linkedin: true. Preflight
        # the cookie state so we fail fast with a clear error instead of
        # having the agent hit a login wall and waste tokens.
        if task.family_constraints.get("needs_logged_in_linkedin"):
            err = self._check_linkedin_login()
            if err:
                r = RunResult(
                    task_id=task.id, family=self.family, model=self.model,
                    run_id=run_dir.parent.parent.name,
                    started_at=self.now_iso(), ended_at=self.now_iso(),
                    duration_s=0.0, n_tool_calls=0, n_assistant_messages=0,
                    n_screenshots=0, thinking_chars=0,
                    prompt_tokens=0, completion_tokens=0,
                    final_answer="", final_answer_parsed=None,
                    termination_reason="error",
                    error_message=f"preflight: {err}",
                )
                save_run_artifacts(run_dir, r)
                return r
        return asyncio.run(self._run_async(task, run_dir))

    async def _run_async(self, task: TaskSpec, run_dir: Path) -> RunResult:
        from contextlib import AsyncExitStack
        from mcp.client.stdio import stdio_client
        from mcp import ClientSession, StdioServerParameters

        actions_log: list[dict] = []
        messages_log: list[dict] = []
        n_tool_calls = 0
        n_assistant_messages = 0
        thinking_chars = 0
        prompt_tokens_total = 0
        completion_tokens_total = 0

        # Optional screen recording (BENCH_RECORD_VIDEO=1). Only useful
        # when Playwright is headed (BENCH_PLAYWRIGHT_HEADLESS=0) — when
        # headless, Chromium renders off-screen and the recording is blank.
        from .recorder import DisplayRecorder
        recorder = DisplayRecorder(out_path=run_dir / "recording.mp4") if not HEADLESS else None
        if recorder:
            recorder.start()

        started_at = self.now_iso()
        t0 = time.time()
        deadline = t0 + task.max_duration_s

        # 1) Spawn Playwright MCP --------------------------------------
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        playwright_args = [
            "-y", "@playwright/mcp@latest",
            "--browser", "chromium",
            "--user-data-dir", str(PROFILE_DIR),
            "--viewport-size", "1920,1080",
        ]
        if HEADLESS:
            playwright_args.append("--headless")
        server = StdioServerParameters(
            command="npx",
            args=playwright_args,
            env={**os.environ, "DISPLAY": DISPLAY},
        )

        async with AsyncExitStack() as stack:
            try:
                read, write = await stack.enter_async_context(stdio_client(server))
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
            except Exception as e:
                ended_at = self.now_iso()
                result = RunResult(
                    task_id=task.id, family=self.family, model=self.model,
                    run_id=run_dir.parent.parent.name,
                    started_at=started_at, ended_at=ended_at,
                    duration_s=time.time() - t0,
                    n_tool_calls=0, n_assistant_messages=0, n_screenshots=0,
                    thinking_chars=0, prompt_tokens=0, completion_tokens=0,
                    final_answer="", final_answer_parsed=None,
                    termination_reason="error",
                    error_message=f"Playwright MCP spawn failed: {e}",
                )
                save_run_artifacts(run_dir, result)
                return result

            # 2) Discover tools ----------------------------------------
            tools_resp = await session.list_tools()
            tools = list(tools_resp.tools)
            openai_tools = [_mcp_tool_to_openai(t) for t in tools]

            # Pre-flight: close all open tabs to reset visual state.
            try:
                # Many playwright-mcp builds expose `browser_close` or
                # `browser_tabs` / similar. We try a few common names.
                for reset_tool in ("browser_close_all_tabs", "browser_close",
                                    "browser_navigate"):
                    if any(t.name == reset_tool for t in tools):
                        if reset_tool == "browser_navigate":
                            await session.call_tool(reset_tool, {"url": "about:blank"})
                        else:
                            await session.call_tool(reset_tool, {})
                        break
            except Exception:
                pass

            # 3) Build initial messages --------------------------------
            messages: list[dict] = [
                {
                    "role": "system",
                    "content": (
                        "You are a GUI agent driving a Chromium browser via "
                        "Playwright tools. Complete the task by navigating, "
                        "clicking, typing, and reading page content. When you "
                        "have the answer, return it as plain text in the "
                        "exact JSON shape the task asks for, using a ```json "
                        "fenced block. Do not call further tools after you "
                        "have the answer."
                    ),
                },
                {"role": "user", "content": task.prompt},
            ]
            messages_log.append({"role": "system", "content": messages[0]["content"]})
            messages_log.append({"role": "user", "content": task.prompt})

            # 4) Tool loop ---------------------------------------------
            final_text = ""
            termination = "completed"
            # `async with` ensures the connection pool is released even if
            # an unhandled exception bubbles out of the loop body.
            async with httpx.AsyncClient(timeout=120.0) as client:
                while True:
                    if time.time() > deadline:
                        termination = "timeout"
                        break
                    if n_tool_calls >= task.max_actions:
                        termination = "max_actions"
                        break

                    # If the conversation has grown past the cap, replace the
                    # OLDEST tool result contents with "[truncated]" — keep
                    # the structure (role/tool_call_id) so the model's tool-
                    # use trace stays self-consistent.
                    if len(messages) > _MESSAGE_HISTORY_HARD_CAP:
                        for m in messages[1:-_MESSAGE_HISTORY_HARD_CAP // 2]:
                            if m.get("role") == "tool" and m.get("content") != "[truncated]":
                                m["content"] = "[truncated]"

                    # Call OpenRouter
                    try:
                        or_resp = await client.post(
                            OPENROUTER_URL,
                            headers={
                                "Authorization": f"Bearer {self.api_key}",
                                "Content-Type": "application/json",
                                "HTTP-Referer": "https://detm.local/bench",
                                "X-Title": "DETM-bench-gui-comparison",
                            },
                            json={
                                "model": self.model,
                                "messages": messages,
                                "tools": openai_tools,
                                "tool_choice": "auto",
                                "max_tokens": 4096,
                            },
                        )
                    except Exception as e:
                        termination = "error"
                        final_text = f"OpenRouter call failed: {e}"
                        break
                    if or_resp.status_code != 200:
                        termination = "error"
                        final_text = (
                            f"OpenRouter HTTP {or_resp.status_code}: {or_resp.text[:500]}"
                        )
                        break
                    data = or_resp.json()
                    usage = data.get("usage", {}) or {}
                    prompt_tokens_total += int(usage.get("prompt_tokens", 0))
                    completion_tokens_total += int(usage.get("completion_tokens", 0))
                    choices = data.get("choices") or []
                    if not choices:
                        termination = "error"
                        final_text = f"OpenRouter returned no choices: {data}"
                        break
                    msg = choices[0].get("message", {}) or {}
                    # If the provider returned tool_calls without ids,
                    # synthesize them HERE — before the assistant turn is
                    # logged or the tool results are dispatched. This keeps
                    # the assistant.tool_calls[i].id and the matching
                    # tool.tool_call_id in lockstep (otherwise they drift
                    # and the next API call 400s).
                    if msg.get("tool_calls"):
                        for i, tc in enumerate(msg["tool_calls"]):
                            if not tc.get("id"):
                                tc["id"] = f"call_{n_assistant_messages + 1}_{i}"
                    n_assistant_messages += 1
                    # Track reasoning if present (some models on OpenRouter
                    # surface reasoning_content / thinking).
                    reasoning = msg.get("reasoning") or msg.get("reasoning_content") or ""
                    if isinstance(reasoning, list):
                        reasoning = "\n".join(
                            r.get("text", "") if isinstance(r, dict) else str(r)
                            for r in reasoning
                        )
                    if reasoning:
                        thinking_chars += len(reasoning)

                    # Append assistant turn (preserving tool_calls for context).
                    assistant_entry: dict[str, Any] = {
                        "role": "assistant",
                        "content": msg.get("content") or "",
                    }
                    if msg.get("tool_calls"):
                        assistant_entry["tool_calls"] = msg["tool_calls"]
                    messages.append(assistant_entry)
                    messages_log.append({
                        "role": "assistant",
                        "content": msg.get("content") or "",
                        "tool_calls": msg.get("tool_calls"),
                        "reasoning_chars": len(reasoning) if reasoning else 0,
                    })

                    tool_calls = msg.get("tool_calls") or []
                    if not tool_calls:
                        final_text = msg.get("content") or ""
                        termination = "completed"
                        break

                    # 4a) Dispatch tool calls
                    for tc in tool_calls:
                        if time.time() > deadline:
                            break
                        n_tool_calls += 1
                        fn = tc.get("function") or {}
                        name = fn.get("name", "")
                        args_raw = fn.get("arguments") or "{}"
                        try:
                            args = json.loads(args_raw) if isinstance(args_raw, str) else (args_raw or {})
                        except json.JSONDecodeError:
                            args = {}
                        call_t0 = time.time()
                        try:
                            result = await session.call_tool(name, args)
                            result_text = _summarize_tool_result(result)
                            ok = not getattr(result, "isError", False)
                        except Exception as e:
                            result_text = f"tool error: {e}"
                            ok = False
                        call_dt = time.time() - call_t0

                        actions_log.append({
                            "n": n_tool_calls,
                            "tool": name,
                            "args": args,
                            "ok": ok,
                            "result_chars": len(result_text),
                            "latency_s": round(call_dt, 3),
                        })

                        # IDs were synthesized above (if missing) on the
                        # assistant message side, so they're always present
                        # here AND match what we said in the assistant turn.
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "name": name,
                            "content": result_text,
                        })
                        messages_log.append({
                            "role": "tool",
                            "name": name,
                            "content_chars": len(result_text),
                        })

                        # Cap per-task tool calls.
                        if n_tool_calls >= task.max_actions:
                            break

            # 5) Take final screenshot via MCP if available
            screenshot_path = run_dir / "screenshots" / "final.png"
            try:
                ss_tool = next(
                    (t.name for t in tools if t.name in (
                        "browser_take_screenshot", "screenshot",
                    )),
                    None,
                )
                if ss_tool:
                    res = await session.call_tool(ss_tool, {})
                    for c in (res.content or []):
                        if getattr(c, "type", None) == "image":
                            data = getattr(c, "data", None)
                            if isinstance(data, str):
                                screenshot_path.write_bytes(base64.b64decode(data))
                                break
            except Exception:
                pass
            # Fallback: scrot the X display. (May capture wrong window if
            # other apps are present, but better than nothing for the judge.)
            if not screenshot_path.exists():
                try:
                    if shutil.which("scrot"):
                        subprocess.run(
                            ["scrot", "-z", str(screenshot_path)],
                            env={**os.environ, "DISPLAY": DISPLAY},
                            stderr=subprocess.DEVNULL, timeout=10,
                        )
                except Exception:
                    pass

        if recorder:
            recorder.stop()

        ended_at = self.now_iso()
        duration = time.time() - t0

        result = RunResult(
            task_id=task.id, family=self.family, model=self.model,
            run_id=run_dir.parent.parent.name,
            started_at=started_at, ended_at=ended_at, duration_s=duration,
            n_tool_calls=n_tool_calls,
            n_assistant_messages=n_assistant_messages,
            n_screenshots=1 if screenshot_path.exists() else 0,
            thinking_chars=thinking_chars,
            prompt_tokens=prompt_tokens_total,
            completion_tokens=completion_tokens_total,
            final_answer=final_text,
            final_answer_parsed=extract_json_block(final_text),
            termination_reason=termination,
            error_message=None,
        )
        save_run_artifacts(run_dir, result, actions=actions_log, messages=messages_log)
        return result
