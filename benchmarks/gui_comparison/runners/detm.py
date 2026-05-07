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

The runner DOES cancel any in-flight gui_agent at the start of each
trial (via /gui_agent/cancel?all=true) and again after _wait_for_sentinel
returns, so a still-navigating agent doesn't bleed into the next task.
On timeout we return whatever the agent produced and mark
termination_reason="timeout"; the judge decides whether the partial
answer was salvageable.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional
from urllib.request import Request, urlopen
from urllib.error import URLError

from .base import RunResult, RunnerBase, TaskSpec, extract_json_block, save_run_artifacts


DAEMON_URL = os.environ.get("DETM_DAEMON_URL", "http://127.0.0.1:18790")
# Default to "bench-oc" — this is what the README documents and what we
# actually use in smokes. The runner kills+recreates this session per
# task, so it doesn't need to pre-exist.
TMUX_SESSION = os.environ.get("BENCH_TMUX_SESSION", "bench-oc")
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
    """Snapshot of every task id visible to /api/tasks across all statuses.

    The daemon's /api/tasks defaults to status=active, limit=20. We need
    the full set to do correct before/after diffing AND correct action-
    detail attribution (we previously assumed action attribution didn't
    depend on this — that was wrong; if a task isn't in our set, we never
    fetch its action_details). So we explicitly query each status with a
    high limit to get the full picture.
    """
    ids: set[str] = set()
    for status in ("active", "completed", "failed", "cancelled", "paused", "pending"):
        try:
            r = _get_json(f"/api/tasks?status={status}&limit=10000")
            ids.update(t["task_id"] for t in r.get("tasks", []))
        except Exception:
            continue
    return ids


def _get_task(task_id: str) -> Optional[dict]:
    try:
        return _get_json(f"/api/tasks/{task_id}")
    except Exception:
        return None


def _cancel_all_gui_agents() -> bool:
    """POST /gui_agent/cancel?all=true so any in-flight gui_agent is
    stopped before we start the next benchmark task. Returns True on
    success, False on any error (best-effort)."""
    try:
        req = Request(
            f"{DAEMON_URL}/gui_agent/cancel",
            data=json.dumps({"all": True}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urlopen(req, timeout=5).read()
        return True
    except Exception:
        return False


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


# `_tmux_paste_prompt` is gone — superseded by passing the prompt as
# `openclaw tui --message <prompt>` at TUI startup, which preserves
# newlines and starts the agent immediately. See _recreate_oc_session.


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
        # Warn if the daemon URL is non-local — our screenshot helper still
        # captures the local DISPLAY, which won't match what a remote agent
        # sees and will produce nonsense judge verdicts.
        if not any(host in DAEMON_URL for host in ("127.0.0.1", "localhost")):
            print(
                f"  ⚠ DETM_DAEMON_URL={DAEMON_URL!r} is non-local but "
                f"screenshots will still be captured from local DISPLAY "
                f"{DISPLAY!r}. Judge verdicts will be unreliable."
            )
        # The daemon's gui_agent model should match what we requested.
        # We compare on `live_ui_model` since that's what `/health` exposes
        # for the bash backend.
        live_model = h.get("live_ui_model", "")
        if not live_model:
            raise RuntimeError(
                f"DETM daemon at {DAEMON_URL} has no live_ui_model set "
                f"(/health returned {h.get('live_ui_model')!r}). The daemon "
                f"may be in a half-started state. Verify it's actually "
                f"serving the bash backend with ACU_OPENROUTER_GUI_DIRECT_MODEL set."
            )
        if live_model != self.model:
            raise RuntimeError(
                f"DETM daemon is configured with live_ui_model={live_model!r}, "
                f"but this runner requested model={self.model!r}. "
                f"Restart the daemon with ACU_OPENROUTER_GUI_DIRECT_MODEL={self.model} "
                f"and re-run."
            )
        return h

    def _recreate_oc_session(
        self, task_id: str, run_id: str, prompt: str
    ) -> None:
        """Kill any existing tmux session named `self.tmux_session` and
        start a fresh one running:

            openclaw tui --session <key> --message "$(cat <tmpfile>)"

        --message delivers the prompt as the initial user message, so
        the agent starts working immediately. The prompt is written to
        a tmpfile and read via shell-substitution because tmux
        `send-keys` interprets embedded \\n in the command string as
        Enter keys — passing a multi-line command directly causes the
        shell to get stuck in a quoted-string continuation. The
        tmpfile-via-$(cat) trick keeps the actual `tmux send-keys`
        command on a single line.

        The tmux session is just a TTY container; the TUI process needs
        a terminal. We wait for the openclaw header to appear (proof
        the TUI connected) and then return — the agent is already
        processing the prompt by that point.

        Raises RuntimeError if the TUI doesn't come up within 60s.
        """
        # Sanitize task_id + run_id for the openclaw --session arg (alnum +
        # hyphen). Truncate to keep tmux/openclaw key lengths sane.
        sess_key = re.sub(r"[^A-Za-z0-9-]", "-", f"bench-{run_id}-{task_id}")[:60]

        # Kill any prior session of this name.
        subprocess.run(
            ["tmux", "kill-session", "-t", self.tmux_session],
            stderr=subprocess.DEVNULL,
            check=False,
        )
        time.sleep(0.5)

        # Detached new session, wider+taller than default for long output.
        subprocess.check_call([
            "tmux", "new-session", "-d", "-s", self.tmux_session,
            "-x", "220", "-y", "60",
        ], timeout=10)

        # Write the prompt to a tmpfile (handles arbitrary content
        # including newlines, quotes, backticks). Shell will read it
        # via $(cat ...) as a single argument to --message.
        msg_path = Path(f"/tmp/bench_oc_msg_{sess_key}.txt")
        msg_path.write_text(prompt)

        # Single-line command — no embedded newlines for tmux to misinterpret.
        cmd_str = (
            f'openclaw tui --session {shlex.quote(sess_key)} '
            f'--message "$(cat {shlex.quote(str(msg_path))})"'
        )
        subprocess.check_call([
            "tmux", "send-keys", "-t", self.tmux_session,
            cmd_str,
            "Enter",
        ], timeout=10)

        # Wait for the TUI to connect — the openclaw header line tells us.
        # We DON'T wait for "idle" because with --message the agent starts
        # processing immediately and may never go idle before producing
        # the answer.
        deadline = time.time() + 60.0
        while time.time() < deadline:
            try:
                pane = _tmux_capture(self.tmux_session, lines=200)
            except Exception:
                pane = ""
            # The TUI prints "openclaw tui - ws://..." once connected,
            # plus a "session agent:main:<key>" line.
            if "openclaw tui -" in pane and sess_key in pane:
                return
            time.sleep(1.0)
        raise RuntimeError(
            f"openclaw tui did not become ready in 60s in session "
            f"{self.tmux_session}. Last pane head: {pane[:300]!r}"
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
        """Build the prompt that gets passed to `openclaw tui --message`.

        Newlines are preserved verbatim (no flattening — shell-escape via
        shlex.quote in _recreate_oc_session handles them). The marker +
        sentinel pattern is unchanged: the runner extracts the answer
        between the LAST occurrence of <<<BENCH_ANSWER_START>>> and
        <<<BENCH_ANSWER_END>>>, and waits for the sentinel to know the
        agent is done.
        """
        return (
            f"DETM ONLY\n\n"
            f"{task.prompt}\n\n"
            f"---\n\n"
            f"When you finish the task (success OR failure), produce your "
            f"final JSON answer wrapped in these EXACT markers, each on "
            f"their own line:\n\n"
            f"<<<BENCH_ANSWER_START>>>\n"
            f"```json\n"
            f"{{... your JSON answer here ...}}\n"
            f"```\n"
            f"<<<BENCH_ANSWER_END>>>\n\n"
            f"Then on a new line type exactly: {sentinel}\n\n"
            f"Do not type {sentinel} before you have produced both markers "
            f"and the JSON between them. Do not abbreviate the markers."
        )

    def _wait_for_sentinel(
        self, sentinel: str, baseline_count: int, deadline_s: float
    ) -> tuple[str, bool]:
        """Poll tmux pane for one MORE occurrence of the sentinel than was
        present at baseline_count.

        baseline_count is the count of sentinel occurrences in the pane
        immediately after we pasted the prompt — that paste itself contains
        the sentinel string (in the instruction "type exactly: <sentinel>"),
        so we must wait for the count to grow before declaring the agent
        done.

        Returns (full_pane_text, sentinel_seen)."""
        start = time.time()
        last_pane = ""
        while time.time() - start < deadline_s:
            try:
                pane = _tmux_capture(self.tmux_session)
            except subprocess.CalledProcessError:
                pane = ""
            if pane.count(sentinel) > baseline_count:
                return pane, True
            last_pane = pane
            time.sleep(2.0)
        return last_pane, False

    _ANSWER_START_MARKER = "<<<BENCH_ANSWER_START>>>"
    _ANSWER_END_MARKER = "<<<BENCH_ANSWER_END>>>"

    @classmethod
    def _extract_final_text(cls, pane: str, sentinel: str, baseline_len: int = 0) -> str:
        """Pull just the agent's final-answer block out of the tmux pane.

        Strategy (most-specific to most-fallback):
          1. Find the LAST occurrence of <<<BENCH_ANSWER_END>>> — the agent
             emitted it after their answer (the prompt also contains the
             marker as part of the instruction, but the agent's output is
             always after the prompt).
          2. Walk backward to the matching <<<BENCH_ANSWER_START>>> and
             extract between them.
          3. If markers aren't found, fall back to the old "search past
             baseline_len for sentinel" approach.
          4. If even that fails, return the last 4000 chars.

        The baseline-length approach is fragile (tmux can redraw and shift
        offsets), so the marker-based path is preferred.
        """
        # Strategy 1: marker-based extraction.
        end_idx = pane.rfind(cls._ANSWER_END_MARKER)
        if end_idx != -1:
            # Find the matching START marker before this END.
            start_search_region = pane[:end_idx]
            start_idx = start_search_region.rfind(cls._ANSWER_START_MARKER)
            if start_idx != -1:
                inner_start = start_idx + len(cls._ANSWER_START_MARKER)
                return pane[inner_start:end_idx].strip()

        # Strategy 2: baseline-length offset.
        new_text = pane[baseline_len:]
        idx_in_new = new_text.find(sentinel)
        if idx_in_new != -1:
            idx = baseline_len + idx_in_new
            return pane[max(baseline_len, idx - 6000):idx]

        # Strategy 3: fall through.
        return pane[-4000:]

    def run(self, task: TaskSpec, run_dir: Path) -> RunResult:
        from .base import short_uid  # local import to avoid cycle warning
        sentinel = f"{_SENTINEL_PREFIX}{short_uid(8)}"
        run_id = run_dir.parent.parent.name

        actions_log: list[dict] = []
        messages_log: list[dict] = []

        # Build the prompt BEFORE we spin up the session — we pass it as
        # `--message` so the agent starts working immediately.
        prompt = self._build_prompt(task, sentinel)

        try:
            self._verify_daemon()
            # Cancel any in-flight gui_agent left over from a prior run
            # before we start. This is also our defense against runs where
            # the previous task's agent is still navigating when we arrive.
            _cancel_all_gui_agents()
            # Recreate the tmux/OpenClaw session WITH the prompt as the
            # initial message. No paste-buffer dance, no newline flattening.
            self._reset_browser()
            tasks_before = _list_task_ids()
            self._recreate_oc_session(task.id, run_id, prompt)
        except Exception as e:
            r = RunResult(
                task_id=task.id, family=self.family, model=self.model,
                run_id=run_id,
                started_at=self.now_iso(), ended_at=self.now_iso(),
                duration_s=0.0, n_tool_calls=0, n_assistant_messages=0,
                n_screenshots=0, thinking_chars=0,
                prompt_tokens=0, completion_tokens=0,
                final_answer="", final_answer_parsed=None,
                termination_reason="error",
                error_message=f"setup failed: {e}",
            )
            save_run_artifacts(run_dir, r)
            return r

        # Optional screen recording (BENCH_RECORD_VIDEO=1).
        from .recorder import DisplayRecorder
        recorder = DisplayRecorder(out_path=run_dir / "recording.mp4")
        recorder.start()

        # Capture the baseline pane AFTER the user message has been
        # rendered (otherwise our sentinel-count baseline is wrong:
        # baseline=0, then once openclaw renders the prompt the sentinel
        # appears in the pane, making the count grow and tricking
        # _wait_for_sentinel into thinking the agent finished).
        # We detect "prompt rendered" by waiting for a chunk of the
        # prompt's tail to appear in the pane.
        prompt_tail = prompt[-60:].strip()
        render_deadline = time.time() + 30.0
        baseline_pane = ""
        while time.time() < render_deadline:
            try:
                baseline_pane = _tmux_capture(self.tmux_session)
            except subprocess.CalledProcessError:
                baseline_pane = ""
            if prompt_tail and prompt_tail in baseline_pane:
                break
            time.sleep(1.0)
        baseline_count = baseline_pane.count(sentinel)

        # Now that setup is done (TUI up + prompt rendered), start the
        # task clock. duration_s should reflect agent work time, not
        # setup overhead.
        started_at = self.now_iso()
        t0 = time.time()

        # Wait for the agent to finish or time out.
        pane, saw_sentinel = self._wait_for_sentinel(
            sentinel, baseline_count=baseline_count,
            deadline_s=task.max_duration_s + 30.0,
        )
        baseline_len = len(baseline_pane)

        # Always cancel any still-running gui_agent so the next task
        # doesn't inherit it (regardless of whether we saw the sentinel).
        _cancel_all_gui_agents()

        duration = time.time() - t0
        ended_at = self.now_iso()

        final_text = (
            self._extract_final_text(pane, sentinel, baseline_len)
            if saw_sentinel else pane[-6000:]
        )
        parsed = extract_json_block(final_text)

        # Take final screenshot, then stop recording.
        screenshot_path = run_dir / "screenshots" / "final.png"
        _screenshot(screenshot_path)
        recorder.stop()

        # Save raw tmux pane for debugging.
        (run_dir / "tmux_pane.txt").write_text(pane)

        # Identify DETM activity during this run.
        #
        # We can't rely solely on "new task_id appeared" because OpenClaw
        # sometimes routes gui_agent calls to a pre-existing task (when
        # task_register isn't called, or when DETM matches by name). So we
        # widen the scope: collect every action_detail across every task
        # whose `created_at` falls between started_at and ended_at + grace,
        # regardless of which DETM task it lives on.
        tasks_after = _list_task_ids()
        all_task_ids = tasks_before | tasks_after
        new_task_ids = list(tasks_after - tasks_before)
        detm_task_summary = None
        n_tool_calls = 0
        n_assistant_messages = 0
        action_window_start = started_at
        # ended_at is computed below; we use the wall-clock 'now' minus
        # a small buffer so any action_detail produced during this run
        # is captured.
        for tid in all_task_ids:
            d = _get_task(tid)
            if not d:
                continue
            for item in d.get("items", []):
                for ad in item.get("action_details", []):
                    ad_ts = ad.get("created_at", "")
                    if not ad_ts:
                        continue
                    # Half-open interval [start, end) — excludes the
                    # boundary so an action_detail at the exact instant
                    # one task ended and the next began isn't double-
                    # counted across two adjacent runs.
                    if not (action_window_start <= ad_ts < ended_at):
                        continue
                    out = ad.get("output_data") or "{}"
                    try:
                        out_obj = json.loads(out) if isinstance(out, str) else out
                    except Exception:
                        out_obj = {}
                    n_tool_calls += int(out_obj.get("actions_taken", 0))
                    n_assistant_messages += 1
                    actions_log.append({
                        "detm_task_id": tid,
                        "id": ad.get("id"),
                        "type": ad.get("action_type"),
                        "summary": ad.get("summary"),
                        "status": ad.get("status"),
                        "created_at": ad.get("created_at"),
                        "actions_taken": int(out_obj.get("actions_taken", 0)),
                    })
        # Pick the new task (if any) as the "primary" detm_task for
        # artifact copy + traceability.
        if new_task_ids:
            details = [d for d in (_get_task(tid) for tid in new_task_ids) if d]
            details.sort(key=lambda d: d.get("created_at", ""), reverse=True)
            if details:
                detm_task_summary = details[0]
                _copy_detm_artifacts(detm_task_summary["task_id"], run_dir)
        elif actions_log:
            # No new task — fall back to the task most-touched during this run.
            from collections import Counter
            counts = Counter(a["detm_task_id"] for a in actions_log)
            primary_tid = counts.most_common(1)[0][0]
            detm_task_summary = _get_task(primary_tid)
            if detm_task_summary:
                _copy_detm_artifacts(primary_tid, run_dir)

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
