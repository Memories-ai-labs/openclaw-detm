#!/usr/bin/env python3
"""Benchmark orchestrator.

Usage examples:

  # Smoke run: DETM family with the current daemon model on tasks 1+6.
  python run_benchmark.py --family detm --models openai/gpt-5.4 \\
      --tasks 01,06 --run-id smoke-2026-05-05

  # Full run, all 15 tasks against one DETM model.
  python run_benchmark.py --family detm --models openai/gpt-5.4 \\
      --tasks all --run-id full-detm-gpt54

  # Just judge an existing run (re-score without rerunning).
  python run_benchmark.py --rejudge --run-id smoke-2026-05-05

The orchestrator does ONE trial per (family, model, task). Specify
--family multiple times or comma-separate --models / --tasks to fan out.

Results land in benchmarks/gui_comparison/results/<run-id>/.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from pathlib import Path

# Allow `python run_benchmark.py` from any cwd by adding parent on path.
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))  # benchmarks/

# Load repo-root .env so OPENROUTER_API_KEY etc. are available to runners
# and the judge without the user having to source it manually.
_ENV_FILE = HERE.parent.parent / ".env"
if _ENV_FILE.exists():
    import os
    for _line in _ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if not _line or _line.startswith("#") or "=" not in _line:
            continue
        _k, _v = _line.split("=", 1)
        _v = _v.strip().strip('"').strip("'")
        os.environ.setdefault(_k.strip(), _v)

from gui_comparison.runners.base import (
    RESULTS_DIR, TaskSpec, load_all_tasks, load_task, make_run_dir,
)
from gui_comparison.judges.model_judge import judge as run_judge


def _resolve_tasks(spec: str) -> list[TaskSpec]:
    if spec == "all":
        return load_all_tasks()
    ids = [s.strip() for s in spec.split(",") if s.strip()]
    out = []
    all_by_prefix = {t.id.split("_", 1)[0]: t for t in load_all_tasks()}
    for ident in ids:
        if ident in all_by_prefix:
            out.append(all_by_prefix[ident])
        else:
            out.append(load_task(ident))
    return out


def _make_runner(family: str, model: str):
    if family == "detm":
        from gui_comparison.runners.detm import DETMRunner
        return DETMRunner(model=model)
    if family == "playwright_mcp":
        from gui_comparison.runners.playwright_mcp import PlaywrightMCPRunner
        return PlaywrightMCPRunner(model=model)
    if family == "agent_s":
        from gui_comparison.runners.agent_s import AgentSRunner
        return AgentSRunner(model=model)
    raise ValueError(f"unknown family: {family}")


def _judge_run(run_dir: Path, task: TaskSpec, final_answer: str) -> dict:
    screenshot = run_dir / "screenshots" / "final.png"
    return run_judge(
        task,
        final_answer=final_answer,
        final_screenshot_path=screenshot if screenshot.exists() else None,
    )


def main() -> int:
    p = argparse.ArgumentParser(description="GUI agent comparison benchmark")
    p.add_argument("--family", action="append", default=None,
                   help="Family name. Repeat to run multiple. Choices: detm, playwright_mcp, agent_s")
    p.add_argument("--models", default="openai/gpt-5.4",
                   help="Comma-separated model names within the family. Default: openai/gpt-5.4")
    p.add_argument("--tasks", default="01",
                   help="Comma-separated task ids or short prefixes (e.g. 01,06) or 'all'")
    p.add_argument("--run-id", required=True,
                   help="Identifier for this benchmark run (becomes the results subdir)")
    p.add_argument("--rejudge", action="store_true",
                   help="Re-run only the judge against existing run dirs; skip agent runs")
    p.add_argument("--no-judge", action="store_true",
                   help="Skip the judge step (still records metrics)")
    args = p.parse_args()

    if args.rejudge:
        return _rejudge(args.run_id)

    families = args.family or ["detm"]
    models = [m.strip() for m in args.models.split(",") if m.strip()]
    tasks = _resolve_tasks(args.tasks)

    print(f"\nRun id: {args.run_id}")
    print(f"Families: {families}")
    print(f"Models:   {models}")
    print(f"Tasks:    {[t.id for t in tasks]}")
    print()

    summary = []
    for family in families:
        for model in models:
            try:
                runner = _make_runner(family, model)
            except Exception as e:
                print(f"[{family} / {model}] runner construction failed: {e}")
                continue
            for task in tasks:
                run_dir = make_run_dir(args.run_id, family, model, task.id)
                print(f"\n────────────────────────────────────────────────────────")
                print(f"[{family} / {model}] task={task.id}  →  {run_dir.relative_to(RESULTS_DIR.parent)}")
                t0 = time.time()
                try:
                    result = runner.run(task, run_dir)
                except Exception as e:
                    print(f"  ✗ runner.run() raised: {e}")
                    traceback.print_exc()
                    continue
                dt = time.time() - t0
                print(f"  duration={dt:.1f}s  termination={result.termination_reason}  "
                      f"actions={result.n_tool_calls}  msgs={result.n_assistant_messages}")
                if result.final_answer_parsed:
                    print(f"  parsed answer: {json.dumps(result.final_answer_parsed)[:200]}")
                else:
                    print(f"  no JSON parsed from agent answer")

                if not args.no_judge and result.termination_reason != "error":
                    print(f"  judging...")
                    try:
                        verdict = _judge_run(run_dir, task, result.final_answer)
                        result.judge_success = verdict["success"]
                        result.judge_partial_credit = verdict["partial_credit"]
                        result.judge_reason = verdict["reason"]
                        # Re-save metrics.json with judge fields.
                        (run_dir / "metrics.json").write_text(
                            json.dumps(result.to_dict(), indent=2)
                        )
                        (run_dir / "judge_verdict.json").write_text(
                            json.dumps(verdict, indent=2)
                        )
                        print(f"  judge: success={verdict['success']} "
                              f"score={verdict['partial_credit']:.2f}")
                        print(f"  reason: {verdict['reason'][:200]}")
                    except Exception as e:
                        print(f"  ✗ judge failed: {e}")

                summary.append(result)

    print(f"\n══════════════════════════════════════════════════════════")
    print(f"Summary for run_id={args.run_id}: {len(summary)} trials")
    n_success = sum(1 for r in summary if r.judge_success)
    avg_score = (
        sum(r.judge_partial_credit or 0 for r in summary) / max(len(summary), 1)
    )
    print(f"  success_rate: {n_success}/{len(summary)}")
    print(f"  avg_score:    {avg_score:.3f}")
    return 0


def _rejudge(run_id: str) -> int:
    """Walk results/<run_id>/, re-judge each task using the existing
    final_answer.txt + screenshots/final.png. Updates metrics.json + writes
    judge_verdict.json."""
    root = RESULTS_DIR / run_id
    if not root.exists():
        print(f"No such run: {root}")
        return 1
    for family_model_dir in sorted(root.iterdir()):
        if not family_model_dir.is_dir():
            continue
        for task_dir in sorted(family_model_dir.iterdir()):
            if not task_dir.is_dir():
                continue
            metrics_path = task_dir / "metrics.json"
            if not metrics_path.exists():
                continue
            metrics = json.loads(metrics_path.read_text())
            task_id = metrics.get("task_id")
            if not task_id:
                continue
            try:
                task = load_task(task_id)
            except FileNotFoundError:
                print(f"  skip {task_dir}: task {task_id} not found")
                continue
            final_answer = (task_dir / "final_answer.txt").read_text() if (
                task_dir / "final_answer.txt"
            ).exists() else metrics.get("final_answer", "")
            print(f"  judging {family_model_dir.name}/{task_id} ...")
            verdict = run_judge(
                task,
                final_answer=final_answer,
                final_screenshot_path=(
                    task_dir / "screenshots" / "final.png"
                    if (task_dir / "screenshots" / "final.png").exists()
                    else None
                ),
            )
            metrics["judge_success"] = verdict["success"]
            metrics["judge_partial_credit"] = verdict["partial_credit"]
            metrics["judge_reason"] = verdict["reason"]
            metrics_path.write_text(json.dumps(metrics, indent=2))
            (task_dir / "judge_verdict.json").write_text(json.dumps(verdict, indent=2))
            print(f"    success={verdict['success']} score={verdict['partial_credit']:.2f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
