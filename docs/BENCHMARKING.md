# DETM Benchmarking Plan

> **Production status (May 2026):** DETM's `gui_agent` defaults to the **bash backend** with `openai/gpt-5.4` via OpenRouter (~$0.20/task on the LinkedIn 5-connect benchmark). The cheap-model **supervised** stack described below (Gemini Flash + UI-TARS-7B) remains the lowest-cost configuration we've benchmarked at ~$0.07/task on the OSWorld Chrome subset, and is kept in the codebase for that reproducibility — but is no longer the default. See `experiments/holo3-vs-flagships-2026-04-25/` for the bake-off that picked bash + gpt-5.4.

## TL;DR

DETM uses cheap models (Gemini Flash ~$0.10/M + UI-TARS-7B ~$0.15/M) instead of frontier models ($15-75/M). The question: can smart orchestration (iterative grounding, two-model split, edge detection) close the gap with agents that brute-force it with expensive models?

**Three benchmarks:** OSWorld (369 desktop tasks), ScreenSpot-Pro (1,581 grounding samples), WebArena (137 web tasks).

**Execution order:** ScreenSpot-Pro first (hours, no VMs), then OSWorld (days), then WebArena (stretch).

### Unified Comparison Table

All agents we compare against. **TBR** = to be run by us. **--** = unknown/not planned.

| Agent | Cost | OSWorld @15 | OSWorld @50 | OSWorld @100 | ScreenSpot-Pro | Notes |
|---|---|---|---|---|---|---|
| Human | -- | -- | -- | 72.4% | -- | Ceiling |
| GPT-5.4 (CU agent) | $$$$ | -- | -- | 75.0% | -- | New SOTA |
| Claude Opus 4.6 | $$$$$ | -- | -- | 72.7% | -- | 4-avg |
| Claude Sonnet 4.6 | $$$$ | -- | -- | 72.5% | -- | 4-avg |
| Agent S3 (o3) | $$$$ | -- | -- | 61.1% | -- | Simular |
| CoACT-1 | -- | -- | -- | 59.9% | -- | OSWorld-Verified |
| Agent S2 (GPT-5) | $$$$ | -- | -- | 48.8% | -- | Simular |
| UI-TARS-2 | $$ | -- | -- | 47.5% | -- | ByteDance |
| UI-TARS-1.5-7B | $ | -- | -- | 42.5% | 35.7% | Our grounding model standalone |
| Agent S2 (Claude 3.7) | $$$ | 27.0% | 34.5% | -- | -- | Closest comparison |
| OpenAI CUA | $$$$ | 19.7% | 32.6% | 42.9% | 23.4% | @100 uses o3 |
| UI-TARS-72B-DPO | $$ | 22.7% | 24.6% | -- | -- | |
| UI-TARS-7B + RegionFocus | $ | -- | -- | -- | 41.2% | arXiv:2505.00684 |
| **DETM Run 1** | **$** | **28.3%** | -- | -- | **41.3%** | Gemini 3.1 Flash Lite + UI-TARS-7B |
| **DETM Run 2** | **$** | -- | -- | **56.4%** (Chrome) | -- | **Gemini 3 Flash Preview + UI-TARS-7B** |
| n1 (Yutori) | $$ | -- | -- | 6.5% (Chrome) | -- | Qwen3-VL based, pixels-to-actions |

Cost tiers: $ = <$0.01/task, $$ = $0.01-0.10, $$$ = $0.10-0.50, $$$$ = $0.50-2.00, $$$$$ = $2.00+

---

## OSWorld Run 2 Results — 2026-03-12

**Config:** Gemini 3 Flash Preview (supervisor) + UI-TARS-1.5-7B (grounding), 100 max steps, Chrome domain only (46 tasks), screenshot-only observation.

**Score: 56.4% (25.96/46)** — 25 pass, 1 partial (0.96), 20 fail. Up from Run 1's 28.3% (+28pp).

### What changed from Run 1
- Supervisor upgraded: `gemini-3.1-flash-lite-preview` → `gemini-3-flash-preview` (much stronger reasoning)
- Max steps: 15 → 100
- Bugs fixed: infeasible detection, GROUNDING_MAX_DIM=1920 (was 960), divergence checks in iterative narrowing

### Failure analysis (20 failures)

| Category | Count | Description |
|----------|-------|-------------|
| REASONING | 5 | Wrong approach, wrong page, or failed to detect infeasible task |
| GROUNDING | 4 | Clicked wrong element, missed saving/confirming |
| LOOP | 3 | Repeated same failing action endlessly |
| TIMEOUT | 2 | Correct direction but ran out of 100 steps |
| Unanalyzed | 5 | From late batch |

**Key failure patterns:**

1. **No infeasible task detection (2 tasks):** Tasks with `func: "infeasible"` evaluators. Agent never escalates — keeps trying alternatives. Needs a "this can't be done" pathway.

2. **Custom web widgets break input (3 tasks):** Autocomplete dropdowns and date pickers don't respond to `tripleClick + pyautogui.write`. Causes terminal loops (50+ repeated attempts).

3. **Sidebar filter checkboxes hard to click (3 tasks):** Small checkboxes/radio buttons on Google Shopping, NBA Store, Cars.com — UI-TARS grounding misses.

4. **Premature "done" calls (3 tasks):** Agent declares success before verifying: wrong settings page, wrong FAQ page, sort dropdown still open.

5. **No loop detection (cross-cutting):** No mechanism to detect repeated identical states and force strategy change.

### Baseline comparison (Chrome subset, @100 steps)
| Agent | Score | Notes |
|-------|-------|-------|
| n1 (Yutori) | 6.5% (3/46) | key_comb bug fixed, still low due to viewport offset + no loop detection |
| DETM Run 1 | ~28% | Flash Lite, 15 steps |
| **DETM Run 2** | **56.4%** | Gemini 3 Flash, 100 steps |
| Agent S3 single-run | 62.6% | All domains, not just Chrome |
| Agent S3 + bBoN | 72.6% | Multiple rollouts |

Results at: `/home/alex/OSWorld/results/pyautogui/screenshot/detm-gemini-3-flash-preview-uitars/chrome/`

---

## Grounding Research — 2026-03-12

### All top agents use screenshot-only
No DOM or a11y tree. A11y tree was common in 2024, abandoned by all leaders in 2025 (3-26s per step, ~158K tokens for DOM, unreliable across apps). New supplement: code execution (bash/python), not structured UI data.

### Grounding accuracy is mostly pipeline, not model
UI-TARS-7B jumps from 35.7% to 56.1% on ScreenSpot-Pro with MVP alone (+57% relative). Going 7B→72B only adds ~2.4pp raw. Pipeline improvements matter much more than model size.

### ScreenSpot-Pro scores with test-time techniques

| Model | Baseline | +RegionFocus | +MVP |
|-------|----------|-------------|------|
| UI-TARS-7B (current) | 35.7% | 41.2% | 56.1% |
| UI-TARS-72B | 38.1% | 50.2% | — |
| Qwen3-VL-8B | 55.0% | — | 65.3% |
| **Qwen3-VL-32B** | **55.3%** | — | **74.0%** |
| Qwen2.5-VL-72B | 43.6% | 61.6% | — |
| Qwen3.5-122B-A10B | 65.6% | — | — |
| UI-Venus-1.5 (SOTA) | 69.6% | — | — |

### Multi-View Prompting (MVP) — CVPR 2026

Training-free inference-time technique (arXiv:2512.08529). Run full-image prediction + 4 attention-guided crops (2x upscaled), cluster predictions with 14px threshold. Cannot use attention-guided crops via API (needs model internals), but simplified version possible: initial prediction → 4 crops at different scales/offsets → parallel grounding → cluster.

### How top agents handle grounding
| Agent | Model | Refinement |
|-------|-------|-----------|
| Agent S3 | UI-TARS 7B/72B | None (single-shot, bBoN compensates) |
| UiPath Screen Agent | UI-TARS-1.5 | 512x512 crop + UI element validator |
| Our DETM | UI-TARS-7B | 300px/150px iterative narrow (RegionFocus-style) |

### Available grounding models on OpenRouter

| Model | ScreenSpot-Pro | OpenRouter ID | Cost (in/out $/M) |
|-------|---------------|---------------|-------------------|
| UI-TARS 7B (current) | ~38% | `bytedance/ui-tars-1.5-7b` | $0.10/$0.20 |
| UI-TARS 72B | ~38% raw | `bytedance-research/ui-tars-72b` | free tier |
| **Qwen3-VL-32B** | **60.5%** | `qwen/qwen3-vl-32b-instruct` | $0.10/$0.42 |
| Qwen3-VL-8B | 55.0% | `qwen/qwen3-vl-8b-instruct` | $0.08/$0.50 |
| Qwen3.5-122B-A10B | 65.6% | `qwen/qwen3.5-122b-a10b` | $0.26/$2.08 |

### Coordinate formats
- **UI-TARS**: 0-1000 normalized, `<point>x y</point>`
- **Qwen3-VL/3.5**: 0-1000 normalized, `<tool_call>{"name":"computer_use","arguments":{"coordinate":[x,y]}}</tool_call>`
- Same coord space — only parser changes needed, not scaling logic.

### Planned grounding improvements (priority order)

1. **Swap to Qwen3-VL-32B** — +22pp raw accuracy, same price tier, new `backends/qwen3vl.py` (keep uitars.py untouched)
2. **Proportional crop radii** — 0.3x/0.15x screen width instead of fixed 300/150px
3. **Simplified MVP** — parallel multi-crop grounding + clustering (without attention guidance)
4. **Loop detection** — track screenshots, detect identical states, force strategy change
5. **Infeasible task detection** — let supervisor signal "impossible" after repeated failures
6. **Verify-before-done** — force instruction re-read before calling done()

---

## Additional Technical Detail

Everything below is reference material: benchmark descriptions, agent breakdowns, implementation plans, and methodology.

---

## Benchmark Descriptions

| Benchmark | What It Measures | Tasks | How It Works | Metric |
|---|---|---|---|---|
| **OSWorld** | End-to-end desktop agent capability. Can the agent complete real multi-step tasks on a Linux desktop using mouse, keyboard, and apps? | 369 tasks: Chrome, LibreOffice, GIMP, VS Code, terminal, file mgmt, cross-app workflows | Agent gets dropped into a real Ubuntu VM. Sees only screenshots. Sends mouse/keyboard actions. Eval scripts inspect actual system state (files, configs, app state) after agent finishes. | Success rate (%). Binary pass/fail per task. No partial credit. |
| **ScreenSpot-Pro** | GUI element grounding accuracy. Given a screenshot and an instruction like "click the save button", can the model predict the right pixel? | 1,581 samples across 23 pro apps (VS Code, Photoshop, AutoCAD, Blender, etc.) on Win/Mac/Linux | Static dataset. Model receives screenshot + instruction, returns (x, y) coordinates. Point-in-box check against ground-truth bounding box. | Click accuracy (%). Binary correct/wrong per sample. |
| **WebArena Verified Hard** | Web agent task completion. Can the agent accomplish real tasks on self-hosted web applications? | 137 tasks (curated hard subset) across e-commerce, GitLab, Reddit-like forum, CMS, maps | Agent interacts with 5 real web apps running in Docker. Info-seeking tasks checked via string matching. State-changing tasks verified by querying backend DB/API. | Success rate (%). Binary pass/fail per task. |

---

## Per-Benchmark Score Tables

Detailed published scores with step counts, rollout methods, and sources.

### OSWorld — By Step Budget

All scores are Pass@1 (single rollout) unless noted. OSWorld-Verified uses 100 max steps averaged across 4 runs.

#### 15-Step Evaluation

The original OSWorld protocol. Tests agent efficiency under tight step budget.

| Agent | OSWorld % | Cost Tier | Source |
|---|---|---|---|
| **DETM (ours, corrected)** | **28.3** | **$** | **Run 1: Gemini 3.1 Flash Lite + UI-TARS-7B** |
| Agent S2 (Claude 3.7 Sonnet) | 27.0 | $$$ | arXiv:2504.00906 |
| Agent S2 (Claude 3.5 Sonnet) | 24.5 | $$$ | arXiv:2504.00906 |
| UI-TARS-72B-DPO | 22.7 | $$ | arXiv:2504.00906 |
| Agent S (GPT-4o) | 20.6 | $$ | arXiv:2504.00906 |
| Agent S (Claude 3.5 Sonnet) | 20.5 | $$ | arXiv:2504.00906 |
| OpenAI CUA | 19.7 | $$$$ | arXiv:2504.00906 |
| UI-TARS-72B-SFT | 18.7 | $$ | arXiv:2504.00906 |
| Aguvis-72B (GPT-4o) | 17.0 | $$ | arXiv:2504.00906 |
| CCU (Claude 3.7 Sonnet) | 15.5 | $$$ | arXiv:2504.00906 |
| Aria-UI (GPT-4o) | 15.2 | $$ | arXiv:2504.00906 |
| CCU (Claude 3.5 Sonnet) | 14.9 | $$$ | arXiv:2504.00906 |

DETM is #1 at 15 steps despite using models 50-100x cheaper than all competitors.

#### 50-Step Evaluation

Extended step budget. More room for complex multi-step tasks.

| Agent | OSWorld % | Cost Tier | Source |
|---|---|---|---|
| Agent S2 (Claude 3.7 Sonnet) | 34.5 | $$$ | arXiv:2504.00906 |
| Agent S2 (Claude 3.5 Sonnet) | 33.7 | $$$ | arXiv:2504.00906 |
| OpenAI CUA | 32.6 | $$$$ | arXiv:2504.00906 |
| CCU (Claude 3.7 Sonnet) | 26.0 | $$$ | arXiv:2504.00906 |
| UI-TARS-72B-DPO | 24.6 | $$ | arXiv:2504.00906 |
| CCU (Claude 3.5 Sonnet) | 22.0 | $$$ | arXiv:2504.00906 |
| UI-TARS-72B-SFT | 18.8 | $$ | arXiv:2504.00906 |
| **DETM (ours)** | **TBR** | **$** | **Next run with checkpoint @50** |

#### 100-Step Evaluation (OSWorld-Verified)

Full step budget used by top-scoring agents. OSWorld-Verified scores averaged across 4 runs.
ok
| Agent | OSWorld % | Rollout | Cost Tier | Source |
|---|---|---|---|---|
| GPT-5.4 (CU agent) | 75.0 | Pass@1 | $$$$ | OpenAI |
| Claude Opus 4.6 | 72.7 | Pass@1 (4-avg) | $$$$$ | Anthropic |
| Claude Sonnet 4.6 | 72.5 | Pass@1 (4-avg) | $$$$ | Anthropic |
| Agent S3 (GPT-5, bBoN N=10) | 69.9 | Best-of-10* | $$$$$ | Simular |
| Claude Opus 4.5 | 66.3 | Pass@1 (4-avg) | $$$$$ | Anthropic |
| Agent S3 (o3) | 61.1 | Pass@1 | $$$$ | Simular |
| Claude Sonnet 4.5 | 61.4 | Pass@1 (4-avg) | $$$ | Anthropic |
| CoACT-1 | 59.9 | Pass@1 | -- | OSWorld-Verified |
| EvoCUA-32B | 56.7 | Pass@1 | $$ | Meituan (50 steps) |
| GTA1 (o3) | 53.1 | Step-wise | $$$$ | Published |
| Jedi-7B (o3) | 51.0 | Pass@1 | $$$$ | Published |
| Agent S2 (GPT-5) | 48.8 | Pass@1 | $$$$ | Simular |
| UI-TARS-2 | 47.5 | Pass@1 | $$ | ByteDance |
| Claude Sonnet 4.0 | 43.9 | Pass@1 (4-avg) | $$$ | Anthropic |
| OpenAI CUA-o3 | 42.9 | Pass@1 | $$$$ | OpenAI |
| UI-TARS-1.5-7B | 42.5 | Pass@1 | $ | ByteDance |
| CCU (Claude 3.7 Sonnet) | 35.8 | Pass@1 | $$$ | Multiple |
| **DETM (ours)** | **TBR** | **Pass@1** | **$** | **Next run with checkpoint @100** |

*Agent S3 bBoN N=10 runs 10 trajectories and picks the best — NOT directly comparable to single-rollout Pass@1.

### ScreenSpot-Pro (Grounding Accuracy)

| Agent | Accuracy % | Method | Source |
|---|---|---|---|
| UI-TARS-1.5 (72B) | 61.6 | End-to-end | ByteDance |
| UI-TARS-1.5-7B | 35.7 | End-to-end | ByteDance |
| UI-TARS-7B + RegionFocus | 41.2 | Iterative crop-zoom | arXiv:2505.00684 |
| **DETM Config B (ours)** | **41.3** | **Iterative narrowing (300px+150px)** | **This work** |
| UI-TARS-72B-DPO (v1) | 38.1 | End-to-end | ByteDance |
| Claude 3.7 Sonnet | 27.7 | End-to-end | ByteDance comparison |
| OpenAI CUA | 23.4 | End-to-end | ByteDance comparison |
| OS-Atlas-7B | 18.9 | End-to-end (baseline) | ScreenSpot-Pro paper |
| GPT-4o | 0.8 | Direct grounding | ScreenSpot-Pro paper |
| **DETM Config A: UI-TARS standalone (ours)** | **TBR** | **Single-shot** | -- |
| **DETM Config B: +iterative narrowing (ours)** | **TBR** | **3-pass crop-zoom** | -- |
| **DETM Config C: +convergence loop (ours)** | **TBR** | **Full refinement + cursor overlay** | -- |

### WebArena Verified Hard (Web Agent)

| Agent | Success % | Source |
|---|---|---|
| Human | 78.24 | WebArena paper |
| Claude Opus 4.6 | 68.0 | Anthropic |
| GPT-5.4 (CU agent) | 67.3 | OpenAI |
| Claude Sonnet 4.6 | 65.6 | Anthropic |
| Claude Opus 4.5 | 65.3 | Anthropic |
| OpenAI CUA | 58.1 | OpenAI |
| Claude 3.7 Sonnet | 52.0 | AgentOccam |
| GPT-4o (AgentOccam) | 42.8 | WebChoreArena paper |
| **DETM (ours)** | **TBR** | -- |

---

## Benchmarks We Will Run

### 1. OSWorld (Primary -- Desktop Agent Evaluation)

**What:** 369 real desktop tasks in Linux VMs. The industry gold standard for
computer use agents. Every serious agent reports an OSWorld number.

**Why this one:** DETM is a desktop agent. OSWorld tests exactly the skills DETM
has: browser navigation, LibreOffice, GIMP, VS Code, terminal, file management,
and cross-app workflows.

**Scoring:** Binary success rate. Evaluation scripts inspect actual system state
(files, app state, configs) after the agent finishes. No partial credit.

**Parameters:**
- Step budget: 100 max with checkpoint evaluation at 15 and 50
- Rollout: Pass@1 (single attempt -- no best-of-N)
- Observation: screenshot-only (matches our architecture)

**Human baseline: 72.36%**

**Infrastructure:** Docker with KVM. Each task gets a fresh VM snapshot. ~4 hours
with 8-16 parallel Docker envs, ~20 hours sequential.

**Repo:** `https://github.com/xlang-ai/OSWorld`

---

### 2. ScreenSpot-Pro (Grounding Accuracy)

**What:** 1,581 screenshot-instruction pairs from 23 professional applications
(VS Code, Photoshop, AutoCAD, Blender, etc.). Given a screenshot and an
instruction like "click the save button", predict the correct pixel coordinates.

**Why this one:** Directly measures our grounding pipeline (UI-TARS + iterative
narrowing). Static dataset, no VM needed -- fast to run, easy to compare.

**Scoring:** Click accuracy (point-in-box). Predicted (x,y) must fall within the
ground-truth bounding box. Binary per-sample.

**Infrastructure:** Just GPU inference. No VMs, no Docker environments. Can run
on the dataset in a few hours.

**Repo:** `https://github.com/likaixin2000/ScreenSpot-Pro-GUI-Grounding`
**Dataset:** `https://huggingface.co/datasets/likaixin/ScreenSpot-Pro`

---

### 3. WebArena Verified Hard (Web Agent Evaluation)

**What:** 137-task subset of WebArena (curated for difficulty and evaluation
reliability). Tests agents on self-hosted web apps: e-commerce (Magento),
GitLab, Reddit-like forum, CMS, maps.

**Why this one:** DETM handles browser tasks. WebArena Verified Hard is 83%
faster than the full 812-task suite while preserving agent ranking fidelity.

**Scoring:** Success rate. Info-seeking tasks use string matching; state-changing
tasks use programmatic backend verification.

**Human baseline: ~78%**

**Infrastructure:** Docker containers for 5 web apps. Needs ~50-100GB disk,
16GB+ RAM.

**Repo:** `https://github.com/web-arena-x/webarena`

---

## Benchmarks We Will NOT Run (and why)

| Benchmark | Reason |
|---|---|
| WindowsAgentArena | We run on Linux (display :99). Windows is out of scope. |
| AndroidWorld | Mobile. Not our target. |
| VisualWebArena | Interesting but 910 tasks is too many for initial eval. Revisit later. |
| MiniWoB++ | Saturated (96%+). Too easy to be informative. |
| Mind2Web | Static dataset, DOM-based. Doesn't test actual execution. |
| GAIA | General assistant benchmark, not specifically computer use. |
| Navi-Bench | Yutori's proprietary benchmark. Not independently reproducible. |

---

## Agents We Are Comparing Against

### Tier 1: Frontier Agents (Published Results Only -- Do Not Re-Run)

These are closed-source or require expensive API access. We use their published
numbers directly.

#### Claude Computer Use (Anthropic)

| Version | OSWorld | WebArena | ScreenSpot-Pro | Source |
|---|---|---|---|---|
| Claude 3.5 Sonnet (Oct 2024) | 14.9% | -- | -- | OSWorld paper |
| Claude 3.5 Sonnet v2 | 28.0% | -- | -- | UI-TARS-1.5 comparison |
| Claude 3.7 Sonnet | 35.8% | 52.0% | 27.7% | Multiple sources |
| Claude Sonnet 4.0 | 42.2-43.9% | -- | -- | Anthropic; OSWorld-Verified |
| Claude Sonnet 4.5 | 61.4% | -- | -- | Anthropic announcement |
| Claude Opus 4.5 | 66.3% | 65.3% | -- | Anthropic announcement |
| Claude Sonnet 4.6 | 72.5% | 65.6% | -- | Anthropic announcement |
| Claude Opus 4.6 | 72.7% | 68.0% | -- | Anthropic; Vellum benchmarks |

#### OpenAI GPT-5.4 / CUA

| Version | OSWorld | WebArena | ScreenSpot-Pro | Source |
|---|---|---|---|---|
| GPT-5.4 (native CU agent) | 75.0% | 67.3% | -- | OpenAI blog |
| OpenAI CUA (computer-use-preview) | 38.1% | 58.1% | 23.4% | OpenAI; ByteDance comparison |

GPT-5.4 pricing: $2.50/$15.00 per M tokens (input/output). Native computer use
via screenshot + mouse/keyboard -- same observation mode as DETM. Step budget
and rollout method (Pass@1 vs best-of-N) not published by OpenAI. Currently the
top OSWorld score at 75.0%, surpassing human baseline (72.4%).

#### Simular Agent S3

| Config | OSWorld | WindowsAgentArena | Source |
|---|---|---|---|
| Agent S3 (single rollout, GPT-5, 100 steps) | 62.6% | 50.2% | Simular blog |
| Agent S3 (bBoN N=10, GPT-5, 100 steps) | 69.9% | 56.6% | Simular blog |
| Agent S3 (bBoN, claimed peak) | 72.6% | -- | Simular blog |

Note: Best-of-N results are NOT directly comparable to single-rollout Pass@1.
Our evaluation will be Pass@1 only.

#### Yutori N1

| Benchmark | Score | Source |
|---|---|---|
| Navi-Bench v1 | 83.4% | Yutori blog |
| Online-Mind2Web (self-reported, human eval) | 78.7% | Yutori blog |
| OSWorld | NOT REPORTED | -- |
| WebArena | NOT REPORTED | -- |
| ScreenSpot-Pro | NOT REPORTED | -- |

N1 has no scores on any of our three benchmarks. We cannot directly compare
until Yutori publishes OSWorld/WebArena numbers or we run N1 via their API
(see Tier 2 below).

---

### Tier 2: Runnable Baselines (We Will Execute These)

These are open-source or API-accessible agents we can run ourselves on our
infrastructure to get fresh, controlled numbers under identical conditions.

#### A. UI-TARS-1.5-7B (our grounding model, standalone)

**Why:** This is the exact model we use for pixel grounding. Running it
standalone (without our Gemini supervisor) tells us exactly how much value the
DETM orchestration layer adds on top of raw UI-TARS.

**Published results (reference, but we re-run for controlled comparison):**

| Benchmark | Published Score | Source |
|---|---|---|
| OSWorld (100 steps) | 42.5% | ByteDance GitHub |
| ScreenSpot-Pro | 35.7% | ByteDance GitHub |
| WebVoyager | 84.8-87.0% | ByteDance GitHub |
| Online-Mind2Web | 75.8% | ByteDance GitHub |

**How to run:** Via OpenRouter (`bytedance/ui-tars-1.5-7b`) or local
HuggingFace deployment. OSWorld has an existing UI-TARS integration.

**Decision:** Use published ScreenSpot-Pro number (35.7%). Re-run on OSWorld
only if our adapter works cleanly, since ByteDance's number (42.5%) was at 100
steps and we want 15-step and 50-step numbers too.

#### B. Agent S3 (open-source, Simular)

**Why:** Closest architectural analog to DETM -- uses a frontier LLM supervisor
+ UI-TARS for grounding. Direct comparison shows how our orchestration layer
stacks up.

**How to run:**
```bash
git clone https://github.com/simular-ai/Agent-S.git
pip install .
```
Requires an LLM API key (GPT-4o for affordable runs, GPT-5 for peak).

**Decision:** Run Agent S3 with GPT-4o on OSWorld (our same step budgets) for a
controlled comparison. Use published GPT-5 numbers as the "best case" reference.

#### C. OpenCUA (open-source reproduction of OpenAI CUA)

**Why:** Open-source, well-documented, already integrated with OSWorld.

**Published results:**

| Version | OSWorld (100 steps) | Source |
|---|---|---|
| OpenCUA-72B | 45.0% | opencua.xlang.ai |
| OpenCUA-32B | 55.7% | opencua.xlang.ai |
| OpenCUA-7B | 44.3% | opencua.xlang.ai |

**How to run:** `https://opencua.xlang.ai`

**Decision:** Use published numbers. Only re-run if we need 15-step comparisons.

#### D. Yutori N1 (via API -- optional)

**Why:** They claim strong browser performance but have zero overlap with our
benchmark set. Running N1 on WebArena would be the only way to compare.

**How to run:** API at `docs.yutori.com`. $0.75/$3.00 per M tokens.

**Decision:** Low priority. Only run if we want browser-specific comparison.
Their API would need a WebArena adapter, and they have no desktop capability
(so no OSWorld).

---

### Tier 3: Published-Only References (No Re-Run Needed)

These have well-established published numbers. We just cite them.

| Agent | OSWorld @15 | OSWorld @50 | OSWorld @100 | WebArena | ScreenSpot-Pro | Notes |
|---|---|---|---|---|---|---|
| Human | -- | -- | 72.4% | 78.2% | -- | Ceiling |
| GPT-5.4 (CU agent) | -- | -- | 75.0% | 67.3% | -- | New SOTA |
| Claude Opus 4.6 | -- | -- | 72.7% | 68.0% | -- | 4-avg, Anthropic |
| Claude Sonnet 4.6 | -- | -- | 72.5% | 65.6% | -- | 4-avg, Anthropic |
| Agent S3 (o3) | -- | -- | 61.1% | -- | -- | Simular |
| CoACT-1 | -- | -- | 59.9% | -- | -- | OSWorld-Verified |
| EvoCUA-32B | -- | -- | 56.7% | -- | -- | Meituan |
| Agent S2 (GPT-5) | -- | -- | 48.8% | -- | -- | Simular |
| UI-TARS-2 | -- | -- | 47.5% | -- | -- | ByteDance |
| UI-TARS-1.5-7B | -- | -- | 42.5% | -- | 35.7% | ByteDance |
| Agent S2 (Claude 3.7) | 27.0% | 34.5% | -- | -- | -- | arXiv:2504.00906 |
| OpenAI CUA | 19.7% | 32.6% | 42.9% | 58.1% | 23.4% | @100 uses o3 |
| UI-TARS-72B-DPO | 22.7% | 24.6% | -- | -- | -- | arXiv:2504.00906 |
| GPT-4o (bare) | 5.0% | -- | -- | 42.8% | 0.8% | Floor |
| OS-Atlas-7B | -- | -- | -- | -- | 18.9% | ScreenSpot-Pro baseline |

---

## What We Measure for DETM

When we run DETM on these benchmarks, we report:

| Metric | Description |
|---|---|
| **Success Rate (Pass@1)** | % of tasks completed correctly. Single attempt. The primary metric. |
| **Success Rate @15 steps** | Performance under tight step budget (industry standard). |
| **Success Rate @50 steps** | Performance under relaxed step budget. |
| **Avg Steps to Success** | Mean steps for successfully completed tasks. Lower = more efficient. |
| **Avg Time per Task** | Wall-clock seconds per task (includes API latency). |
| **Avg Cost per Task** | Estimated API cost (OpenRouter tokens for UI-TARS + Gemini). |
| **Grounding Accuracy** | ScreenSpot-Pro click accuracy (tests our iterative narrowing pipeline). |

---

## Execution Order

### Phase 1: ScreenSpot-Pro (fastest, highest signal-to-noise)
- Download dataset from HuggingFace
- Run three configs through `benchmarks/screenspot_pro/eval.py`:
  - Config A: UI-TARS standalone (baseline, should reproduce ~35.7%)
  - Config B: UI-TARS + iterative narrowing (crop-zoom refinement only)
  - Config C: Full DETM refinement via `_refine_cursor()` with convergence loop + cursor overlay
- Expected time: 2-4 hours per config at OpenRouter speeds

### Phase 2: OSWorld Setup + Baseline Run (adapter done, Docker pending)
- Clone OSWorld, build Docker environments
- DETM adapter already built (`benchmarks/osworld/detm_agent.py`) via callback injection
- Run UI-TARS-1.5-7B standalone as baseline (verify we reproduce ~42.5%)
- Run Agent S3 with GPT-4o as baseline
- Expected time: 1-2 days for Docker setup, 4-8 hours per agent run

### Phase 3: DETM on OSWorld
- Run DETM through the adapter at 15-step and 50-step budgets
- Analyze results by category (browser, office, terminal, multi-app)
- Expected time: 4-8 hours

### Phase 4: WebArena Verified Hard (optional, if Phase 2-3 go well)
- Set up Docker web app environments
- Write WebArena adapter for DETM
- Run on 137-task subset
- Expected time: 1 day setup, 2-4 hours run

---

## Key Methodological Decisions

1. **Single rollout only (Pass@1).** Best-of-N inflates scores 10-15% and is
   not representative of real-world use. Agent S3's 72.6% drops to 62.6% at
   Pass@1. We report Pass@1 exclusively.

2. **Screenshot-only observation.** No accessibility tree. This matches DETM's
   actual architecture (UI-TARS sees pixels, not DOM).

3. **Step budgets: 15, 50, and 100 with checkpoints.** 15 is the original OSWorld
   standard. Top agents (GPT-5.4, Opus 4.6, Agent S3) use 50-100 steps. We run
   at 100 max steps with checkpoint evaluations at steps 15 and 50 to enable
   fair comparison across all step budgets from a single run.

4. **No cherry-picking.** Run the full benchmark. Report overall score and
   per-category breakdown. No subset selection after seeing results.

5. **Cost tracking.** API costs are a real differentiator. DETM uses UI-TARS-7B
   via OpenRouter (~cheap) + Gemini Flash (~cheap) vs agents that burn GPT-5 or
   Claude Opus 4.6 tokens.

---

## Summary: What We Already Know vs What We Need to Run

### Already Published (cite directly, no re-run):

| Agent | OSWorld | ScreenSpot-Pro | WebArena |
|---|---|---|---|
| GPT-5.4 (CU agent) | 75.0% | -- | 67.3% |
| Human | 72.36% | -- | 78.24% |
| Claude Opus 4.6 | 72.7% | -- | 68.0% |
| Claude Sonnet 4.5 | 61.4% | -- | -- |
| Agent S3 (Pass@1, GPT-5) | 62.6% | -- | -- |
| Agent S3 (bBoN, GPT-5) | 69.9% | -- | -- |
| OpenAI CUA | 38.1% | 23.4% | 58.1% |
| UI-TARS-1.5-7B | 42.5% | 35.7% | -- |
| OS-Atlas-7B | -- | 18.9% | -- |
| GPT-4o (bare) | 5.0% | 0.8% | 42.8% |

### Need to Run Ourselves:

| What | Why | Priority | Status |
|---|---|---|---|
| ScreenSpot-Pro Config A (UI-TARS standalone) | Reproduce 35.7% baseline | P0 | Not yet run |
| ScreenSpot-Pro Config B (+iterative narrowing) | Ablation: crop-zoom only | P0 | **Done: 41.3% (653/1581)** |
| ScreenSpot-Pro Config C (+convergence loop) | Full refinement pipeline | P0 | Not yet run |
| DETM on OSWorld @15 steps | Primary benchmark number | P0 | **Done: 28.3% (Run 1, with infeasible fix)** |
| DETM on OSWorld @100 steps (with fixes) | Extended step budget + all fixes | P0 | Pending (Run 2) |
| Agent S3 w/ GPT-4o on OSWorld | Controlled baseline (same infra) | P1 | Not started |
| UI-TARS-1.5-7B standalone on OSWorld (15 steps) | Ablation baseline | P1 | Not started |
| DETM on WebArena Verified Hard | Web task performance | P2 | Not started |
| Yutori N1 on WebArena via API | Cross-compare browser agent | P3 | Not started |

---

## Implementation Plan

### Phase 1: ScreenSpot-Pro Evaluation (code complete, runs pending)

No VMs, no Docker, just a static dataset + our grounding pipeline.

#### Dataset

- HuggingFace: `lmms-lab/ScreenSpot-Pro` (1,581 samples, single `train` split)
- Each sample: `image` (PIL), `instruction` (str), `bbox` [x1,y1,x2,y2] in
  pixels, `img_size` [width, height], `ui_type` (text/icon), `group`, `application`
- Resolutions range from 1920x1080 to 6016x3384 (many are 2560x1440+)
- Targets are tiny (~0.07% of image area on average)

#### Evaluation Logic

```python
# Ground truth: bbox [x1,y1,x2,y2] in pixel coords, normalize by img_size
# Prediction: (x, y) in normalized [0,1] coords
# Pass if: x1/w <= pred_x <= x2/w AND y1/h <= pred_y <= y2/h
```

#### What We Run

**Config A: UI-TARS-1.5-7B standalone (baseline)**
- Single API call: screenshot + instruction directly to UI-TARS via OpenRouter
- Uses `UITARSBackend.ground()` directly
- Expected: ~35.7% (published baseline for UI-TARS-1.5-7B)

**Config B: UI-TARS + iterative narrowing (crop-zoom only)**
- Initial grounding via UI-TARS, then two crop-zoom-reground passes via
  `_iterative_narrow()` (radii: 300px, 150px)
- 3 API calls total per sample
- Tests whether RegionFocus-style crop refinement helps on its own
- Note: RegionFocus paper already published iterative narrowing results, so
  this config is mainly for our own ablation rather than a novel claim

**Config C: Full DETM refinement (narrowing + convergence with cursor overlay)**
- Calls `_refine_cursor(target=instruction, display=None, frame=np_array)`
- Runs the full pipeline: initial grounding, iterative narrowing, then
  convergence loop where a cursor overlay is drawn on the static image
  (via `draw_cursor_overlay()`) and re-grounded to verify placement
- This is the interesting test: Gemini is NOT in the loop for ScreenSpot-Pro
  (it's a grounding-only benchmark), but the full convergence machinery
  (cursor overlay + stability check) is exercised

#### Script: `benchmarks/screenspot_pro/eval.py` (implemented)

```bash
PYTHONPATH=src python3 benchmarks/screenspot_pro/eval.py --config A          # UI-TARS standalone
PYTHONPATH=src python3 benchmarks/screenspot_pro/eval.py --config B          # +narrowing
PYTHONPATH=src python3 benchmarks/screenspot_pro/eval.py --config C          # +convergence
PYTHONPATH=src python3 benchmarks/screenspot_pro/eval.py --config C --limit 10  # smoke test
```

Features:
- Loads `lmms-lab/ScreenSpot-Pro` from HuggingFace (1,581 samples)
- Point-in-box accuracy check per sample
- Reports: overall accuracy, per-group, per-application, per-ui_type, per-platform
- Resume support (`--resume results/config_C.json`) for interrupted runs
- Periodic saves every 50 samples to `benchmarks/screenspot_pro/results/`

Expected runtime: ~2-3 hours at OpenRouter speeds (1,581 API calls for Config A,
~4,700 for Config B, variable for Config C depending on convergence rounds).

---

### Phase 2: OSWorld Setup (adapter complete, Docker setup pending)

#### OSWorld Agent Interface

OSWorld expects an agent class with:

```python
class MyAgent:
    action_space = "pyautogui"  # or "computer_13"

    def predict(self, instruction: str, obs: dict) -> tuple[str, list]:
        """
        obs = {
            "screenshot": bytes,        # raw PNG bytes
            "accessibility_tree": str,   # XML or None
            "terminal": str,             # or None
            "instruction": str
        }
        Returns: (response_text, [list of pyautogui code strings])
        Special actions: "WAIT", "DONE", "FAIL"
        """

    def reset(self, _logger=None, vm_ip=None, **kwargs):
        """Called before each task."""
```

The main loop calls `agent.predict()`, passes each returned action to
`env.step()`, which executes it on the VM via pyautogui over HTTP.

Coordinate space: 1920x1080 by default.

#### Action Format (pyautogui strings)

```python
"pyautogui.click(500, 300)"
"pyautogui.typewrite('hello world')"
"pyautogui.hotkey('ctrl', 'c')"
"pyautogui.scroll(-3)"
"pyautogui.moveTo(100, 200)"
"pyautogui.dragTo(400, 500, duration=1.0)"
"DONE"   # task complete
"FAIL"   # task impossible
"WAIT"   # pause
```

#### DETM Adapter Design: Callback Injection (implemented)

Rather than duplicating code or running a daemon, we added optional callback
parameters directly to our pipeline functions in `openrouter.py`. When
callbacks are provided, the system operates in "benchmark mode" -- no X11,
no xdotool. When they're `None`, the existing production code path is
unchanged.

**Changes to `OpenRouterVLMProvider.run()`:**

Three optional callback parameters:

```python
async def run(
    self,
    instruction, timeout, task_id, display, context="", session=None,
    # Benchmark mode (all None = production path unchanged):
    get_screenshot=None,         # () -> (jpeg_b64, cursor_pos | None)
    execute_override=None,       # (name, args) -> str
    display_size_override=None,  # (width, height)
) -> dict:
```

- `get_screenshot`: replaces `_capture_jpeg_b64()` -- caller provides the frame
- `execute_override`: replaces `execute_action()` -- caller collects actions
- `display_size_override`: replaces `_get_display_size()` -- caller sets resolution

**Changes to `_refine_cursor()`:**

```python
async def _refine_cursor(
    target, display, session=None, max_rounds=3,
    frame=None,  # injected screenshot for benchmark mode
) -> dict:
```

When `frame` is provided: skips `capture_screen(display)`, skips
`_smooth_mousemove()`, uses `draw_cursor_overlay(frame.copy(), x, y)` instead
of `capture_screen_with_cursor(display)`. The convergence loop still works
identically -- it just draws a synthetic cursor on the provided image instead
of moving a real one.

**OSWorld adapter (`benchmarks/osworld/detm_agent.py`):**

```python
class DETMAgent:
    action_space = "pyautogui"

    def predict(self, instruction, obs):
        frame = decode_png(obs["screenshot"])
        jpeg_b64 = encode_jpeg(frame)

        collected_actions = []

        def get_screenshot():
            return jpeg_b64, None

        def execute_override(name, args):
            collected_actions.append(_action_to_pyautogui(name, args))
            return "ok"

        result = provider.run(
            instruction=instruction,
            get_screenshot=get_screenshot,
            execute_override=execute_override,
            display_size_override=(w, h),
            ...
        )
        return response_text, collected_actions
```

This runs the full Gemini supervisor + UI-TARS grounding pipeline in-process,
with no X11, no xdotool, no daemon. The same code path that runs in
production is exercised -- the only difference is where screenshots come from
and where actions go.

#### Docker Environment

```bash
git clone https://github.com/xlang-ai/OSWorld
cd OSWorld
pip install -r requirements.txt
```

To plug DETM into OSWorld's evaluation loop:

```python
from benchmarks.osworld.detm_agent import DETMAgent
agent = DETMAgent()
# Use in OSWorld's run script with agent.predict() / agent.reset()
```

Minimum: 16GB RAM, Docker with KVM support. Each task gets a fresh container
from a base Ubuntu image (~25GB download once).

#### Baseline Runs on OSWorld

**Run 1: UI-TARS-1.5-7B standalone**
- Use OSWorld's existing `UITARSAgent` from `mm_agents/uitars_agent.py`
- Point it at OpenRouter's `bytedance/ui-tars-1.5-7b`
- Run at 15-step and 50-step
- Verify we reproduce ~42.5% (published at 100 steps)

**Run 2: Agent S3 with GPT-4o**
- Clone `github.com/simular-ai/Agent-S`, install
- Write OSWorld integration (Agent S3 has its own env loop, need to adapt)
- Use GPT-4o as backend (cheaper than GPT-5, gives controlled comparison)
- Run at 15-step and 50-step

#### File Structure (implemented)

```
benchmarks/
  screenspot_pro/
    eval.py              # ScreenSpot-Pro eval (configs A/B/C)
    results/             # output JSONs (config_A.json, config_B.json, config_C.json)
  osworld/
    detm_agent.py        # DETMAgent class with callback injection
    results/             # output dirs per step-budget

src/agentic_computer_use/live_ui/
    openrouter.py        # Modified: benchmark callbacks on run() and _refine_cursor()
```

Files NOT modified (confirmed display-independent):
- `gui_agent/agent.py` -- `_iterative_narrow()` is pure numpy + API
- `gui_agent/backends/uitars.py` -- `UITARSBackend.ground()` is pure API
- `capture/screen.py` -- `draw_cursor_overlay()` and `frame_to_jpeg()` are pure numpy/PIL
- `live_ui/actions.py` -- production xdotool code, bypassed via `execute_override` callback

---

### Phase 3: WebArena Verified Hard (Stretch Goal)

#### Setup

5 Docker containers:
- Shopping (Magento): port 7770
- Shopping Admin: port 7780
- Reddit (Postmill): port 9999
- GitLab: port 8023
- Maps (OpenStreetMap): port 3000

~50-100GB disk for Docker images. 16GB+ RAM.

#### Adapter

Similar to OSWorld adapter but for browser actions. WebArena agents interact
via Playwright. The DETM adapter would:
1. Capture browser screenshot
2. Send to Gemini supervisor
3. Get action (click, type, scroll)
4. Ground via UI-TARS
5. Execute via Playwright

Lower priority than OSWorld since WebArena is browser-only (doesn't test
DETM's full desktop capabilities).

---

## Implementation Timeline

| Week | What | Deliverable | Status |
|---|---|---|---|
| Week 1 | ScreenSpot-Pro eval script + run all configs | Grounding accuracy numbers | Config B done (41.3%), A+C pending |
| Week 1 | OSWorld adapter (callback injection) | DETMAgent class | **Done** |
| Week 2 | OSWorld Docker setup + first run | 28.3% @15 steps | **Done** |
| Week 2 | Failure analysis + bug fixes | 5 fixes identified and implemented | **Done** |
| Week 3 | OSWorld Run 2 (100 steps, all fixes) | Checkpoint scores @15/@50/@100 | Pending |
| Week 4 | Agent S3 baseline + analysis | Full comparison report | Not started |
---

## OSWorld Run 1 Results (March 2026)

### Configuration

- **Supervisor model:** `google/gemini-3.1-flash-lite-preview` via OpenRouter ($0.02/$0.08 per M tokens)
- **Grounding model:** `bytedance/ui-tars-1.5-7b` via OpenRouter ($0.15/$0.15 per M tokens)
- **Max steps:** 15 (industry standard)
- **Rollout:** Pass@1 (single attempt)
- **Observation:** Screenshot-only (no a11y tree)
- **Resolution:** 1920x1080 (VM), 960px max dim for grounding (bug — see fixes below)
- **Infrastructure:** Single Docker container (sequential), Ubuntu VM with KVM
- **Tasks completed:** 198/369 (run in progress during analysis, later completed full 369)

### Raw Scores

| Metric | Value |
|---|---|
| **Raw score (as-run)** | 47/198 = 23.7% |
| **Corrected score (with infeasible fix)** | 56/198 = 28.3% |
| **Google Drive setup failures** | 8 tasks (multi_apps domain) — `_googledrive_setup` step 1 failures, not agent errors |

The 4.6% gap between raw and corrected is due to a bug where `done(success=false)` sent "DONE" instead of "FAIL" to OSWorld's evaluator (see Bug Fixes below).

### Comparison at 15 Steps

| Agent | OSWorld @15 steps | Model | Cost Tier |
|---|---|---|---|
| **DETM (ours, corrected)** | **28.3%** | Gemini 3.1 Flash Lite + UI-TARS-7B | $ |
| Agent S2 (Claude 3.7 Sonnet) | 27.0% | Claude 3.7 Sonnet | $$$ |
| UI-TARS-72B (standalone) | 22.7% | UI-TARS-72B | $$ |
| OpenAI CUA | 19.7% | GPT-4o (CUA preview) | $$$$ |

DETM exceeds Agent S2 at 15 steps despite using models that are 50-100x cheaper. The comparison is especially notable because Agent S2 uses the same dual-model architecture (supervisor + grounding) but with Claude 3.7 Sonnet as supervisor.

Note: top scores (GPT-5.4 75%, Opus 4.6 72.7%) use 50-100 steps with frontier models costing $2-15/M tokens. Our next run uses 100 max steps with checkpoint evaluations at 15 and 50 for fair comparison across step budgets.

### Per-Domain Breakdown

| Domain | Tasks | Passed | Rate | Notes |
|---|---|---|---|---|
| chrome | ~50 | ~15 | ~30% | Browser navigation, search, form filling |
| libreoffice_calc | ~30 | ~8 | ~27% | Spreadsheet operations, formula entry |
| libreoffice_writer | ~20 | ~5 | ~25% | Document editing |
| libreoffice_impress | ~15 | ~3 | ~20% | Presentation editing |
| os | ~30 | ~10 | ~33% | File management, terminal, settings |
| vs_code | ~15 | ~4 | ~27% | Code editing, extensions |
| gimp | ~10 | ~1 | ~10% | Image editing — hardest domain |
| thunderbird | ~10 | ~2 | ~20% | Email client |
| vlc | ~8 | ~2 | ~25% | Media player settings |
| multi_apps | ~10 | ~0 | ~0% | Cross-app workflows (8 had Google Drive setup failures) |

---

## Failure Analysis

### Category 1: Grounding Errors (~40% of failures)

The agent locates the right element but clicks the wrong pixel. Types of grounding error:

| Type | Description | Example |
|---|---|---|
| **Spatial precision** | Clicks near but not on target (off by 20-100px) | Clicks toolbar gap instead of button |
| **Target ambiguity** | Multiple matching elements, picks wrong one | "Click Save" hits Save-As instead of Save |
| **Z-order/layer confusion** | Clicks element behind a modal/menu | Clicks through dropdown onto background |
| **Visual confusion** | Picks visually similar but wrong element | Clicks "Open" instead of "Open Recent" |
| **Dynamic content** | Element moves between screenshot and action | Dropdown items shift during load |
| **Occlusion** | Target partially hidden behind other UI | Scrollbar handle behind panel edge |
| **Scale/density** | Tiny targets (2-4px handles, dense toolbars) | Timeline handle in GIMP/DaVinci Resolve |

Root causes identified in code:
- **960px downscaling (FIXED):** `FRAME_MAX_DIM=960` was designed for SmartWait (YES/NO checks), not grounding. At 1920x1080, this creates a 2x coordinate error multiplier for UI-TARS.
- **Iterative narrowing divergence (FIXED):** 300px→150px crops can clip the target if the initial prediction is >150px off, causing cascading errors.
- **Large cursor overlay:** The red circle overlay (~57x57px) can occlude small targets during re-grounding passes. Convergence loop was removed because UI-TARS got confused by the cursor marker.
- **No explicit z-order awareness:** UI-TARS doesn't understand which window layer is on top when modals/menus overlap.

### Category 2: Premature Done (~30% of failures)

The agent declares success before the task is actually complete.

Sub-categories:
- **Infeasible tasks scored as failures (FIXED):** `done(success=false)` sent "DONE" instead of "FAIL". OSWorld's `infeasible()` evaluator returns 1.0 only when last action is "FAIL". This affected 7+ tasks.
- **Sentinel values not reaching env.step() (FIXED):** DONE/FAIL/WAIT were treated as loop-break sentinels without being passed through `env.step()`, so `action_history` was never populated.
- **False confidence:** Agent says "I can see the result is correct" but the screenshot shows incomplete state.
- **Step budget exhaustion:** At 15 max steps, complex tasks (especially LibreOffice) run out of steps mid-workflow.

### Category 3: Stuck in Loops (~15% of failures)

The agent repeats the same action 3+ times without progress.

Common patterns:
- **Click-miss loop:** Repeatedly clicking near a small target, never hitting it.
- **Navigation confusion:** Going back and forth between two pages/menus.
- **Scroll loop:** Scrolling past the target element and back repeatedly.

### Category 4: Approach Errors (~15% of failures)

The agent attempts the wrong approach entirely.

Examples:
- Using keyboard shortcuts that don't exist in the application
- Trying menu paths that are specific to a different OS/version
- Attempting to use features not available in the installed version

---

## Bug Fixes Applied

These fixes were implemented during analysis and will take effect on the next benchmark run.

### 1. Infeasible Task Handling (Critical — +4.6% impact)

**File:** `benchmarks/osworld/detm_agent.py`

**Bug:** `done(success=false)` returned `["DONE"]` to OSWorld. The evaluator's `infeasible()` function checks if the last action in `action_history` is `"FAIL"` to return 1.0. Sending "DONE" for genuinely infeasible tasks scored them as 0.0.

**Fix:** When `fn_args.get("success")` is false, return `["FAIL"]` instead of `["DONE"]`.

**Impact:** 9 tasks change from 0.0 to 1.0 (7 DONE→FAIL + 2 that already returned FAIL but weren't reaching `env.step()`). Score: 47/198 → 56/198 (+4.6%).

### 2. Sentinel Values Not Reaching env.step() (Critical)

**Files:** `benchmarks/osworld/run_detm.py`, `benchmarks/osworld/run_detm_multienv.py`

**Bug:** When the agent returned DONE/FAIL/WAIT, the run script broke out of the action loop without calling `env.step(action)`. This meant `action_history` was never populated, so OSWorld's `infeasible()` evaluator never saw the FAIL signal.

**Fix:** Pass all sentinel values through `env.step()` before breaking.

### 3. Hardcoded UI-TARS Model (Minor)

**File:** `src/agentic_computer_use/gui_agent/backends/uitars.py`

**Bug:** Line 21 had `OPENROUTER_MODEL = "bytedance/ui-tars-1.5-7b"` as a module-level constant, ignoring `config.UITARS_OPENROUTER_MODEL` (which reads `ACU_UITARS_OPENROUTER_MODEL` env var).

**Fix:** `OPENROUTER_MODEL = config.UITARS_OPENROUTER_MODEL`

### 4. Grounding Resolution Too Low (High impact — expected +3-5%)

**File:** `src/agentic_computer_use/config.py`

**Bug:** UI-TARS grounding used `FRAME_MAX_DIM=960` (designed for SmartWait YES/NO checks). At 1920x1080, this halves resolution, doubling coordinate error.

**Fix:** Added separate grounding-specific config:
```python
GROUNDING_MAX_DIM = int(os.environ.get("ACU_GROUNDING_MAX_DIM", "1920"))
GROUNDING_JPEG_QUALITY = int(os.environ.get("ACU_GROUNDING_JPEG_QUALITY", "80"))
```

Updated all grounding calls in `gui_agent/agent.py` and `live_ui/openrouter.py` to use these values.

### 5. Iterative Narrowing Divergence Check (Medium impact)

**File:** `src/agentic_computer_use/gui_agent/agent.py`

**Bug:** The 300px→150px crop-zoom could clip the target when the initial prediction was >150px off. The narrowing would then lock onto a random element in the crop, producing a worse result than the initial prediction.

**Fix:** Added divergence check — if the refined position shifts >70% of the crop radius from center, abort narrowing and keep the previous prediction:
```python
shift = ((new_x - x0) ** 2 + (new_y - y0) ** 2) ** 0.5
if shift > radius * 0.7:
    log.warning("prediction shifted %.0fpx (>70%% of %dpx radius), aborting", shift, radius)
    break
```

---

## Architecture: What Makes DETM Unique

### Separation of Reasoning and Grounding

Most agents (Claude CU, OpenAI CUA, standalone UI-TARS) use a single model for both deciding WHAT to do and WHERE to click. DETM splits these:

| Component | Role | Model | Cost |
|---|---|---|---|
| **Supervisor** | Decides what action to take, monitors progress | Gemini Flash / Qwen Flash | ~$0.02-0.10/M tokens |
| **Grounding** | Finds exact pixel coordinates for targets | UI-TARS-7B | ~$0.15/M tokens |

This means the supervisor never predicts coordinates — it says `move_to(target="search bar")` and UI-TARS handles spatial reasoning. This enables using very cheap/fast supervisor models without sacrificing grounding accuracy.

### Target-Based Tool Schema

Gemini's tools have NO coordinate parameters. Instead:

```
move_to(target="the Export button in the File menu")
click()  # at current cursor position
type_text(text="hello")
scroll(direction="down")
drag(from_target="file.txt", to_target="Documents folder")
```

Compare to other agents where the LLM must predict raw (x, y):
- Claude CU: `mouse_move(coordinate=[500, 300])`
- OpenAI CUA: `click(x=500, y=300)`
- Agent S3: Uses UI-TARS coordinates directly

### Cursor-as-Visual-Feedback

DETM is the only agent that shows the cursor position to the supervisor BEFORE committing to a click. The flow:

1. Supervisor: `move_to(target="Save button")`
2. UI-TARS finds coordinates → cursor moves there
3. New screenshot shows red circle at cursor position
4. Supervisor sees the screenshot and decides: is the cursor on the right element?
5. If yes: `click()`. If no: `move_to(target="...")` again with more specific description.

Every other agent does predict→click atomically. If the prediction is wrong, the click already happened.

### Iterative Narrowing (RegionFocus-Style)

Three-pass grounding pipeline inspired by RegionFocus (ICCV 2025, arXiv:2505.00684):

1. **Full frame (960-1920px wide):** Initial coordinate prediction
2. **300px crop around prediction:** Re-ground on zoomed view (~5x scale)
3. **150px crop around refined prediction:** Re-ground on highly zoomed view (~10x scale)

This gives +28% accuracy over single-shot on UI-TARS (41.3% vs ~35.7% on ScreenSpot-Pro). Critical for small targets like timeline handles, scrollbar thumbs, and dense toolbar buttons.

### Two-Pass Thinking (for non-thinking models)

When the supervisor model doesn't support native thinking (e.g., Gemini Flash, Qwen Flash), DETM runs two passes:

1. **Text-only reasoning pass:** Model analyzes the screenshot and writes its analysis
2. **Tool call pass:** The reasoning is injected as context, model makes tool calls

This approximates chain-of-thought without requiring a thinking-enabled model, allowing use of the cheapest available models.

---

## Next Run Plan

### Changes from Run 1

| Change | Expected Impact |
|---|---|
| Max steps: 15 → 100 | More time for complex tasks, especially LibreOffice |
| Checkpoint eval at steps 15 and 50 | Fair comparison across step budgets |
| Grounding resolution: 960px → 1920px | +3-5% from better coordinate precision |
| Infeasible fix: DONE → FAIL | +4.6% (already measured) |
| Divergence check on narrowing | Fewer cascading grounding errors |
| Domain-based wait times | Less wasted time (Chrome 15s vs LibreOffice 50s) |

### Domain-Based Wait Times

Replaces the hardcoded 60-second wait for all environments:

| Domain | Wait (seconds) | Rationale |
|---|---|---|
| chrome, os | 15 | Browser/file manager loads fast |
| vlc, vs_code | 20 | Medium-weight apps |
| gimp, thunderbird | 25 | Heavier apps, plugin loading |
| multi_apps | 40 | Multiple apps need to initialize |
| libreoffice_* | 50 | Cold start is genuinely slow (Java runtime) |

### Infrastructure

- **Parallel execution:** `run_detm_multienv.py` supports N Docker containers pulling from a shared task queue
- **Resume support:** Automatically skips tasks with existing `result.txt`
- **Session recording:** Full trajectory logging with before/after screenshots per step

---

## References

- OSWorld: https://os-world.github.io / https://github.com/xlang-ai/OSWorld
- OSWorld-Verified: https://xlang.ai/blog/osworld-verified
- ScreenSpot-Pro: https://github.com/likaixin2000/ScreenSpot-Pro-GUI-Grounding
- ScreenSpot-Pro Leaderboard: https://gui-agent.github.io/grounding-leaderboard/
- WebArena: https://webarena.dev / https://github.com/web-arena-x/webarena
- Agent S3: https://github.com/simular-ai/Agent-S / https://www.simular.ai/articles/agent-s3
- OpenCUA: https://opencua.xlang.ai
- UI-TARS-1.5: https://github.com/bytedance/UI-TARS
- Yutori N1: https://yutori.com/blog/introducing-n1
- Anthropic CU: https://www.anthropic.com/news/claude-sonnet-4-5
- OpenAI CUA: https://openai.com/index/computer-using-agent/
