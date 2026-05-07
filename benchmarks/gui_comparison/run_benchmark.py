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


def _write_progress_table(
    run_id: str,
    plan: list[tuple[str, str, str]],
    statuses: dict[tuple[str, str, str], dict],
    started_at: float,
) -> None:
    """Atomically (re)write results/<run_id>/progress.md with the current
    state of every (family, model, task) trial in `plan`. Called once at
    orchestrator start (all rows = pending), then again after each trial
    transitions state. The user can `watch -n 5 cat progress.md` to follow
    the sweep live."""
    run_dir = RESULTS_DIR / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # Counts for the header.
    n_total = len(plan)
    completed_states = {"completed", "completed_no_json", "failed", "timeout",
                         "max_actions", "error", "construct_failed"}
    n_done = sum(1 for k in plan if statuses[k].get("status") in completed_states)
    n_running = sum(1 for k in plan if statuses[k].get("status") == "running")
    n_pending = sum(1 for k in plan if statuses[k].get("status") == "pending")
    n_ok = sum(
        1 for k in plan
        if (statuses[k].get("log_score") or 0) >= 0.99
    )
    elapsed = time.time() - started_at

    md = [
        f"# Run: `{run_id}`",
        "",
        f"- Progress: **{n_done}/{n_total}** complete "
        f"({n_running} running, {n_pending} pending)",
        f"- Perfect (log_score ≥ 0.99): {n_ok}",
        f"- Elapsed: {elapsed/60:.1f} min",
        "",
        "Watch live: `watch -n 5 cat " + str(run_dir / "progress.md") + "`",
        "",
        "| family | model | task | status | log_score | video_score | duration_s | actions |",
        "|---|---|---|---|---|---|---|---|",
    ]

    def _fmt(v):
        if v is None:
            return "-"
        if isinstance(v, float):
            return f"{v:.2f}"
        return str(v)

    for (fam, mdl, task_id) in plan:
        s = statuses.get((fam, mdl, task_id), {"status": "pending"})
        status_icon = {
            "pending": "⏳ pending",
            "running": "🟡 running",
            "completed": "✅ completed",
            "completed_no_json": "⚠️ completed_no_json",
            "failed": "❌ failed",
            "timeout": "⏱ timeout",
            "max_actions": "🛑 max_actions",
            "error": "💥 error",
            "construct_failed": "💥 construct_failed",
        }.get(s.get("status", "?"), s.get("status", "?"))
        md.append(
            f"| {fam} | {mdl} | {task_id} | {status_icon} | "
            f"{_fmt(s.get('log_score'))} | {_fmt(s.get('video_score'))} | "
            f"{_fmt(s.get('duration_s'))} | {_fmt(s.get('actions'))} |"
        )

    text = "\n".join(md) + "\n"

    # Atomic write so the user never sees a half-written file.
    out = run_dir / "progress.md"
    tmp = out.with_suffix(".md.tmp")
    tmp.write_text(text)
    tmp.replace(out)


def _judge_run(run_dir: Path, task: TaskSpec, final_answer: str) -> dict:
    screenshot = run_dir / "screenshots" / "final.png"
    # Load the action log so the judge can apply rubrics that reference
    # the trajectory (e.g., task 14's "did the agent visit the archive
    # vs typing the URL directly").
    actions = []
    actions_path = run_dir / "actions.jsonl"
    if actions_path.exists():
        for line in actions_path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                actions.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return run_judge(
        task,
        final_answer=final_answer,
        final_screenshot_path=screenshot if screenshot.exists() else None,
        actions=actions or None,
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
    p.add_argument("--video-judge", action="store_true",
                   help="ALSO run the gemini-3.1-pro video judge on the "
                        "trial's recording.mp4 (requires BENCH_RECORD_VIDEO=1 "
                        "or recording.mp4 already present). Costs more; "
                        "off by default.")
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

    # Build the (family, model, task) plan up front so we can show a
    # live progress table that gets updated after each trial finishes.
    plan: list[tuple[str, str, str]] = [
        (family, model, task.id)
        for family in families
        for model in models
        for task in tasks
    ]
    statuses: dict[tuple[str, str, str], dict] = {
        c: {"status": "pending"} for c in plan
    }
    started_at_run = time.time()
    _write_progress_table(args.run_id, plan, statuses, started_at_run)
    print(
        f"\nProgress table: {RESULTS_DIR / args.run_id / 'progress.md'}\n"
        f"(updated after each trial — `watch -n 5 cat <path>` to follow live)\n"
    )

    for family in families:
        for model in models:
            try:
                runner = _make_runner(family, model)
            except Exception as e:
                print(f"[{family} / {model}] runner construction failed: {e}")
                # Mark all of this (family, model)'s remaining trials as
                # construct_failed so the progress table reflects it.
                for task in tasks:
                    statuses[(family, model, task.id)] = {
                        "status": "construct_failed",
                        "error": str(e)[:200],
                    }
                _write_progress_table(args.run_id, plan, statuses, started_at_run)
                continue
            for task in tasks:
                statuses[(family, model, task.id)]["status"] = "running"
                _write_progress_table(args.run_id, plan, statuses, started_at_run)
                run_dir = make_run_dir(args.run_id, family, model, task.id)
                print(f"\n────────────────────────────────────────────────────────")
                print(f"[{family} / {model}] task={task.id}  →  {run_dir.relative_to(RESULTS_DIR.parent)}")
                t0 = time.time()
                try:
                    result = runner.run(task, run_dir)
                except Exception as e:
                    err_msg = f"{type(e).__name__}: {e}"
                    print(f"  ✗ runner.run() raised: {err_msg}")
                    traceback.print_exc()
                    # Synthesize a metrics.json so this trial appears in
                    # the aggregator + summary as an error, not a hole.
                    crash_result_dict = {
                        "task_id": task.id, "family": family, "model": model,
                        "run_id": args.run_id,
                        "started_at": "", "ended_at": "",
                        "duration_s": round(time.time() - t0, 1),
                        "n_tool_calls": 0, "n_assistant_messages": 0,
                        "n_screenshots": 0, "thinking_chars": 0,
                        "prompt_tokens": 0, "completion_tokens": 0,
                        "final_answer": "", "final_answer_parsed": None,
                        "termination_reason": "error",
                        "judge_success": None,
                        "judge_partial_credit": None,
                        "judge_reason": None,
                        "error_message": f"runner.run() raised: {err_msg}",
                    }
                    (run_dir / "metrics.json").write_text(
                        json.dumps(crash_result_dict, indent=2)
                    )
                    statuses[(family, model, task.id)] = {
                        "status": "error",
                        "log_score": None, "video_score": None,
                        "duration_s": round(time.time() - t0, 1),
                        "actions": None,
                    }
                    _write_progress_table(args.run_id, plan, statuses, started_at_run)
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

                # Optional second-pass video judge (gemini-3.1-pro).
                # Independent of the log judge — produces its own verdict
                # in judge_video_verdict.json so the writeup can show
                # both side by side and flag disagreement.
                recording = run_dir / "recording.mp4"
                if args.video_judge and recording.exists():
                    print(f"  video-judging (gemini-3.1-pro)...")
                    try:
                        from gui_comparison.judges.video_judge import judge_video
                        v_verdict = judge_video(
                            task, result.final_answer, recording,
                        )
                        (run_dir / "judge_video_verdict.json").write_text(
                            json.dumps(v_verdict, indent=2)
                        )
                        print(f"  video judge: success={v_verdict.get('success')} "
                              f"score={v_verdict.get('partial_credit', 0):.2f} "
                              f"mode={v_verdict.get('mode')}")
                        print(f"  v-reason: {(v_verdict.get('reason') or '')[:200]}")
                    except Exception as e:
                        print(f"  ✗ video judge failed: {e}")
                elif args.video_judge:
                    print(f"  (video-judge requested but no recording.mp4)")

                # Update progress table with this trial's outcome.
                key = (family, model, task.id)
                vverdict_path = run_dir / "judge_video_verdict.json"
                video_score = None
                if vverdict_path.exists():
                    try:
                        video_score = json.loads(vverdict_path.read_text()).get("partial_credit")
                    except Exception:
                        pass
                statuses[key] = {
                    "status": result.termination_reason,
                    "log_score": result.judge_partial_credit,
                    "video_score": video_score,
                    "duration_s": round(result.duration_s, 1),
                    "actions": result.n_tool_calls,
                }
                _write_progress_table(args.run_id, plan, statuses, started_at_run)

                summary.append(result)

    print(f"\n══════════════════════════════════════════════════════════")
    print(f"Summary for run_id={args.run_id}: {len(summary)} trials")
    # Per-(family, model) breakdown — one row each. Aggregating across
    # ALL trials would mix real and zero-token Families together and
    # obscure the per-family performance picture.
    from collections import defaultdict
    groups: dict[tuple[str, str], list] = defaultdict(list)
    for r in summary:
        groups[(r.family, r.model)].append(r)
    for (fam, mdl), trials in sorted(groups.items()):
        n = len(trials)
        n_ok = sum(1 for t in trials if t.judge_success)
        avg_score = sum(t.judge_partial_credit or 0 for t in trials) / max(n, 1)
        n_no_json = sum(1 for t in trials if t.termination_reason == "completed_no_json")
        n_err = sum(1 for t in trials if t.termination_reason == "error")
        print(
            f"  {fam:16s} {mdl:25s}  "
            f"{n_ok}/{n} ok  avg_score={avg_score:.3f}  "
            f"no_json={n_no_json}  err={n_err}"
        )
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
            actions = []
            actions_path = task_dir / "actions.jsonl"
            if actions_path.exists():
                for line in actions_path.read_text().splitlines():
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        actions.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            verdict = run_judge(
                task,
                final_answer=final_answer,
                final_screenshot_path=(
                    task_dir / "screenshots" / "final.png"
                    if (task_dir / "screenshots" / "final.png").exists()
                    else None
                ),
                actions=actions or None,
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
