"""Common scaffolding shared by every family runner.

Defines:
  - TaskSpec: parsed YAML task definition
  - RunResult: the metrics row for one (family, model, task) trial
  - load_task() / load_all_tasks(): YAML loaders
  - save_run_artifacts(): writes the per-run folder layout described
    in benchmarks/gui_comparison/README.md
  - extract_json_block(): pulls the agent's final JSON answer out of
    a free-text response

Every concrete runner subclasses RunnerBase and implements .run(task).
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
BENCH_ROOT = Path(__file__).resolve().parents[1]
TASKS_DIR = BENCH_ROOT / "tasks"
RESULTS_DIR = BENCH_ROOT / "results"


@dataclass
class TaskSpec:
    id: str
    title: str
    tier: int
    prompt: str
    max_actions: int
    max_duration_s: int
    judge_rubric: str
    family_constraints: dict = field(default_factory=dict)

    @classmethod
    def from_yaml(cls, path: Path) -> "TaskSpec":
        data = yaml.safe_load(path.read_text())
        return cls(
            id=data["id"],
            title=data["title"],
            tier=int(data["tier"]),
            prompt=data["prompt"],
            max_actions=int(data["max_actions"]),
            max_duration_s=int(data["max_duration_s"]),
            judge_rubric=data["judge_rubric"],
            family_constraints=data.get("family_constraints") or {},
        )


def load_task(task_id: str) -> TaskSpec:
    """Load a task by its id (e.g., '01_linkedin_anthropic_employees')."""
    path = TASKS_DIR / f"{task_id}.yaml"
    if not path.exists():
        raise FileNotFoundError(f"No such task: {task_id} (looked in {path})")
    return TaskSpec.from_yaml(path)


def load_all_tasks() -> list[TaskSpec]:
    tasks = []
    for path in sorted(TASKS_DIR.glob("*.yaml")):
        if path.stem.startswith("_"):
            continue
        tasks.append(TaskSpec.from_yaml(path))
    return tasks


@dataclass
class RunResult:
    """One trial of one family/model on one task. Serialized to metrics.json
    and aggregated into summary.csv."""

    task_id: str
    family: str           # "detm" | "playwright_mcp" | "agent_s"
    model: str            # e.g. "openai/gpt-5.4"
    run_id: str           # the parent benchmark run's id
    started_at: str       # iso8601
    ended_at: str
    duration_s: float
    n_tool_calls: int
    n_assistant_messages: int
    n_screenshots: int
    thinking_chars: int
    prompt_tokens: int
    completion_tokens: int
    final_answer: str           # the raw text the agent produced (may include JSON)
    final_answer_parsed: Optional[dict]   # parsed JSON answer, or None if unparseable
    termination_reason: str     # "completed" | "failed" | "timeout" | "max_actions" | "error"
    judge_success: Optional[bool] = None
    judge_partial_credit: Optional[float] = None
    judge_reason: Optional[str] = None
    error_message: Optional[str] = None

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


class RunnerBase(ABC):
    family: str = "<override-me>"

    def __init__(self, model: str):
        self.model = model

    @abstractmethod
    def run(self, task: TaskSpec, run_dir: Path) -> RunResult:
        """Execute one trial. Writes artifacts into run_dir, returns metrics."""

    # Common helpers -------------------------------------------------------

    def now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()


# ── JSON extraction ──────────────────────────────────────────────────────

_FENCE_RE = re.compile(r"```(?:json)?\s*\n(.*?)\n```", re.DOTALL)


def _balanced_objects(text: str) -> list[str]:
    """Walk `text` and yield every top-level balanced {...} block.

    Skips braces inside string literals (handles \" escapes). Avoids the
    greedy regex pitfall where `{garbage} ... { real json }` would match
    everything between the first `{` and last `}`.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        if text[i] != "{":
            i += 1
            continue
        depth = 0
        in_str = False
        esc = False
        start = i
        while i < n:
            c = text[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
            else:
                if c == '"':
                    in_str = True
                elif c == "{":
                    depth += 1
                elif c == "}":
                    depth -= 1
                    if depth == 0:
                        out.append(text[start:i + 1])
                        i += 1
                        break
            i += 1
        else:
            # Reached end without closing — abandon.
            break
    return out


def extract_json_block(text: str) -> Optional[dict]:
    """Pull a JSON object out of an agent's final response.

    Order of preference:
      1. The LAST fenced ```json block (agents tend to put their final
         answer last after some narration).
      2. Any balanced {...} top-level object that parses, walked left
         to right — the first one that parses wins.

    Returns None if nothing parses.
    """
    if not text:
        return None
    candidates: list[str] = []
    fences = _FENCE_RE.findall(text)
    if fences:
        # Last fence first — that's the most likely "final answer".
        candidates.append(fences[-1])
        candidates.extend(reversed(fences[:-1]))
    candidates.extend(_balanced_objects(text))
    for cand in candidates:
        try:
            return json.loads(cand)
        except json.JSONDecodeError:
            continue
    return None


# ── Artifact layout ──────────────────────────────────────────────────────

def make_run_dir(run_id: str, family: str, model: str, task_id: str) -> Path:
    """Create and return results/<run_id>/<family>__<modelslug>/<task_id>/.

    model slug: replace / and : with _ for filesystem safety.
    """
    slug = model.replace("/", "_").replace(":", "_")
    d = RESULTS_DIR / run_id / f"{family}__{slug}" / task_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "screenshots").mkdir(exist_ok=True)
    return d


def save_run_artifacts(
    run_dir: Path,
    result: RunResult,
    actions: list[dict] | None = None,
    messages: list[dict] | None = None,
) -> None:
    """Write metrics.json (+ optional jsonl logs) into run_dir."""
    (run_dir / "metrics.json").write_text(json.dumps(result.to_dict(), indent=2))
    if actions is not None:
        with (run_dir / "actions.jsonl").open("w") as f:
            for a in actions:
                f.write(json.dumps(a) + "\n")
    if messages is not None:
        with (run_dir / "messages.jsonl").open("w") as f:
            for m in messages:
                f.write(json.dumps(m) + "\n")
    if result.final_answer_parsed is not None:
        (run_dir / "final_answer.json").write_text(
            json.dumps(result.final_answer_parsed, indent=2)
        )
    (run_dir / "final_answer.txt").write_text(result.final_answer or "")


# ── Misc ─────────────────────────────────────────────────────────────────

def short_uid(n: int = 8) -> str:
    return uuid.uuid4().hex[:n]
