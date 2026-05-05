"""Model-as-judge: claude-haiku-4-5 verdict on one trial.

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

import anthropic

JUDGE_MODEL = os.environ.get("BENCH_JUDGE_MODEL", "claude-haiku-4-5")

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
(see attached image)

Return JSON: {{"success": bool, "partial_credit": float, "reason": str}}
"""


def judge(
    task,
    final_answer: str,
    final_screenshot_path: Optional[Path] = None,
    api_key: Optional[str] = None,
) -> dict:
    """Returns dict with keys: success (bool), partial_credit (float), reason (str).
    On API error or unparseable response, returns success=False with the error in reason.
    """
    client = anthropic.Anthropic(
        api_key=api_key or os.environ.get("ANTHROPIC_API_KEY"),
    )

    user_content: list = [
        {
            "type": "text",
            "text": _USER_TEMPLATE.format(
                task_title=task.title,
                task_prompt=task.prompt,
                judge_rubric=task.judge_rubric,
                final_answer=final_answer or "(empty)",
            ),
        }
    ]
    if final_screenshot_path and final_screenshot_path.exists():
        try:
            data = final_screenshot_path.read_bytes()
            b64 = base64.standard_b64encode(data).decode("ascii")
            ext = final_screenshot_path.suffix.lstrip(".").lower()
            if ext == "jpg":
                ext = "jpeg"
            mt = {"png": "image/png", "jpeg": "image/jpeg"}.get(ext, "image/png")
            user_content.append({
                "type": "image",
                "source": {"type": "base64", "media_type": mt, "data": b64},
            })
        except Exception as e:
            user_content[0]["text"] += f"\n\n(screenshot read failed: {e})"
    else:
        user_content[0]["text"] += "\n\n(no screenshot was captured)"

    try:
        resp = client.messages.create(
            model=JUDGE_MODEL,
            max_tokens=1024,
            system=_SYSTEM,
            messages=[{"role": "user", "content": user_content}],
        )
        raw = "".join(
            b.text for b in resp.content if getattr(b, "type", None) == "text"
        )
    except Exception as e:
        return {
            "success": False,
            "partial_credit": 0.0,
            "reason": f"judge API call failed: {e}",
        }

    # Parse — judge is instructed to return bare JSON.
    try:
        # Tolerate accidental fences.
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.strip("`")
            if cleaned.lower().startswith("json"):
                cleaned = cleaned[4:].strip()
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
