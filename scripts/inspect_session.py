#!/usr/bin/env python3
"""Inspect a live_ui session — turn-by-turn debug view with frame references.

Designed for nested debugging by an AI agent (Claude Code, etc.):
  1. Skim the trajectory in compact mode (cheap to ingest)
  2. Drill into a suspicious turn with verbose mode
  3. Pull a specific frame's path and Read it (one image into context, not all)

Usage:
    # List recent sessions (most recent first)
    python3 scripts/inspect_session.py --list

    # Compact one-line-per-turn trajectory — best for skimming long sessions
    python3 scripts/inspect_session.py <session_id> --compact

    # Full turn-by-turn debug view (action + result + frame for each turn)
    python3 scripts/inspect_session.py <session_id>

    # Slice to a turn range
    python3 scripts/inspect_session.py <session_id> --turns 3-8

    # Print a specific frame's path so Claude Code can `Read` it
    python3 scripts/inspect_session.py <session_id> --frame 5

    # Save frame to a known path
    python3 scripts/inspect_session.py <session_id> --frame 5 --save /tmp/frame.jpg

    # Use "latest" to inspect the most recent session
    python3 scripts/inspect_session.py latest --compact

Verifying DETM is alive (canonical 3-step recipe):
    1. python3 scripts/inspect_session.py latest --compact   # see what just ran
    2. python3 scripts/inspect_session.py latest --turns 1-3 # zoom in on early turns if anything looks wrong
    3. python3 scripts/inspect_session.py latest --frame 0 ; Read that path  # ingest the actual screen the model saw
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

DATA_DIR = Path(os.environ.get("ACU_DATA_DIR", os.path.expanduser("~/.agentic-computer-use")))
SESSIONS_DIR = DATA_DIR / "live_sessions"


def _safe_load_events(path: Path) -> list[dict]:
    """Read a JSONL file, skipping malformed lines.

    The daemon can die mid-write (OOM kill, power loss), leaving a partial trailing
    line. Without per-line tolerance, the inspector crashes for the one session you
    most want to inspect.
    """
    out: list[dict] = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return out


def list_sessions(limit: int = 20) -> None:
    if not SESSIONS_DIR.exists():
        print("No sessions directory found.")
        return

    sessions = []
    for d in SESSIONS_DIR.iterdir():
        if not d.is_dir():
            continue
        events_path = d / "events.jsonl"
        if not events_path.exists():
            continue
        mtime = events_path.stat().st_mtime
        events = _safe_load_events(events_path)
        first = events[0] if events else {}
        last = events[-1] if events else {}
        frames_dir = d / "frames"
        frame_count = len(list(frames_dir.glob("*.jpg"))) if frames_dir.exists() else 0

        sessions.append({
            "id": d.name,
            "mtime": mtime,
            "instruction": first.get("instruction", "")[:80],
            "status": last.get("type", "?"),
            "success": last.get("success", None),
            "frames": frame_count,
        })

    sessions.sort(key=lambda s: s["mtime"], reverse=True)

    print(f"{'SESSION ID':<40} {'WHEN':<20} {'STATUS':<10} {'FRAMES':>6}  INSTRUCTION")
    print("-" * 120)
    for s in sessions[:limit]:
        when = datetime.fromtimestamp(s["mtime"]).strftime("%Y-%m-%d %H:%M")
        status = s["status"]
        if s["success"] is True:
            status = "success"
        elif s["success"] is False and s["status"] == "done":
            status = "failed"
        print(f"{s['id']:<40} {when:<20} {status:<10} {s['frames']:>6}  {s['instruction']}")


def resolve_session_id(sid: str) -> str:
    if sid == "latest":
        if not SESSIONS_DIR.exists():
            print("No sessions found.", file=sys.stderr)
            sys.exit(1)
        candidates = [d for d in SESSIONS_DIR.iterdir()
                      if d.is_dir() and (d / "events.jsonl").exists()]
        if not candidates:
            print("No sessions found.", file=sys.stderr)
            sys.exit(1)
        latest = max(candidates, key=lambda d: (d / "events.jsonl").stat().st_mtime)
        return latest.name
    # Allow prefix match
    if not (SESSIONS_DIR / sid).exists():
        matches = [d.name for d in SESSIONS_DIR.iterdir() if d.name.startswith(sid)]
        if len(matches) == 1:
            return matches[0]
        elif len(matches) > 1:
            print(f"Ambiguous prefix '{sid}': {matches[:5]}", file=sys.stderr)
            sys.exit(1)
        else:
            print(f"Session not found: {sid}", file=sys.stderr)
            sys.exit(1)
    return sid


def compact_trajectory(sid: str) -> None:
    """One-line-per-turn trajectory dump. Designed for an AI agent (or human) to
    skim a long session quickly and decide which turns/frames to ingest in detail."""
    session_dir = SESSIONS_DIR / sid
    events_path = session_dir / "events.jsonl"
    if not events_path.exists():
        print(f"No events.jsonl found for session {sid}", file=sys.stderr)
        sys.exit(1)
    events = _safe_load_events(events_path)
    if not events:
        return
    start_ts = events[0].get("ts", 0)
    print(f"SESSION: {sid}  (frames at {session_dir / 'frames'})")
    instr = next((e for e in events if e["type"] == "instruction"), None)
    if instr:
        print(f"  Instruction: {instr.get('instruction', '')[:200]}")
    print()
    turn_n = 0
    last_frame_n = None
    for ev in events:
        rel = f"+{ev['ts'] - start_ts:5.1f}s"
        t = ev["type"]
        if t == "frame":
            last_frame_n = ev.get("n")
        elif t == "tool_call":
            turn_n += 1
            name = ev.get("name", "?")
            args = ev.get("args", {})
            if name == "bash":
                cmd = args.get("command", "")
                if len(cmd) > 140:
                    cmd = cmd[:137] + "..."
                frame_tag = f" [f#{last_frame_n}]" if last_frame_n is not None else ""
                print(f"  {rel} T{turn_n:>2}  bash{frame_tag}: {cmd}")
            elif name in ("done", "partial", "failed", "escalate"):
                summ = (args.get("summary") or args.get("reason") or "")[:120]
                print(f"  {rel} T{turn_n:>2}  {name}: {summ}")
            else:
                print(f"  {rel} T{turn_n:>2}  {name}({json.dumps(args)[:120]})")
        elif t == "tool_response" and ev.get("name") == "bash":
            result = ev.get("result", "")
            ec = next((l.split(":", 1)[1].strip() for l in result.split("\n") if l.startswith("exit_code:")), "?")
            so = next((l.split(":", 1)[1].strip() for l in result.split("\n") if l.startswith("stdout:")), "")
            se = next((l.split(":", 1)[1].strip() for l in result.split("\n") if l.startswith("stderr:")), "")
            extras = []
            if so and so != "(empty)":
                extras.append(f"out={so[:60]}")
            if se and se != "(empty)":
                extras.append(f"err={se[:60]}")
            extra_str = " " + " ".join(extras) if extras else ""
            if ec != "0" or extras:
                print(f"            -> exit={ec}{extra_str}")
        elif t in ("done", "escalate", "error"):
            summ = ev.get("summary") or ev.get("reason") or ev.get("message") or ""
            ok = "SUCCESS" if (t == "done" and ev.get("success")) else t.upper()
            print(f"  {rel}      [{ok}] {summ[:160]}")


def inspect_session(sid: str, turn_range: str | None = None) -> None:
    session_dir = SESSIONS_DIR / sid
    events_path = session_dir / "events.jsonl"
    if not events_path.exists():
        print(f"No events.jsonl found for session {sid}", file=sys.stderr)
        sys.exit(1)

    events = _safe_load_events(events_path)

    # Group events into turns: each turn = [model_text?, tool_call, grounding*, tool_response?, frame?]
    # Turn boundary = a new tool_call arrives (not model_text — bash backend rarely emits text).
    # Pre-tool_call events (frame, model_text) attach to the *next* turn.
    turns: list[dict] = []
    current_turn: dict = {}
    pending: dict = {}  # events that arrived before any tool_call yet
    start_ts = events[0]["ts"] if events else 0

    def _flush_pending_into(turn: dict) -> None:
        for k in ("thought", "thought_ts", "pre_frame", "pre_frame_ts"):
            if k in pending:
                turn[k] = pending[k]
        pending.clear()

    for ev in events:
        t = ev["type"]
        rel = f"+{ev['ts'] - start_ts:.1f}s" if "ts" in ev else ""

        if t == "instruction":
            print(f"SESSION: {sid}")
            print(f"  Instruction: {ev.get('instruction', '')}")
            if ev.get("context"):
                print(f"  Context: {ev['context'][:200]}")
            print(f"  Timeout: {ev.get('timeout', '?')}s")
            print(f"  Started: {datetime.fromtimestamp(ev['ts']).strftime('%Y-%m-%d %H:%M:%S')}")
            print()
            continue

        if t == "model_text":
            pending["thought"] = ev.get("text", "")
            pending["thought_ts"] = rel

        elif t == "tool_call":
            # Boundary: push current turn (if any), start fresh, attach pending pre-events.
            if current_turn:
                turns.append(current_turn)
            current_turn = {"tool_call": ev, "tool_call_ts": rel}
            _flush_pending_into(current_turn)

        elif t == "grounding":
            current_turn.setdefault("groundings", []).append(ev)

        elif t == "tool_response":
            current_turn["tool_response"] = ev
            current_turn["tool_response_ts"] = rel

        elif t == "frame":
            # Frame after a tool_call attaches to the current turn (post-action screenshot).
            # Frame before any tool_call (e.g. the initial capture) becomes pre_frame for next turn.
            if current_turn.get("tool_call"):
                current_turn["frame"] = ev.get("n")
                current_turn["frame_ts"] = rel
            else:
                pending["pre_frame"] = ev.get("n")
                pending["pre_frame_ts"] = rel

        elif t in ("done", "escalate", "error"):
            current_turn["terminal"] = ev
            current_turn["terminal_ts"] = rel
            turns.append(current_turn)
            current_turn = {}

    if current_turn:
        turns.append(current_turn)

    # Apply turn range filter
    start_turn, end_turn = 0, len(turns)
    if turn_range:
        parts = turn_range.split("-")
        start_turn = int(parts[0]) - 1
        end_turn = int(parts[1]) if len(parts) > 1 else start_turn + 1

    print(f"Total turns: {len(turns)}")
    print("=" * 100)

    for i, turn in enumerate(turns[start_turn:end_turn], start=start_turn + 1):
        print(f"\n--- Turn {i} ---")

        # Pre-action screenshot (if this turn inherited the initial frame from the harness).
        if turn.get("pre_frame") is not None:
            ts = turn.get("pre_frame_ts", "")
            pf = SESSIONS_DIR / sid / "frames" / f"{turn['pre_frame']:05d}.jpg"
            print(f"  PRE-FRAME ({ts}): #{turn['pre_frame']}  ->  {pf}")

        if turn.get("thought"):
            ts = turn.get("thought_ts", "")
            # Truncate very long thoughts but show key info
            thought = turn["thought"]
            if len(thought) > 500:
                thought = thought[:500] + f"... [{len(thought)} chars total]"
            print(f"  THOUGHT ({ts}): {thought}")

        if turn.get("tool_call"):
            tc = turn["tool_call"]
            ts = turn.get("tool_call_ts", "")
            name = tc.get("name", "?")
            args = tc.get("args", {})
            # Format args concisely. Bash backend's `bash` is the production case;
            # the others are the supervised backend's structured tools (kept for
            # backward-compat with old sessions).
            if name == "bash":
                cmd = args.get("command", "")
                cmd_to = args.get("timeout", 10)
                # One-line trailing-trim. Long heredocs and multi-step pipelines stay readable.
                if len(cmd) > 240:
                    cmd = cmd[:237] + "..."
                print(f"  ACTION ({ts}): bash (timeout={cmd_to}s)")
                print(f"    $ {cmd}")
            elif name == "screenshot":
                print(f"  ACTION ({ts}): screenshot")
            elif name == "move_to":
                args_str = f'target={args.get("target", "")!r}'
                print(f"  ACTION ({ts}): {name}({args_str})")
            elif name in ("click", "double_click", "right_click"):
                args_str = ""
                if name == "click" and args.get("button", "left") != "left":
                    args_str = f"button={args['button']}"
                print(f"  ACTION ({ts}): {name}({args_str})")
            elif name == "scroll":
                args_str = f"dir={args.get('direction')}, amt={args.get('amount', 3)}"
                print(f"  ACTION ({ts}): {name}({args_str})")
            elif name == "type_text":
                args_str = f'"{args.get("text", "")}"'
                print(f"  ACTION ({ts}): {name}({args_str})")
            elif name == "key_press":
                print(f"  ACTION ({ts}): {name}({args.get('key', '')})")
            elif name == "drag":
                args_str = f'from={args.get("from_target", "")!r}, to={args.get("to_target", "")!r}'
                print(f"  ACTION ({ts}): {name}({args_str})")
            elif name == "done":
                summ = args.get("summary", "")
                if len(summ) > 200:
                    summ = summ[:200] + "..."
                # Bash backend's done has no `success` arg; success implied by terminal type.
                if "success" in args:
                    print(f"  ACTION ({ts}): done(success={args.get('success')}, summary={summ!r})")
                else:
                    print(f"  ACTION ({ts}): done(summary={summ!r})")
            elif name in ("partial", "failed"):
                summ = args.get("summary", "")
                rest = args.get("remaining") or args.get("tried") or ""
                print(f"  ACTION ({ts}): {name}(summary={summ!r}, {('remaining' if name=='partial' else 'tried')}={rest!r})")
            elif name == "escalate":
                args_str = f"reason={args.get('reason', '')!r}"
                print(f"  ACTION ({ts}): {name}({args_str})")
            else:
                args_str = json.dumps(args)[:100]
                print(f"  ACTION ({ts}): {name}({args_str})")

        # Show grounding events (UI-TARS predictions) between action and result
        for g in turn.get("groundings", []):
            g_rel = f"+{g['ts'] - start_ts:.1f}s" if "ts" in g else ""
            model_short = g.get("model", "ui-tars").split("/")[-1]
            rnd = g.get("round", 0)
            if g.get("error"):
                print(f"  [{model_short}] GROUNDING ({g_rel}): FAILED round={rnd} — {g['error']}")
            elif g.get("converged"):
                print(f"  [{model_short}] GROUNDING ({g_rel}): CONVERGED round={rnd} at ({g.get('x')}, {g.get('y')})")
            else:
                target = g.get("target", "")
                if len(target) > 80:
                    target = target[:80] + "..."
                print(f"  [{model_short}] GROUNDING ({g_rel}): round={rnd} target={target!r} -> ({g.get('x')}, {g.get('y')})")

        if turn.get("tool_response"):
            tr = turn["tool_response"]
            ts = turn.get("tool_response_ts", "")
            result = tr.get("result", "")
            tool_name = tr.get("name", "")
            # Bash backend result is a multi-line "exit_code: N\nstdout: ...\nstderr: ...".
            # Render it on one line when stdout/stderr are empty (the common xdotool case).
            if tool_name == "bash" and result.startswith("exit_code:"):
                exit_code = ""
                stdout = ""
                stderr = ""
                for line in result.split("\n"):
                    if line.startswith("exit_code:"):
                        exit_code = line.split(":", 1)[1].strip()
                    elif line.startswith("stdout:"):
                        stdout = line.split(":", 1)[1].strip()
                    elif line.startswith("stderr:"):
                        stderr = line.split(":", 1)[1].strip()
                if stdout in ("", "(empty)") and stderr in ("", "(empty)"):
                    print(f"  RESULT ({ts}): exit={exit_code}")
                else:
                    print(f"  RESULT ({ts}): exit={exit_code}")
                    if stdout and stdout != "(empty)":
                        print(f"    stdout: {stdout[:240]}")
                    if stderr and stderr != "(empty)":
                        print(f"    stderr: {stderr[:240]}")
            else:
                print(f"  RESULT ({ts}): {result}")

        if turn.get("frame") is not None:
            ts = turn.get("frame_ts", "")
            frame_path = session_dir / "frames" / f"{turn['frame']:05d}.jpg"
            print(f"  FRAME ({ts}): #{turn['frame']}  ->  {frame_path}")

        if turn.get("terminal"):
            te = turn["terminal"]
            ts = turn.get("terminal_ts", "")
            if te["type"] == "done":
                ok = "SUCCESS" if te.get("success") else "FAILED"
                print(f"  {ok} ({ts}): {te.get('summary', '')}")
            elif te["type"] == "escalate":
                print(f"  ESCALATED ({ts}): {te.get('reason', '')}")
            elif te["type"] == "error":
                print(f"  ERROR ({ts}): {te.get('message', '')}")

    # Summary
    print("\n" + "=" * 100)
    total_frames = sum(1 for ev in events if ev["type"] == "frame")
    total_actions = sum(1 for ev in events if ev["type"] == "tool_call" and ev.get("name") not in ("done", "escalate"))
    grounding_evs = [ev for ev in events if ev["type"] == "grounding"]
    total_groundings = len(grounding_evs)
    converged_groundings = sum(1 for ev in grounding_evs if ev.get("converged"))
    failed_groundings = sum(1 for ev in grounding_evs if ev.get("error"))
    duration = events[-1]["ts"] - events[0]["ts"] if len(events) > 1 else 0
    terminal = [ev for ev in events if ev["type"] in ("done", "escalate", "error")]
    status = terminal[-1] if terminal else None
    grounding_str = f" | Groundings: {total_groundings} ({converged_groundings} converged, {failed_groundings} failed)" if total_groundings else ""
    print(f"Duration: {duration:.1f}s | Actions: {total_actions} | Frames: {total_frames}{grounding_str}")
    if status:
        if status["type"] == "done":
            print(f"Outcome: {'SUCCESS' if status.get('success') else 'FAILED'} — {status.get('summary', '')}")
        else:
            print(f"Outcome: {status['type'].upper()} — {status.get('reason', status.get('message', ''))}")


def show_frame(sid: str, frame_n: int, save_path: str | None = None) -> None:
    frame_path = SESSIONS_DIR / sid / "frames" / f"{frame_n:05d}.jpg"
    if not frame_path.exists():
        print(f"Frame {frame_n} not found at {frame_path}", file=sys.stderr)
        sys.exit(1)

    if save_path:
        shutil.copy2(frame_path, save_path)
        print(f"Frame {frame_n} saved to {save_path}")
    else:
        # Just print the path so Claude Code can use Read to view it
        print(f"Frame path: {frame_path}")
        print(f"Size: {frame_path.stat().st_size} bytes")
        print(f"Use: Read tool on {frame_path} to view")


def main():
    parser = argparse.ArgumentParser(description="Inspect a live_ui session")
    parser.add_argument("session_id", nargs="?", help="Session ID or 'latest'")
    parser.add_argument("--list", action="store_true", help="List recent sessions")
    parser.add_argument("--turns", help="Turn range to show, e.g. '3-8' or '5'")
    parser.add_argument("--compact", action="store_true",
                        help="One-line-per-turn trajectory (skim mode for AI agents)")
    parser.add_argument("--frame", type=int, help="Show a specific frame")
    parser.add_argument("--save", help="Save frame to this path (with --frame)")
    parser.add_argument("--limit", type=int, default=20, help="Max sessions to list")
    args = parser.parse_args()

    if args.list:
        list_sessions(args.limit)
        return

    if not args.session_id:
        parser.print_help()
        return

    sid = resolve_session_id(args.session_id)

    if args.frame is not None:
        show_frame(sid, args.frame, args.save)
    elif args.compact:
        compact_trajectory(sid)
    else:
        inspect_session(sid, args.turns)


if __name__ == "__main__":
    main()
