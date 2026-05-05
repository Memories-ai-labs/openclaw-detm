"""Aggregate per-trial metrics.json files into summary.csv.

Walks results/<run_id>/<family>__<model>/<task_id>/metrics.json and
emits one CSV row per trial. Also writes a per-(family, model) summary
with averages, and a tiny markdown table for the writeup.

Usage:
    python -m gui_comparison.analysis.aggregate <run_id>
    python -m gui_comparison.analysis.aggregate --all   # every run dir
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean

HERE = Path(__file__).resolve().parents[1]
RESULTS = HERE / "results"

FIELDS = [
    "run_id", "family", "model", "task_id",
    "duration_s", "n_tool_calls", "n_assistant_messages",
    "thinking_chars", "prompt_tokens", "completion_tokens",
    "termination_reason", "judge_success", "judge_partial_credit",
    "judge_reason",
]


def _walk_run(run_dir: Path) -> list[dict]:
    rows = []
    if not run_dir.exists():
        return rows
    for fam_dir in sorted(run_dir.iterdir()):
        if not fam_dir.is_dir():
            continue
        for task_dir in sorted(fam_dir.iterdir()):
            mp = task_dir / "metrics.json"
            if not mp.exists():
                continue
            try:
                m = json.loads(mp.read_text())
            except Exception:
                continue
            row = {f: m.get(f) for f in FIELDS}
            rows.append(row)
    return rows


def _write_csv(rows: list[dict], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _aggregate_by_model(rows: list[dict]) -> list[dict]:
    """One summary row per (family, model).

    Note on tokens: Family B (DETM) doesn't surface token counts (the
    OpenClaw side hides them). Aggregated `total_*_tokens` will be 0
    for that family — don't compare across families on this column."""
    groups = defaultdict(list)
    for r in rows:
        groups[(r["family"], r["model"])].append(r)
    out = []
    for (fam, mdl), trials in sorted(groups.items()):
        n = len(trials)
        n_ok = sum(1 for t in trials if t.get("judge_success"))
        n_no_json = sum(
            1 for t in trials
            if t.get("termination_reason") == "completed_no_json"
        )
        n_err = sum(
            1 for t in trials if t.get("termination_reason") == "error"
        )
        n_timeout = sum(
            1 for t in trials if t.get("termination_reason") == "timeout"
        )
        scores = [t.get("judge_partial_credit") or 0.0 for t in trials]
        durations = [t.get("duration_s") or 0.0 for t in trials]
        actions = [t.get("n_tool_calls") or 0 for t in trials]
        msgs = [t.get("n_assistant_messages") or 0 for t in trials]
        prompt_tok = sum(t.get("prompt_tokens") or 0 for t in trials)
        comp_tok = sum(t.get("completion_tokens") or 0 for t in trials)
        out.append({
            "family": fam, "model": mdl, "n_tasks": n,
            "n_success": n_ok, "success_rate": round(n_ok / max(n, 1), 3),
            "avg_score": round(mean(scores) if scores else 0.0, 3),
            "avg_duration_s": round(mean(durations) if durations else 0.0, 1),
            "avg_actions": round(mean(actions) if actions else 0.0, 1),
            "avg_messages": round(mean(msgs) if msgs else 0.0, 1),
            "n_no_json": n_no_json,
            "n_timeout": n_timeout,
            "n_error": n_err,
            "total_prompt_tokens": prompt_tok,
            "total_completion_tokens": comp_tok,
        })
    return out


def _markdown_table(by_model: list[dict]) -> str:
    headers = [
        "family", "model", "n_tasks", "n_success", "success_rate",
        "avg_score", "avg_duration_s", "avg_actions", "avg_messages",
        "n_no_json", "n_timeout", "n_error",
    ]
    lines = ["| " + " | ".join(headers) + " |"]
    lines.append("|" + "|".join(["---"] * len(headers)) + "|")
    for row in by_model:
        lines.append("| " + " | ".join(str(row.get(h, "")) for h in headers) + " |")
    return "\n".join(lines)


def aggregate(run_id: str) -> None:
    run_dir = RESULTS / run_id
    rows = _walk_run(run_dir)
    if not rows:
        print(f"No metrics found under {run_dir}")
        return
    csv_path = run_dir / "summary.csv"
    _write_csv(rows, csv_path)
    print(f"Wrote {csv_path}  ({len(rows)} trials)")

    by_model = _aggregate_by_model(rows)
    by_model_csv = run_dir / "summary_by_model.csv"
    if by_model:
        with by_model_csv.open("w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(by_model[0].keys()))
            w.writeheader()
            w.writerows(by_model)
        print(f"Wrote {by_model_csv}")

    md_path = run_dir / "summary.md"
    md = "# Run summary: " + run_id + "\n\n" + _markdown_table(by_model)
    md_path.write_text(md)
    print(f"Wrote {md_path}\n\n" + md)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("run_id", nargs="?")
    p.add_argument("--all", action="store_true")
    args = p.parse_args()
    if args.all:
        for run_dir in sorted(RESULTS.iterdir()):
            if run_dir.is_dir():
                print(f"\n=== {run_dir.name} ===")
                aggregate(run_dir.name)
    elif args.run_id:
        aggregate(args.run_id)
    else:
        p.error("provide a run_id or --all")
    return 0


if __name__ == "__main__":
    sys.exit(main())
