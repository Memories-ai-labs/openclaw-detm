"""Family B runner: OpenClaw + DETM.

The pipeline:
  1. Verify the DETM daemon is up and configured for the requested
     gui_agent model. (We do NOT auto-restart the daemon — that would
     require sudo. The runner errors out if the model doesn't match,
     and the user is expected to fix the daemon env and rerun.)
  2. Reset browser state on display :99 (close all tabs except one
     about:blank), and snapshot the list of DETM task ids before we
     start so we can detect the new one.
  3. Send the prompt to an existing OpenClaw TUI tmux session via
     `tmux load-buffer + paste-buffer + send-keys Enter`. The prompt
     is wrapped with a sentinel marker so we know when the model
     produced its final answer.
  4. Poll `tmux capture-pane` for the sentinel, with the task's
     max_duration_s as wall-clock cap.
  5. Once the sentinel appears (or we time out), capture a final
     screenshot of display :99, identify the new DETM task by
     comparing `/api/tasks` before/after, and pull its metrics
     (action_details → n_tool_calls).
  6. Write everything into the run dir per base.save_run_artifacts.

The runner does NOT cancel the task on timeout — it returns whatever
the agent produced, marks termination_reason="timeout", and lets the
judge decide whether the partial answer was salvageable.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional
from urllib.request import urlopen
from urllib.error import URLError

from .base import RunResult, RunnerBase, TaskSpec, extract_json_block, save_run_artifacts


DAEMON_URL = os.environ.get("DETM_DAEMON_URL", "http://127.0.0.1:18790")
TMUX_SESSION = os.environ.get("BENCH_TMUX_SESSION", "oc-test")
DISPLAY = os.environ.get("BENCH_DISPLAY", ":99")
DETM_DATA_DIR = Path(os.environ.get(
    "DETM_DATA_DIR", os.path.expanduser("~/.agentic-computer-use")
))


# ── HTTP helpers ─────────────────────────────────────────────────────────

def _get_json(path: str, timeout: float = 10.0) -> dict:
    with urlopen(f"{DAEMON_URL}{path}", timeout=timeout) as resp:
        return json.loads(resp.read())


def _daemon_health() -> dict:
    return _get_json("/health")


def _list_task_ids() -> set[str]:
    try:
        return {t["task_id"] for t in _get_json("/api/tasks")["tasks"]}
    except Exception:
        return set()


def _get_task(task_id: str) -> Optional[dict]:
    try:
        return _get_json(f"/api/tasks/{task_id}")
    except Exception:
        return None


# ── Display + browser helpers ────────────────────────────────────────────

def _screenshot(out_path: Path) -> bool:
    """Take a screenshot of DISPLAY into out_path (PNG). True on success."""
    if shutil.which("scrot"):
        rc = subprocess.run(
            ["scrot", "-z", str(out_path)],
            env={**os.environ, "DISPLAY": DISPLAY},
            stderr=subprocess.DEVNULL,
        ).returncode
        return rc == 0 and out_path.exists()
    if shutil.which("import"):
        rc = subprocess.run(
            ["import", "-window", "root", "-display", DISPLAY, str(out_path)],
            stderr=subprocess.DEVNULL,
        ).returncode
        return rc == 0 and out_path.exists()
    return False


# ── tmux helpers ─────────────────────────────────────────────────────────

def _tmux(*args: str, capture: bool = False, timeout: float = 10.0) -> str:
    cmd = ["tmux", *args]
    if capture:
        return subprocess.check_output(cmd, text=True, timeout=timeout)
    subprocess.check_call(cmd, timeout=timeout)
    return ""


def _tmux_session_exists(name: str) -> bool:
    try:
        _tmux("has-session", "-t", name)
        return True
    except subprocess.CalledProcessError:
        return False


def _tmux_paste_prompt(session: str, prompt: str) -> None:
    """Load prompt into a tmux buffer, paste into session, hit Enter."""
    tmp = Path(f"/tmp/bench_prompt_{os.getpid()}.txt")
    tmp.write_text(prompt)
    try:
        _tmux("load-buffer", "-b", "bench", str(tmp))
        _tmux("paste-buffer", "-b", "bench", "-t", session)
        time.sleep(0.5)
        _tmux("send-keys", "-t", session, "Enter")
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass


def _tmux_capture(session: str, lines: int = 4000) -> str:
    return _tmux(
        "capture-pane", "-t", session, "-p", "-S", f"-{lines}", capture=True,
        timeout=30.0,
    )


# ── DETM artifact extraction ─────────────────────────────────────────────

def _copy_detm_artifacts(detm_task_id: str, dst: Path) -> None:
    """Copy DETM's per-task journal/events/recordings into dst if they exist."""
    src = DETM_DATA_DIR / "tasks" / detm_task_id
    if not src.exists():
        return
    for name in ("journal.jsonl", "events.jsonl", "task.json"):
        f = src / name
        if f.exists():
            shutil.copy2(f, dst / f"detm_{name}")
    sessions_dir = src / "sessions"
    if sessions_dir.exists():
        for sess in sessions_dir.iterdir():
            if sess.is_dir():
                events_jsonl = sess / "events.jsonl"
                if events_jsonl.exists():
                    shutil.copy2(events_jsonl, dst / f"detm_session_{sess.name}_events.jsonl")


# ── Runner ───────────────────────────────────────────────────────────────

_SENTINEL_PREFIX = "BENCH_END_"


class DETMRunner(RunnerBase):
    """Runs a task by sending it to an OpenClaw TUI in a tmux session,
    where OpenClaw will route GUI work via the DETM MCP server."""

    family = "detm"

    def __init__(self, model: str, tmux_session: str = TMUX_SESSION):
        super().__init__(model=model)
        self.tmux_session = tmux_session

    def _verify_daemon(self) -> dict:
        try:
            h = _daemon_health()
        except URLError as e:
            raise RuntimeError(
                f"DETM daemon not reachable at {DAEMON_URL}: {e}"
            ) from e
        # The daemon's gui_agent model should match what we requested.
        # We compare on `live_ui_model` since that's what `/health` exposes
        # for the bash backend.
        live_model = h.get("live_ui_model", "")
        if live_model and live_model != self.model:
            raise RuntimeError(
                f"DETM daemon is configured with live_ui_model={live_model!r}, "
                f"but this runner requested model={self.model!r}. "
                f"Restart the daemon with ACU_OPENROUTER_GUI_DIRECT_MODEL={self.model} "
                f"and re-run."
            )
        return h

    def _verify_tmux(self) -> None:
        if not _tmux_session_exists(self.tmux_session):
            raise RuntimeError(
                f"tmux session {self.tmux_session!r} not found. Start an OpenClaw "
                f"TUI in that session before running benchmarks."
            )

    def _reset_browser(self) -> None:
        """Best-effort: close all browser tabs, leave one about:blank.

        We use xdotool to send Ctrl+Shift+W (close window) until only one
        remains, then Ctrl+L → about:blank → Enter. For Chrome this is
        equivalent to a hard reset of visual state without losing cookies.

        Falls back to no-op if xdotool not present.
        """
        if not shutil.which("xdotool"):
            return
        env = {**os.environ, "DISPLAY": DISPLAY}
        # Find a browser window
        try:
            wid = subprocess.check_output(
                ["xdotool", "search", "--limit", "1", "--name", "Google Chrome"],
                env=env, text=True, stderr=subprocess.DEVNULL,
            ).strip()
        except subprocess.CalledProcessError:
            try:
                wid = subprocess.check_output(
                    ["xdotool", "search", "--limit", "1", "--name", "Mozilla Firefox"],
                    env=env, text=True, stderr=subprocess.DEVNULL,
                ).strip()
            except subprocess.CalledProcessError:
                return
        if not wid:
            return
        # Activate it, then Ctrl+L → about:blank → Enter
        # (We don't aggressively close tabs because that risks closing the whole
        # window and losing the logged-in profile state. A blanked-out tab is
        # enough of a reset for visual purposes — the agent will navigate from
        # whatever URL is active.)
        try:
            subprocess.run(
                ["xdotool", "windowactivate", "--sync", wid],
                env=env, timeout=5, check=False,
            )
            time.sleep(0.3)
            subprocess.run(
                ["xdotool", "key", "ctrl+l"],
                env=env, timeout=5, check=False,
            )
            time.sleep(0.3)
            subprocess.run(
                ["xdotool", "type", "--delay", "10", "about:blank"],
                env=env, timeout=10, check=False,
            )
            subprocess.run(
                ["xdotool", "key", "Return"],
                env=env, timeout=5, check=False,
            )
            time.sleep(0.5)
        except Exception:
            pass

    def _build_prompt(self, task: TaskSpec, sentinel: str) -> str:
        return (
            f"DETM ONLY\n\n"
            f"{task.prompt}\n\n"
            f"---\n"
            f"When you finish the task (success OR failure), produce your "
            f"final JSON answer in a ```json fenced code block, then on its "
            f"own line type exactly:\n"
            f"{sentinel}\n\n"
            f"Do not type that sentinel before you are completely done."
        )

    def _wait_for_sentinel(
        self, sentinel: str, deadline_s: float
    ) -> tuple[str, bool]:
        """Poll tmux pane for the sentinel string. Returns (full_pane_text,
        sentinel_seen)."""
        start = time.time()
        last_pane = ""
        while time.time() - start < deadline_s:
            try:
                pane = _tmux_capture(self.tmux_session)
            except subprocess.CalledProcessError:
                pane = ""
            if sentinel in pane:
                return pane, True
            last_pane = pane
            time.sleep(2.0)
        return last_pane, False

    @staticmethod
    def _extract_final_text(pane: str, sentinel: str) -> str:
        """Pull the chunk of pane text just before the sentinel — that's
        the agent's final response."""
        idx = pane.rfind(sentinel)
        if idx == -1:
            return pane[-4000:]
        # Walk backwards a few thousand chars to capture the final response
        return pane[max(0, idx - 6000):idx]

    def run(self, task: TaskSpec, run_dir: Path) -> RunResult:
        from .base import short_uid  # local import to avoid cycle warning
        sentinel = f"{_SENTINEL_PREFIX}{short_uid(8)}"

        actions_log: list[dict] = []
        messages_log: list[dict] = []

        try:
            self._verify_daemon()
            self._verify_tmux()
        except Exception as e:
            return RunResult(
                task_id=task.id, family=self.family, model=self.model,
                run_id=run_dir.parent.parent.name,
                started_at=self.now_iso(), ended_at=self.now_iso(),
                duration_s=0.0, n_tool_calls=0, n_assistant_messages=0,
                n_screenshots=0, thinking_chars=0,
                prompt_tokens=0, completion_tokens=0,
                final_answer="", final_answer_parsed=None,
                termination_reason="error",
                error_message=f"setup failed: {e}",
            )

        self._reset_browser()
        tasks_before = _list_task_ids()

        prompt = self._build_prompt(task, sentinel)
        started_at = self.now_iso()
        t0 = time.time()
        _tmux_paste_prompt(self.tmux_session, prompt)

        # Wait for the agent to finish or time out.
        pane, saw_sentinel = self._wait_for_sentinel(
            sentinel, deadline_s=task.max_duration_s + 30.0
        )

        duration = time.time() - t0
        ended_at = self.now_iso()

        final_text = self._extract_final_text(pane, sentinel) if saw_sentinel else pane[-6000:]
        parsed = extract_json_block(final_text)

        # Take final screenshot.
        screenshot_path = run_dir / "screenshots" / "final.png"
        _screenshot(screenshot_path)

        # Save raw tmux pane for debugging.
        (run_dir / "tmux_pane.txt").write_text(pane)

        # Identify the new DETM task and pull its metrics.
        tasks_after = _list_task_ids()
        new_task_ids = list(tasks_after - tasks_before)
        detm_task_summary = None
        n_tool_calls = 0
        n_assistant_messages = 0
        if new_task_ids:
            # Pick the most recently created.
            details = []
            for tid in new_task_ids:
                d = _get_task(tid)
                if d:
                    details.append(d)
            details.sort(key=lambda d: d.get("created_at", ""), reverse=True)
            if details:
                detm_task_summary = details[0]
                # Sum actions_taken across all gui_agent calls.
                for item in detm_task_summary.get("items", []):
                    for ad in item.get("action_details", []):
                        out = ad.get("output_data") or "{}"
                        try:
                            out_obj = json.loads(out) if isinstance(out, str) else out
                        except Exception:
                            out_obj = {}
                        n_tool_calls += int(out_obj.get("actions_taken", 0))
                        n_assistant_messages += 1
                        actions_log.append({
                            "id": ad.get("id"),
                            "type": ad.get("action_type"),
                            "summary": ad.get("summary"),
                            "status": ad.get("status"),
                            "created_at": ad.get("created_at"),
                            "actions_taken": int(out_obj.get("actions_taken", 0)),
                        })
                # Copy DETM raw artifacts in.
                _copy_detm_artifacts(detm_task_summary["task_id"], run_dir)

        # The OpenClaw TUI itself is the "assistant" — without parsing it
        # turn-by-turn we can only count gui_agent invocations as a lower
        # bound. Stash the raw pane for later analysis.
        messages_log.append({
            "role": "system",
            "content": "OpenClaw TUI pane snapshot — see tmux_pane.txt for full text",
        })

        termination = "completed" if saw_sentinel else "timeout"
        if not parsed and saw_sentinel:
            termination = "completed_no_json"

        result = RunResult(
            task_id=task.id, family=self.family, model=self.model,
            run_id=run_dir.parent.parent.name,
            started_at=started_at, ended_at=ended_at, duration_s=duration,
            n_tool_calls=n_tool_calls,
            n_assistant_messages=n_assistant_messages,
            n_screenshots=1 if screenshot_path.exists() else 0,
            thinking_chars=0,  # OpenClaw TUI output isn't structured enough
                               # to extract thinking — leave as 0.
            prompt_tokens=0,    # not exposed from this side
            completion_tokens=0,
            final_answer=final_text,
            final_answer_parsed=parsed,
            termination_reason=termination,
            error_message=None,
        )
        save_run_artifacts(run_dir, result, actions=actions_log, messages=messages_log)
        # Stash the DETM task summary for traceability.
        if detm_task_summary:
            (run_dir / "detm_task_summary.json").write_text(
                json.dumps(detm_task_summary, indent=2)
            )
        return result
