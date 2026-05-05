"""Family C runner: Simular Agent S3.

Wraps the existing Agent S install at benchmarks/baselines/agent-s/
(cloned from simular-ai/Agent-S). Agent S3 is a desktop agent — its
.predict() method takes a screenshot + instruction and returns
(thought_blob, [pyautogui_action_strings]). It signals completion by
emitting "DONE" or "FAIL".

Pipeline:
  1. Construct the agent (planner = OpenRouter LLM, grounder = UI-TARS).
  2. Loop on display :99: screenshot → predict → execute → repeat.
  3. On DONE/FAIL or hitting caps: capture final screenshot + the
     agent's last response as the "final answer". Also reads
     /tmp/bench_agent_s_answer.txt if the agent wrote one (recommended
     in the prompt addendum).

We DO NOT use Agent S's own logger — we instrument every predict() call
with our own metrics: actions_taken, latency, thought length.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from .base import RunResult, RunnerBase, TaskSpec, extract_json_block, save_run_artifacts


REPO_ROOT = Path(__file__).resolve().parents[3]
AGENT_S_DIR = REPO_ROOT / "benchmarks" / "baselines" / "agent-s"
DISPLAY = os.environ.get("BENCH_DISPLAY", ":99")
ANSWER_FILE = Path("/tmp/bench_agent_s_answer.txt")


def _screenshot_display() -> bytes:
    """Take a PNG screenshot of $DISPLAY using scrot or import."""
    if shutil.which("scrot"):
        out = Path(f"/tmp/bench_ss_{os.getpid()}.png")
        rc = subprocess.run(
            ["scrot", "-z", str(out)],
            env={**os.environ, "DISPLAY": DISPLAY},
            stderr=subprocess.DEVNULL,
        ).returncode
        if rc == 0 and out.exists():
            data = out.read_bytes()
            try:
                out.unlink()
            except FileNotFoundError:
                pass
            return data
    if shutil.which("import"):
        return subprocess.check_output(
            ["import", "-window", "root", "-display", DISPLAY, "png:-"],
            timeout=10,
        )
    raise RuntimeError("Neither scrot nor 'import' is available for screenshots")


def _execute_pyautogui(code: str) -> None:
    """Run pyautogui code on $DISPLAY."""
    script = f"import pyautogui; pyautogui.FAILSAFE = False; {code}"
    subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "DISPLAY": DISPLAY},
        timeout=30,
    )


def _make_agent(model: str, ground_model: str = "bytedance/ui-tars-1.5-7b"):
    """Construct an AgentS3 instance, importing from baselines/agent-s/."""
    if not AGENT_S_DIR.exists():
        raise RuntimeError(
            f"Agent S not found at {AGENT_S_DIR}. "
            "Clone from https://github.com/simular-ai/Agent-S there."
        )
    if str(AGENT_S_DIR) not in sys.path:
        sys.path.insert(0, str(AGENT_S_DIR))

    # Agent S's grounding.py imports paddleocr in some code paths; on
    # systems where paddlepaddle isn't installed (no wheels for new
    # Python versions) those paths blow up at runtime, not import time.
    # Fail loudly up front so the user knows what to expect.
    try:
        import paddleocr  # noqa: F401
    except Exception:
        print(
            "  ⚠ paddleocr/paddlepaddle not installed — Agent S OCR-grounded "
            "code paths will crash mid-run if hit. (Pure UI-TARS grounding is "
            "fine.) Install paddlepaddle if you can; benchmarks/baselines/"
            "agent-s/requirements.txt requires it."
        )

    from gui_agents.s3.agents.agent_s import AgentS3
    from gui_agents.s3.agents.grounding import OSWorldACI

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY required for Agent S")

    # LMMEngineOpenRouter passes the model name straight through to
    # OpenRouter, so we want the raw "owner/model" form (e.g. "openai/gpt-5.4")
    # — NOT "openrouter/openai/gpt-5.4", which OpenRouter rejects as
    # "not a valid model ID".
    base_url = "https://openrouter.ai/api/v1"
    raw_model = model.split("openrouter/", 1)[1] if model.startswith("openrouter/") else model
    raw_ground = (
        ground_model.split("openrouter/", 1)[1]
        if ground_model.startswith("openrouter/")
        else ground_model
    )
    engine_params = {
        "engine_type": "open_router",
        "model": raw_model,
        "base_url": base_url,
        "api_key": api_key,
    }
    engine_params_grounding = {
        "engine_type": "open_router",
        "model": raw_ground,
        "base_url": base_url,
        "api_key": api_key,
        "grounding_width": 1920,
        "grounding_height": 1080,
    }
    grounding_agent = OSWorldACI(
        env=None,
        platform="linux",
        engine_params_for_generation=engine_params,
        engine_params_for_grounding=engine_params_grounding,
        width=1920,
        height=1080,
    )
    agent = AgentS3(engine_params, grounding_agent, platform="linux")
    return agent


def _coerce_thought(response: Any) -> str:
    """Pull a textual thought/plan out of whatever .predict() returned."""
    if isinstance(response, dict):
        return (
            response.get("thought")
            or response.get("plan")
            or json.dumps(response)
        )
    return str(response)


class AgentSRunner(RunnerBase):
    family = "agent_s"

    def __init__(self, model: str, ground_model: str = "bytedance/ui-tars-1.5-7b"):
        super().__init__(model=model)
        self.ground_model = ground_model

    def run(self, task: TaskSpec, run_dir: Path) -> RunResult:
        actions_log: list[dict] = []
        messages_log: list[dict] = []
        n_tool_calls = 0
        n_assistant_messages = 0
        thinking_chars = 0

        # Wipe any stale answer file.
        try:
            ANSWER_FILE.unlink()
        except FileNotFoundError:
            pass

        try:
            agent = _make_agent(self.model, self.ground_model)
            agent.reset()
        except Exception as e:
            r = RunResult(
                task_id=task.id, family=self.family, model=self.model,
                run_id=run_dir.parent.parent.name,
                started_at=self.now_iso(), ended_at=self.now_iso(),
                duration_s=0.0,
                n_tool_calls=0, n_assistant_messages=0, n_screenshots=0,
                thinking_chars=0, prompt_tokens=0, completion_tokens=0,
                final_answer="", final_answer_parsed=None,
                termination_reason="error",
                error_message=f"Agent S construction failed: {e}",
            )
            save_run_artifacts(run_dir, r)
            return r

        # Augment the prompt: instruct the agent to write its final JSON
        # answer to a known file before signaling DONE. (The native
        # Agent S API has no "speak" channel, so we have to bridge via
        # filesystem.)
        addendum = (
            "\n\n--- IMPORTANT (benchmark mode) ---\n"
            "Before signaling DONE, write your final JSON answer to "
            f"{ANSWER_FILE} by opening a terminal (right-click desktop → "
            "Open Terminal Here) and running:\n"
            f"  echo 'YOUR_JSON_HERE' > {ANSWER_FILE}\n"
            "If you cannot open a terminal, embed the JSON in your "
            "narration (final 'thought') in a ```json fenced block."
        )
        instruction = task.prompt + addendum

        started_at = self.now_iso()
        t0 = time.time()
        deadline = t0 + task.max_duration_s
        last_thought = ""
        termination = "completed"
        signaled = None  # "DONE" / "FAIL" / None

        for step in range(1, task.max_actions + 1):
            if time.time() > deadline:
                termination = "timeout"
                break

            try:
                png = _screenshot_display()
            except Exception as e:
                termination = "error"
                last_thought = f"screenshot failed: {e}"
                break

            obs = {"screenshot": png}
            try:
                response, actions = agent.predict(instruction, obs)
            except Exception as e:
                termination = "error"
                last_thought = f"agent.predict failed: {e}"
                break

            n_assistant_messages += 1
            thought = _coerce_thought(response)
            last_thought = thought
            thinking_chars += len(thought)

            messages_log.append({
                "step": step, "role": "assistant", "thought_chars": len(thought),
                "actions": actions,
            })

            for action in actions:
                if time.time() > deadline:
                    termination = "timeout"
                    break
                # DONE/FAIL/WAIT are agent-control signals, not real tool
                # calls — log them but DON'T count toward n_tool_calls (we
                # want this metric to be apples-to-apples with Family A,
                # which only counts real Playwright tool invocations).
                if action == "DONE":
                    actions_log.append({
                        "step": step, "n": n_tool_calls, "action": "DONE",
                    })
                    signaled = "DONE"
                    break
                if action == "FAIL":
                    actions_log.append({
                        "step": step, "n": n_tool_calls, "action": "FAIL",
                    })
                    signaled = "FAIL"
                    break
                if action == "WAIT":
                    actions_log.append({
                        "step": step, "n": n_tool_calls, "action": "WAIT",
                    })
                    time.sleep(2.0)
                    continue
                # Real action — count, log, execute.
                n_tool_calls += 1
                actions_log.append({
                    "step": step, "n": n_tool_calls, "action": action[:300],
                })
                try:
                    _execute_pyautogui(action)
                    # 0.3s settle (was 1.0s — too aggressive). Most tasks
                    # need only enough time for the next screenshot to
                    # show the post-click state; 0.3s is sufficient on
                    # the local Xvfb display.
                    time.sleep(0.3)
                except Exception:
                    pass
                if n_tool_calls >= task.max_actions:
                    break

            if signaled or termination in ("timeout", "error"):
                break

        ended_at = self.now_iso()
        duration = time.time() - t0
        if signaled == "FAIL":
            termination = "failed"

        # Final screenshot.
        screenshot_path = run_dir / "screenshots" / "final.png"
        try:
            screenshot_path.write_bytes(_screenshot_display())
        except Exception:
            pass

        # Read the answer file if the agent wrote one; else fall back to
        # extracting JSON from the last thought.
        final_text = ""
        if ANSWER_FILE.exists():
            final_text = ANSWER_FILE.read_text(errors="replace")
        else:
            final_text = last_thought
        parsed = extract_json_block(final_text)

        result = RunResult(
            task_id=task.id, family=self.family, model=self.model,
            run_id=run_dir.parent.parent.name,
            started_at=started_at, ended_at=ended_at, duration_s=duration,
            n_tool_calls=n_tool_calls,
            n_assistant_messages=n_assistant_messages,
            n_screenshots=1 if screenshot_path.exists() else 0,
            thinking_chars=thinking_chars,
            prompt_tokens=0, completion_tokens=0,
            final_answer=final_text,
            final_answer_parsed=parsed,
            termination_reason=termination,
            error_message=None,
        )
        save_run_artifacts(run_dir, result, actions=actions_log, messages=messages_log)
        return result
