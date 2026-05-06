#!/usr/bin/env python3
"""Inspect benchmark trials — sibling of scripts/inspect_session.py.

Designed for nested debugging by an AI agent (Claude Code, etc.):
  1. Skim the run in compact mode (one line per trial)
  2. Drill into a suspicious trial to see the full action+message flow
  3. Pull the screenshot path for `Read`-into-context
  4. Pull DETM artifacts (tmux pane, journal) for the deep DETM dive

Usage:

    # List all run_ids that have results.
    python -m gui_comparison.analysis.inspect --list

    # Compact one-line-per-trial overview of a run.
    python -m gui_comparison.analysis.inspect <run_id>

    # Same, filtered to failures only.
    python -m gui_comparison.analysis.inspect <run_id> --failures

    # Same, sorted by duration descending (slowest first).
    python -m gui_comparison.analysis.inspect <run_id> --sort duration

    # Detail view of one trial (action log, messages summary, judge verdict).
    python -m gui_comparison.analysis.inspect <run_id> <family> <model> <task_id>

    # Same, with full action arguments + tool result excerpts.
    python -m gui_comparison.analysis.inspect <run_id> <family> <model> <task_id> -v

    # Print just the screenshot path so Claude Code can Read it.
    python -m gui_comparison.analysis.inspect <run_id> <family> <model> <task_id> --frame

    # Print just the tmux pane (DETM trials only).
    python -m gui_comparison.analysis.inspect <run_id> <family> <model> <task_id> --pane

The model name in the CLI may use the filesystem-safe slug
(`openai_gpt-5.4`) or the original (`openai/gpt-5.4`) — both resolve.
Task id can be the full id (`14_xkcd_archive_100`) or just the prefix
(`14`).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
RESULTS = HERE / "results"


# ── Discovery helpers ────────────────────────────────────────────────────

def _list_run_ids() -> list[str]:
    if not RESULTS.exists():
        return []
    return sorted(d.name for d in RESULTS.iterdir() if d.is_dir())


def _list_trials(run_id: str) -> list[Path]:
    """Return list of trial dirs under results/<run_id>/."""
    root = RESULTS / run_id
    if not root.exists():
        return []
    out = []
    for fam_model_dir in sorted(root.iterdir()):
        if not fam_model_dir.is_dir():
            continue
        for task_dir in sorted(fam_model_dir.iterdir()):
            if task_dir.is_dir() and (task_dir / "metrics.json").exists():
                out.append(task_dir)
    return out


def _load_metrics(trial_dir: Path) -> dict:
    try:
        return json.loads((trial_dir / "metrics.json").read_text())
    except Exception:
        return {}


def _model_slug(model: str) -> str:
    return model.replace("/", "_").replace(":", "_")


def _resolve_trial_dir(
    run_id: str, family: str, model: str, task_id: str
) -> Path | None:
    root = RESULTS / run_id
    if not root.exists():
        return None
    slug = _model_slug(model)
    candidate = root / f"{family}__{slug}" / task_id
    if candidate.exists():
        return candidate
    # Loose match: short task prefix (e.g. "14")
    fam_dir = root / f"{family}__{slug}"
    if fam_dir.exists():
        for d in fam_dir.iterdir():
            if d.is_dir() and d.name.startswith(task_id + "_"):
                return d
            if d.is_dir() and d.name == task_id:
                return d
    return None


# ── Pretty-printers ──────────────────────────────────────────────────────

def _fmt_score(m: dict) -> str:
    s = m.get("judge_partial_credit")
    ok = m.get("judge_success")
    if s is None:
        return "    -"
    marker = "✓" if ok else "✗"
    return f"{marker} {s:.2f}"


def _fmt_termination(m: dict) -> str:
    t = (m.get("termination_reason") or "?")[:14]
    if t == "completed":
        return f"\033[32m{t:14s}\033[0m"
    if t in ("timeout", "max_actions"):
        return f"\033[33m{t:14s}\033[0m"
    if t in ("error", "failed"):
        return f"\033[31m{t:14s}\033[0m"
    return f"{t:14s}"


def _print_compact(run_id: str, *, only_failures: bool, sort_by: str) -> None:
    trials = _list_trials(run_id)
    if not trials:
        print(f"No trials under results/{run_id}/")
        return

    rows = []
    for d in trials:
        m = _load_metrics(d)
        if only_failures and m.get("judge_success"):
            continue
        rows.append((d, m))

    if sort_by == "duration":
        rows.sort(key=lambda r: r[1].get("duration_s") or 0, reverse=True)
    elif sort_by == "score":
        rows.sort(key=lambda r: r[1].get("judge_partial_credit") or 0)
    elif sort_by == "actions":
        rows.sort(key=lambda r: r[1].get("n_tool_calls") or 0, reverse=True)

    print(f"\nRun: {run_id}  ({len(rows)} trials shown)\n")
    print(
        f"{'family':14s} {'model':22s} {'task':22s} "
        f"{'term':14s} {'sec':>6s} {'act':>4s} {'msg':>4s} score"
    )
    print("─" * 100)
    for d, m in rows:
        family = (m.get("family") or "?")[:14]
        model = (m.get("model") or "?")[:22]
        task = (m.get("task_id") or d.name)[:22]
        sec = m.get("duration_s") or 0
        actions = m.get("n_tool_calls") or 0
        msgs = m.get("n_assistant_messages") or 0
        print(
            f"{family:14s} {model:22s} {task:22s} "
            f"{_fmt_termination(m)} {sec:6.1f} {actions:4d} {msgs:4d} {_fmt_score(m)}"
        )


def _print_trial_detail(trial_dir: Path, verbose: bool = False) -> None:
    m = _load_metrics(trial_dir)
    if not m:
        print(f"No metrics in {trial_dir}")
        return

    print(f"\n══ Trial: {trial_dir.relative_to(RESULTS)}\n")
    print(f"  task        : {m.get('task_id')}")
    print(f"  family      : {m.get('family')}")
    print(f"  model       : {m.get('model')}")
    print(f"  run_id      : {m.get('run_id')}")
    print(f"  started_at  : {m.get('started_at')}")
    print(f"  duration_s  : {m.get('duration_s'):.1f}" if m.get('duration_s') else "  duration_s  : -")
    print(f"  termination : {m.get('termination_reason')}")
    if m.get("error_message"):
        print(f"  error       : {m['error_message']}")
    print(f"  actions     : {m.get('n_tool_calls')}")
    print(f"  messages    : {m.get('n_assistant_messages')}")
    print(f"  thinking    : {m.get('thinking_chars')} chars")
    pt = m.get("prompt_tokens") or 0
    ct = m.get("completion_tokens") or 0
    if pt or ct:
        print(f"  tokens      : prompt={pt}  completion={ct}")

    print(f"\n  judge       : success={m.get('judge_success')}  "
          f"score={m.get('judge_partial_credit')}")
    if m.get("judge_reason"):
        print(f"  reason      : {m['judge_reason'][:600]}")

    print(f"\n  final_answer (parsed): {json.dumps(m.get('final_answer_parsed'))[:600]}")

    # Available artifacts
    print(f"\n  Artifacts:")
    for fname in sorted(trial_dir.iterdir()):
        if fname.is_dir():
            ss = list(fname.iterdir())
            print(f"    {fname.name}/  ({len(ss)} files)")
        else:
            size = fname.stat().st_size
            print(f"    {fname.name}  ({size:,}b)")

    # Action log preview
    actions_path = trial_dir / "actions.jsonl"
    if actions_path.exists():
        actions = [json.loads(l) for l in actions_path.read_text().splitlines() if l.strip()]
        print(f"\n  Action log ({len(actions)} entries):")
        head = actions if verbose else actions[:5] + (
            [{"...": f"{len(actions) - 10} more"}] if len(actions) > 10 else []
        ) + actions[-5:] if len(actions) > 10 else actions
        for a in (actions if verbose else head):
            print(f"    {json.dumps(a)[:300]}")

    # Messages summary
    messages_path = trial_dir / "messages.jsonl"
    if messages_path.exists():
        msgs = [json.loads(l) for l in messages_path.read_text().splitlines() if l.strip()]
        print(f"\n  Messages ({len(msgs)} entries):")
        for msg in (msgs if verbose else msgs[:3] + msgs[-3:]):
            role = msg.get("role", "?")
            content = (msg.get("content") or "")[:200]
            print(f"    [{role}] {content}")


def _show_screenshot_path(trial_dir: Path) -> None:
    p = trial_dir / "screenshots" / "final.png"
    if p.exists():
        print(p)
    else:
        print(f"(no final.png in {trial_dir / 'screenshots'})", file=sys.stderr)
        sys.exit(1)


def _show_pane(trial_dir: Path) -> None:
    p = trial_dir / "tmux_pane.txt"
    if p.exists():
        print(p.read_text())
    else:
        print(f"(no tmux_pane.txt — this is probably not a DETM trial)",
              file=sys.stderr)
        sys.exit(1)


# ── CLI ──────────────────────────────────────────────────────────────────

def main() -> int:
    p = argparse.ArgumentParser(description="Inspect GUI-comparison benchmark trials")
    p.add_argument("--list", action="store_true",
                   help="List all run_ids that have results")
    p.add_argument("run_id", nargs="?")
    p.add_argument("family", nargs="?",
                   help="Family name (detm, playwright_mcp, agent_s)")
    p.add_argument("model", nargs="?",
                   help="Model name (e.g. openai/gpt-5.4 or openai_gpt-5.4)")
    p.add_argument("task_id", nargs="?",
                   help="Task id or short prefix (e.g. 14 or 14_xkcd_archive_100)")
    p.add_argument("--failures", action="store_true",
                   help="(compact mode) show only trials where judge_success != true")
    p.add_argument("--sort", choices=("duration", "score", "actions"),
                   help="(compact mode) sort order")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="(detail mode) show full action log + all messages")
    p.add_argument("--frame", action="store_true",
                   help="(detail mode) print just the final-screenshot path "
                        "so it can be Read into context")
    p.add_argument("--pane", action="store_true",
                   help="(detail mode) print the tmux pane text (DETM trials)")
    args = p.parse_args()

    if args.list:
        runs = _list_run_ids()
        if not runs:
            print("(no runs found in results/)")
            return 0
        for r in runs:
            n = len(_list_trials(r))
            print(f"  {r}  ({n} trials)")
        return 0

    if not args.run_id:
        p.error("provide a run_id (or --list)")

    # Trial detail mode if family/model/task supplied.
    if args.family and args.model and args.task_id:
        trial = _resolve_trial_dir(args.run_id, args.family, args.model, args.task_id)
        if not trial:
            print(f"No trial: run={args.run_id} family={args.family} "
                  f"model={args.model} task={args.task_id}", file=sys.stderr)
            return 1
        if args.frame:
            _show_screenshot_path(trial)
            return 0
        if args.pane:
            _show_pane(trial)
            return 0
        _print_trial_detail(trial, verbose=args.verbose)
        return 0

    # Compact mode for the run.
    _print_compact(
        args.run_id,
        only_failures=args.failures,
        sort_by=args.sort or "",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
