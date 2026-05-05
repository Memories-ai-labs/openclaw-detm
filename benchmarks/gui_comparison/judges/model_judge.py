"""Model-as-judge: claude-haiku-4-5 verdict on one trial.

Uses OpenRouter (openrouter.ai) for both authentication and routing —
the env we run in has OPENROUTER_API_KEY but no native ANTHROPIC_API_KEY.
The judge model defaults to `anthropic/claude-haiku-4-5` over OpenRouter,
but can be overridden via BENCH_JUDGE_MODEL.

Inputs: TaskSpec, the agent's final text answer, and the path to the
final-state screenshot (optional but strongly recommended — the rubric
verifies what's on screen, not just what the agent claimed).

Output: {success: bool, partial_credit: float in [0,1], reason: str}

The judge is deliberately conservative: if the screenshot doesn't show
evidence the agent reached the target page, we award 0 even if the
answer text looks plausible (this is how we defend against models
hallucinating from their training data).
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from typing import Optional

import httpx

JUDGE_MODEL = os.environ.get("BENCH_JUDGE_MODEL", "anthropic/claude-haiku-4-5")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

_SYSTEM = """You are a strict, conservative judge for a GUI agent benchmark.

You will be given:
1. A task description an agent was asked to perform.
2. A free-text "rubric" describing what success looks like.
3. The agent's final answer (text, possibly containing JSON).
4. A screenshot of the final state of the agent's browser.

Your job: decide whether the agent succeeded, with a partial credit score
between 0.0 and 1.0.

Rules:
- The screenshot is the ground truth. If the agent claims to have reached
  a page but the screenshot doesn't show that page, score is at most 0.0.
- The answer text alone is not enough. Require the screenshot to corroborate.
- Prefer the partial-credit guidance the rubric provides (e.g., "0.2 per
  correct entry"). Apply it literally.
- Be strict but fair: if the rubric says "first 5 employees" and the agent
  reported 5 plausible names + titles AND the screenshot shows those names,
  award 1.0.
- Flag obvious cheating (the agent claims facts but the screenshot is on
  about:blank or a search results page that doesn't show the answer).
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

## Screenshot of agent's final state
(see attached image, if present)

Return JSON: {{"success": bool, "partial_credit": float, "reason": str}}
"""


def _build_message(task, final_answer: str, screenshot_path: Optional[Path]) -> dict:
    text_part = {
        "type": "text",
        "text": _USER_TEMPLATE.format(
            task_title=task.title,
            task_prompt=task.prompt,
            judge_rubric=task.judge_rubric,
            final_answer=final_answer or "(empty)",
        ),
    }
    content: list = [text_part]

    if screenshot_path and screenshot_path.exists():
        try:
            data = screenshot_path.read_bytes()
            b64 = base64.standard_b64encode(data).decode("ascii")
            ext = screenshot_path.suffix.lstrip(".").lower()
            if ext == "jpg":
                ext = "jpeg"
            mt = {"png": "image/png", "jpeg": "image/jpeg"}.get(ext, "image/png")
            # OpenAI-compat (OpenRouter) image format:
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:{mt};base64,{b64}"},
            })
        except Exception as e:
            text_part["text"] += f"\n\n(screenshot read failed: {e})"
    else:
        text_part["text"] += "\n\n(no screenshot was captured)"

    return {"role": "user", "content": content}


def judge(
    task,
    final_answer: str,
    final_screenshot_path: Optional[Path] = None,
    api_key: Optional[str] = None,
    model: Optional[str] = None,
) -> dict:
    """Returns dict with keys: success (bool), partial_credit (float),
    reason (str). On API error or unparseable response, returns success=False
    with the error in reason."""
    api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        return {
            "success": False,
            "partial_credit": 0.0,
            "reason": "OPENROUTER_API_KEY not set — judge cannot run",
        }
    use_model = model or JUDGE_MODEL

    messages = [
        {"role": "system", "content": _SYSTEM},
        _build_message(task, final_answer, final_screenshot_path),
    ]
    try:
        with httpx.Client(timeout=60.0) as c:
            resp = c.post(
                OPENROUTER_URL,
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    # OpenRouter uses these for billing-attribution rankings.
                    # Set to a neutral local identifier — they don't need to
                    # resolve to a real URL.
                    "HTTP-Referer": "https://detm.local/bench",
                    "X-Title": "DETM-bench-judge",
                },
                json={
                    "model": use_model,
                    "messages": messages,
                    "max_tokens": 1024,
                },
            )
        if resp.status_code != 200:
            return {
                "success": False,
                "partial_credit": 0.0,
                "reason": f"judge HTTP {resp.status_code}: {resp.text[:300]}",
            }
        data = resp.json()
        choices = data.get("choices") or []
        if not choices:
            return {
                "success": False, "partial_credit": 0.0,
                "reason": f"judge: no choices: {data}",
            }
        raw = (choices[0].get("message") or {}).get("content") or ""
    except Exception as e:
        return {
            "success": False, "partial_credit": 0.0,
            "reason": f"judge HTTP call failed: {e}",
        }

    # Tolerate accidental code fences in the judge response.
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        # Strip an opening fence line and any trailing fence.
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
            "success": False,
            "partial_credit": 0.0,
            "reason": f"judge response unparseable ({e}): {raw[:300]}",
        }
