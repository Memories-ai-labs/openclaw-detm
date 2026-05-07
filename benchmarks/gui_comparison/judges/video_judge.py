"""Video-native judge: gemini-3.1-pro-preview verdict on a trial's recording.

Sibling of `model_judge.py`. Where the log judge sees a single final
screenshot + action log + final answer, the video judge sees the full
trajectory as motion video, can compare visual states across time, and
catches things the log judge misses (e.g., "agent navigated to the
wrong page first then recovered" — visible in video, opaque in logs).

Currently OPT-IN. Run via `--video-judge` flag on the orchestrator OR
explicitly via `python -m gui_comparison.judges.video_judge ...`.

Two paths for sending the video to the judge:
  - Path A (preferred): inline base64 video_url block. Gemini accepts
    this natively via OpenRouter; ~1 hour of video supported.
  - Path B (fallback): extract N evenly-spaced JPEG frames via ffmpeg
    and send as image_url blocks. Lossy but works on any vision model.

Returns the same shape as the log judge:
  {success: bool, partial_credit: float, reason: str}
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

import httpx

JUDGE_MODEL = os.environ.get(
    "BENCH_VIDEO_JUDGE_MODEL", "google/gemini-3.1-pro-preview"
)
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Frame-extraction parameters (path B fallback).
N_FRAMES = int(os.environ.get("BENCH_VIDEO_JUDGE_FRAMES", "12"))
# If video is small enough we can send it inline; otherwise fall back to frames.
MAX_INLINE_VIDEO_BYTES = int(
    os.environ.get("BENCH_VIDEO_JUDGE_MAX_INLINE_BYTES", str(50 * 1024 * 1024))
)


_SYSTEM = """You are a strict, conservative judge for a GUI agent benchmark.

You will be given:
1. A task description and rubric.
2. The agent's final answer text.
3. A SCREEN RECORDING (or a sequence of frames extracted from it) of
   what the agent did in chronological order.

Your job: decide whether the agent succeeded, with a partial credit
score between 0.0 and 1.0.

Rules:
- The video is the ground truth. Trust what you SEE the agent doing.
- The answer text alone is not enough. Require the video to corroborate.
- Apply the rubric's partial-credit guidance literally.
- If the rubric mentions a specific navigation requirement (e.g., "must
  visit the archive before reaching the comic"), VERIFY that from the
  video — you can see the page transitions.
- Flag obvious cheating (the agent claims facts but the video shows
  they never reached the relevant page).
- The video may be silent / show only screen content; that's fine.
- Return ONLY a JSON object, no markdown fences, no commentary.
"""

_USER_TEMPLATE = """## Task
{task_title}

## Prompt the agent received
{task_prompt}

## Rubric (your judging criteria)
{judge_rubric}

## Agent's final answer
```
{final_answer}
```

## Recording / frames
(See attached video below. Frames are in chronological order if shown
as image sequence.)

Return JSON: {{"success": bool, "partial_credit": float, "reason": str}}
"""


# ── Frame extraction (path B fallback) ────────────────────────────────────

def _video_duration(path: Path) -> Optional[float]:
    """Return video duration in seconds, or None if ffprobe fails."""
    if not shutil.which("ffprobe"):
        return None
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(path)],
            timeout=10,
        )
        return float(out.decode().strip())
    except Exception:
        return None


def _extract_frames(video_path: Path, n_frames: int = N_FRAMES) -> list[bytes]:
    """Extract N evenly-spaced JPEG frames from the video. Returns a list
    of JPEG bytes in chronological order. Empty list if extraction fails."""
    if not shutil.which("ffmpeg"):
        return []
    duration = _video_duration(video_path)
    if not duration or duration <= 0:
        return []
    out_frames: list[bytes] = []
    with tempfile.TemporaryDirectory(prefix="bench_frames_") as td:
        td_path = Path(td)
        for i in range(n_frames):
            # Pick a timestamp just inside each chunk's middle.
            t = (i + 0.5) * duration / n_frames
            out_jpg = td_path / f"frame_{i:04d}.jpg"
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-loglevel", "error", "-nostdin",
                     "-ss", str(t), "-i", str(video_path),
                     "-frames:v", "1", "-vf", "scale=960:-1",
                     "-q:v", "5", str(out_jpg)],
                    timeout=15,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                if out_jpg.exists():
                    out_frames.append(out_jpg.read_bytes())
            except Exception:
                continue
    return out_frames


# ── Message construction ─────────────────────────────────────────────────

def _build_message_with_video(task, final_answer: str, video_path: Path) -> dict:
    """Path A: inline video_url block with the full MP4 base64'd."""
    data = video_path.read_bytes()
    b64 = base64.standard_b64encode(data).decode("ascii")
    return {
        "role": "user",
        "content": [
            {
                "type": "text",
                "text": _USER_TEMPLATE.format(
                    task_title=task.title,
                    task_prompt=task.prompt,
                    judge_rubric=task.judge_rubric,
                    final_answer=final_answer or "(empty)",
                ),
            },
            {
                "type": "video_url",
                "video_url": {"url": f"data:video/mp4;base64,{b64}"},
            },
        ],
    }


def _build_message_with_frames(task, final_answer: str,
                                frames: list[bytes]) -> dict:
    """Path B: a sequence of image_url blocks."""
    text = _USER_TEMPLATE.format(
        task_title=task.title,
        task_prompt=task.prompt,
        judge_rubric=task.judge_rubric,
        final_answer=final_answer or "(empty)",
    )
    text += f"\n\n(showing {len(frames)} frames sampled evenly from the recording)"
    content: list = [{"type": "text", "text": text}]
    for jpg in frames:
        b64 = base64.standard_b64encode(jpg).decode("ascii")
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
        })
    return {"role": "user", "content": content}


# ── Public API ───────────────────────────────────────────────────────────

def _post_judge(messages: list[dict], api_key: str, model: str) -> dict:
    """Send the request and parse the response. Returns the verdict dict."""
    try:
        with httpx.Client(timeout=120.0) as c:
            resp = c.post(
                OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://detm.local/bench",
                    "X-Title": "DETM-bench-video-judge",
                },
                json={
                    "model": model,
                    "messages": messages,
                    # Bumped from 1024 — Gemini's video reasoning often
                    # produces a long preamble before the JSON verdict.
                    # 1024 was getting cut off mid-string and the parser
                    # saw "unparseable" -> false fallback to frames.
                    "max_tokens": 4096,
                },
            )
        if resp.status_code != 200:
            return {
                "success": False, "partial_credit": 0.0,
                "reason": f"video judge HTTP {resp.status_code}: {resp.text[:300]}",
            }
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            return {
                "success": False, "partial_credit": 0.0,
                "reason": f"video judge: no choices: {data}",
            }
        raw = (choices[0].get("message") or {}).get("content") or ""
    except Exception as e:
        return {
            "success": False, "partial_credit": 0.0,
            "reason": f"video judge HTTP call failed: {e}",
        }

    cleaned = raw.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines)
    try:
        parsed = json.loads(cleaned)
        return {
            "success": bool(parsed.get("success")),
            "partial_credit": float(parsed.get("partial_credit", 0.0)),
            "reason": str(parsed.get("reason", "")),
        }
    except Exception as e:
        return {
            "success": False, "partial_credit": 0.0,
            "reason": f"video judge response unparseable ({e}): {raw[:300]}",
        }


def judge_video(
    task,
    final_answer: str,
    recording_path: Optional[Path] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
    force_frames: bool = False,
) -> dict:
    """Returns dict with keys: success (bool), partial_credit (float),
    reason (str), mode (str). On API error or unparseable response,
    returns success=False with the error in reason.

    `recording_path`: path to recording.mp4 from a benchmark trial. If
    missing/empty, returns an error verdict.

    `force_frames`: if True, always extract frames instead of trying
    inline video. Useful when path A (video_url) is broken on a
    particular model/provider.
    """
    api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return {
            "success": False, "partial_credit": 0.0,
            "reason": "OPENROUTER_API_KEY not set — video judge cannot run",
            "mode": "error",
        }
    if not recording_path or not recording_path.exists():
        return {
            "success": False, "partial_credit": 0.0,
            "reason": f"no recording at {recording_path} — video judge cannot run",
            "mode": "error",
        }

    use_model = model or JUDGE_MODEL
    size = recording_path.stat().st_size

    # Path A: inline video. Verified that OpenRouter+Gemini accepts the
    # `{"type": "video_url", "video_url": {"url": "data:video/mp4;base64,..."}}`
    # block format. Skip A only when explicitly forced or video too big
    # to base64 inline (would balloon the request to >50MB).
    inline_attempted = False
    inline_reason: str = ""
    if not force_frames and size <= MAX_INLINE_VIDEO_BYTES:
        inline_attempted = True
        try:
            msg = _build_message_with_video(task, final_answer, recording_path)
            messages = [{"role": "system", "content": _SYSTEM}, msg]
            verdict = _post_judge(messages, api_key, use_model)
            reason_str = verdict.get("reason", "") or ""
            # Fall through ONLY for transport / parse failures, not for
            # "verdict says false" (that's a real verdict).
            transport_failure = (
                reason_str.startswith("video judge HTTP")
                or "unparseable" in reason_str
                or "no choices" in reason_str
                or "HTTP call failed" in reason_str
            )
            if not transport_failure:
                verdict["mode"] = "inline_video"
                return verdict
            inline_reason = reason_str
        except Exception as e:
            inline_reason = f"inline path exception: {e}"

    # Path B: extracted frames. Only reach here on inline transport
    # failure, force_frames=True, or video too big to inline.
    frames = _extract_frames(recording_path)
    if not frames:
        return {
            "success": False, "partial_credit": 0.0,
            "reason": (
                f"ffmpeg frame extraction failed; "
                f"inline path: {inline_reason or 'not attempted'}"
            ),
            "mode": "error",
        }
    msg = _build_message_with_frames(task, final_answer, frames)
    messages = [{"role": "system", "content": _SYSTEM}, msg]
    verdict = _post_judge(messages, api_key, use_model)
    verdict["mode"] = f"frames({len(frames)})"
    if inline_attempted and inline_reason:
        verdict["inline_failure_reason"] = inline_reason
    return verdict


def main() -> int:
    """CLI: re-judge a single trial's recording.

    Usage:
        python -m gui_comparison.judges.video_judge \\
            --run RUN_ID --family detm --model openai/gpt-5.4 --task 14
    """
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--run", required=True)
    p.add_argument("--family", required=True)
    p.add_argument("--model", required=True)
    p.add_argument("--task", required=True)
    p.add_argument("--force-frames", action="store_true",
                   help="Skip inline-video path; extract frames instead")
    args = p.parse_args()

    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    # Load .env
    env_file = Path(__file__).resolve().parents[3] / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

    from gui_comparison.runners.base import RESULTS_DIR, load_task
    task = load_task(args.task) if "_" in args.task else None
    if not task:
        # Allow "14" prefix
        from gui_comparison.runners.base import load_all_tasks
        for t in load_all_tasks():
            if t.id.startswith(args.task + "_") or t.id == args.task:
                task = t
                break
    if not task:
        print(f"No task: {args.task}", file=sys.stderr)
        return 1

    slug = args.model.replace("/", "_").replace(":", "_")
    trial_dir = RESULTS_DIR / args.run / f"{args.family}__{slug}" / task.id
    if not trial_dir.exists():
        print(f"No trial dir: {trial_dir}", file=sys.stderr)
        return 1

    recording = trial_dir / "recording.mp4"
    final_answer = ""
    fa_path = trial_dir / "final_answer.txt"
    if fa_path.exists():
        final_answer = fa_path.read_text()

    print(f"Judging {trial_dir.relative_to(RESULTS_DIR)} (recording: {recording.exists()})...")
    verdict = judge_video(task, final_answer, recording, force_frames=args.force_frames)
    print(json.dumps(verdict, indent=2))

    out = trial_dir / "judge_video_verdict.json"
    out.write_text(json.dumps(verdict, indent=2))
    print(f"\nSaved → {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
