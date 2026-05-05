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
- [x] Common runner scaffolding (`runners/base.py`)
- [x] Model-as-judge (claude-haiku-4-5 via OpenRouter)
- [x] Family B runner — DETM via OpenClaw TUI (smoke: task 14, task 6)
- [x] Family A runner — Playwright MCP (smoke: task 14)
- [x] Family C runner — Agent S3 (smoke: task 14)
- [x] Aggregator — `analysis/aggregate.py` writes summary.csv,
       summary_by_model.csv, summary.md per run id
- [ ] Full sweep across all 15 tasks × all family/model combos
- [ ] Final writeup

## Setup once per machine

The runners assume a few external pieces are in place:

```bash
# 1. Python deps
pip install -e .                                       # repo's main pkg
pip install httpx pyyaml mcp                           # judge + MCP client
pip install -r benchmarks/baselines/agent-s/requirements.txt  # skip paddle

# 2. Playwright's chrome-for-testing build (for Family A)
npx @playwright/mcp@latest install-browser chrome-for-testing

# 3. A logged-in Chromium profile for tier-1 LinkedIn tasks (Family A).
#    Open a Chromium with the bench profile dir and log in once:
mkdir -p ~/.bench-chromium-profile
chromium --user-data-dir=$HOME/.bench-chromium-profile  &
# (then log in to LinkedIn manually — cookies persist across runs)

# 4. DETM daemon reachable at http://127.0.0.1:18790, configured for the
#    gui_agent model under test (bash backend, openai/gpt-5.4 by default).
#
# NOTE: The DETM runner now manages the OpenClaw tmux session itself —
# you DON'T need to pre-create `bench-oc`. Each task gets a fresh
# tmux session + a fresh `openclaw tui --session bench-<run>-<task>`
# so there is no cross-task context bleed across the sweep.
```

## Run

```bash
# All families × one model × one task
PYTHONPATH=benchmarks python benchmarks/gui_comparison/run_benchmark.py \
    --family detm --family playwright_mcp --family agent_s \
    --models openai/gpt-5.4 --tasks 14 --run-id my-smoke

# Full sweep
PYTHONPATH=benchmarks python benchmarks/gui_comparison/run_benchmark.py \
    --family detm --family playwright_mcp --family agent_s \
    --models openai/gpt-5.4 --tasks all --run-id full-2026-05-05

# Aggregate (PYTHONPATH=benchmarks needed for the package import)
PYTHONPATH=benchmarks python -m gui_comparison.analysis.aggregate full-2026-05-05
```

The orchestrator auto-loads the repo-root `.env` so `OPENROUTER_API_KEY`
gets picked up without manual `source`. Override the tmux session name
with `BENCH_TMUX_SESSION` if you want something other than the default
(`bench-oc`).
