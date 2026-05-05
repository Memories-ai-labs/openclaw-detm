# GUI agent comparison benchmark

Compares three families of GUI agents on a fixed set of 15 web-navigation
tasks.

## Families

| Family | Description | Variants |
|---|---|---|
| **A. Playwright MCP** | LLM via OpenRouter + Playwright MCP server (text-DOM-grounded) | gpt-5.4, gpt-5.4-mini, sonnet-4.6, opus-4.6, gemini-3-flash |
| **B. OpenClaw + DETM** | Local OpenClaw (gpt-5.4 main) driving DETM's vision-grounded gui_agent | gui_agent backend: gpt-5.4, gpt-5.4-mini, sonnet-4.6, opus-4.6, gemini-3-flash |
| **C. Agent S3** | Simular Agent S3 (planner + UI-TARS grounding), running directly on display :99 | default config |

## Tasks

15 tasks across 3 tiers:

- **Tier 1 — LinkedIn (5)** — needs a logged-in session; agent navigates and
  extracts profile/company/job data.
- **Tier 2 — other JS-heavy sites (5)** — Reddit, HN, GitHub, Stack Overflow,
  YouTube; mix of logged-out browsing and search.
- **Tier 3 — navigation on stable sites (5)** — Maps, Wikipedia, arXiv, xkcd,
  Scholar; deterministic answers, designed so the agent must actually
  navigate (not just answer from training data).

Each task is a single YAML file under `tasks/`. See `tasks/_schema.yaml` for
the schema.

## Metrics (per run)

- **success** — model-as-judge verdict (`true` if rubric satisfied), with a
  partial-credit float (0.0–1.0).
- **duration_s** — wall-clock time from agent start to final answer.
- **n_tool_calls** — number of GUI tool invocations.
- **n_assistant_messages** — model turns (proxy for plan/act cycles).
- **thinking_chars** — sum of reasoning_content / thought lengths if the
  family/model exposes them.
- **prompt_tokens / completion_tokens** — when the API exposes them.
- **termination_reason** — `completed | failed | timeout | max_actions | error`.

## Repeatability protocol

Each run reuses a single shared logged-in Firefox session running on display
`:99`. Between runs, the runner closes all tabs to reset visual state (the
LinkedIn cookie and other auth survives). The agent always starts from a
fresh `about:blank` (or homepage).

## One trial per task per family/model — that's the spec.

Results land in `results/<run_id>/<family>__<model>/<task_id>/`:
- `metrics.json` — aggregate numbers per the schema above
- `actions.jsonl` — every tool call with timestamps, args, response summary
- `messages.jsonl` — every model turn with role, content, reasoning if any
- `screenshots/step_NN.png` — frame captures
- `final_answer.json` — the JSON the agent returned
- `judge_verdict.json` — model-judge output

`results/<run_id>/summary.csv` aggregates one row per (family, model, task).

## Status

- [x] Branch + clear old infra
- [x] Define 15 task YAMLs
- [ ] Hand-verify task 1 in browser (sanity check)
- [ ] Common runner scaffolding
- [ ] Model-as-judge
- [ ] Family B runner (DETM via OpenClaw TUI)
- [ ] Smoke run on tasks 1+6
- [ ] Family A runner (Playwright MCP)
- [ ] Scope Family C (Agent S3 already cloned at `../baselines/agent-s/`)
- [ ] Full sweep
- [ ] Aggregate + writeup
