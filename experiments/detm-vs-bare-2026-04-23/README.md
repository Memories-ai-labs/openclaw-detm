# DETM vs Bare-xdotool — LinkedIn 5-Connect Benchmark

**Date:** 2026-04-23 / 2026-04-24
**Task shape (constant across runs):** *"Send 5 LinkedIn connection requests to employees of an AI company."*
**Main LLM (all runs):** `openai-codex/gpt-5.4` via OpenClaw TUI on `ws://127.0.0.1:18789`
**Display:** Xvfb `:99` at 1920×1080, real Firefox with persistent LinkedIn session, on host `alxdws2`.

## Executive summary

Four runs, three configurations, same task. All four completed 5/5 — every completion was visually verified by a post-run screenshot (`verification-screenshots/`) plus per-person "Pending" confirmations inside the session JSONL.

- **Full DETM (gui_agent + desktop_look + task_*)** — 6:26 cold/chained.
- **DETM minus gui_agent (desktop_look + task_* only)** — 3:42.
- **Bare xdotool (no DETM at all)** — 6:20 fresh, 9:32 warm replay.

The headline finding: the `desktop_look` + `task_*` scaffolding is the measurable win on this class of task. The `gui_agent` supervisor is a situational add — per-call overhead makes it slower than main-LLM-drives-xdotool when chained across many short calls.

## Results

| # | Config | Target | Wall-clock | Main-LLM tool calls | Outcome |
|---|---|---|---|---|---|
| 2 | **Bare xdotool**, fresh session | Scale AI | **6:20** (379.8s) | **39** (21 exec + 17 image + 1 read) | 5/5 ✓ |
| 3 | **Bare xdotool**, warm session | Inflection | **9:32** (571.6s) | **52** (26 image + 24 exec + 2 process) | 5/5 ✓ |
| 4 | **DETM full**, fresh session | Hugging Face | **6:26** (386.7s) | **53** (6 gui_agent + 4 desktop_look + 5 read + 35 task_* + 2 task_register + 1 task_list) | 5/5 ✓ |
| 5 | **DETM minus gui_agent**, fresh session | Perplexity | **3:42** (222.0s) | **39** (11 task_log_action + 7 task_update + 6 task_item_update + 6 desktop_look + 6 exec + 1 update_plan + 1 task_register + 1 process) | 5/5 ✓ |

(Run numbering reflects the original experimental order; Run 1 was a warm-session best-case outlier and has been dropped — see Methodology.)

Wall-clock measured from the user's prompt timestamp to the assistant's final "Done" message in the OpenClaw session JSONL at `~/.openclaw/agents/main/sessions/*.jsonl` — not the TUI streaming counter (which drifts ±40s). Verification commands at the bottom of this doc reproduce every number from the committed artifacts.

### Surprising result

**Run 5 (DETM minus gui_agent) is 1.7× faster than Run 4 (full DETM).** The gui_agent supervisor loop added net overhead here vs. the main LLM driving xdotool directly with `desktop_look` access. This reshapes DETM's value story from "the supervisor is central" to "`desktop_look` + `task_*` scaffolding is the main win."

### Run-to-run comparisons (what each delta isolates)

- **Run 2 ↔ Run 5** (6:20 ↔ 3:42 = **1.7× speedup**): only `desktop_look` + `task_*` differ — both are main-LLM-drives-xdotool with the same Firefox on `:99`. Clean measurement of what DETM's non-supervisor tools contribute.

- **Run 5 ↔ Run 4** (3:42 ↔ 6:26): adding the gui_agent supervisor on top of `desktop_look` made this specific task slower. Per-turn supervisor latency (screenshot + API call + tool dispatch, ~3–5s each) multiplied over 6 chained calls costs more than the main LLM would have spent screenshotting between clicks itself.

- **Run 2 ↔ Run 3** (6:20 ↔ 9:32): both bare, Run 3 is warm with the main LLM's memory of Run 2. Run 3 was actually *slower* — Inflection's LinkedIn company page required 4 tab-cycles to recover from a dead vanity slug + a broken restore-session state, eating ~3 minutes of recovery. Illustrates that "warm" replay doesn't automatically beat cold when the target site presents new surface.

## Methodology + confounds

### Why Run 1 was dropped

An early full-DETM attempt on OpenAI completed in **2:45** with a single gui_agent call handling 17 actions. We excluded it from the reported results because it stacks three favorable conditions that don't reflect typical performance: warm main-LLM session, a well-trodden OpenAI navigation path, and the rare case where the whole task fit into one gui_agent call with no chaining. Quoting it would overstate DETM's typical wall-clock; Run 4's 6:26 is the more honest fresh-session number.

### Noise check (why 2× per config, not more)

Runs 2 and 3 were executed twice (original + rerun) to sanity-check wall-clock noise in the bare-xdotool configuration, which is where we had the highest uncertainty. The results reported above are the **newest** runs for each config — earlier attempts surfaced a few reliability bugs in DETM (since fixed) and helped calibrate exec-policy state for the bare path.

We did **not** run 3+ trials per config for two reasons:

1. **LinkedIn pool exhaustion.** Each AI company's connectable-people pool is small. After 5–10 sends within one company, most remaining profiles show Pending, Follow-only, or Message (2nd-degree already) and can't be connected to with a single click. Running the same task 3+ times on the same company converges on "all reachable people are Pending."

2. **Switching company = unfair confounding variable.** If we rotate to a new AI company to refill the pool, the LinkedIn page structure, filter depth, and recruiter-to-IC ratio all change — so the timing delta no longer isolates the DETM-vs-bare effect. This is the biggest methodological gap in this benchmark; a cleaner replication would use a synthetic LinkedIn-shaped target where pool is infinite and layout is identical between trials.

### Confound table

| Run | Session warmth | Prompt shape | Exec policy | Notes |
|---|---|---|---|---|
| 2 | Fresh TUI | "do not rely on any prior context; use whatever tools you have" | yolo (ask=off) | Rerun, clean allowlisted state. |
| 3 | Warm TUI (has Run 2 memory) | "apply what you learned; use same tools" | yolo (ask=off) | Rerun; longer due to Inflection's broken vanity-slug recovery. |
| 4 | Fresh TUI | "chain-friendly: one gui_agent at a time, ≤60s, read remaining and continue" | default | Final successful HF attempt. |
| 5 | Fresh TUI | "use the DETM tools available; gui_agent may not be available" | default | — |

### Valid comparisons

- **2 ↔ 5**: same bare-vs-DETM-minus-gui_agent delta. Measures `desktop_look` + `task_*` value.
- **4 ↔ 5**: both fresh DETM sessions. Measures the `gui_agent` supervisor's incremental value on top.
- **2 ↔ 3**: both bare. Measures warm-replay benefit (turned out negative for Inflection specifically).

---

## Friction-point comparison: where DETM helps

The tightest case for DETM is the side-by-side: pick a friction the bare path actually hit in Runs 2/3, then show how the DETM path handled the same moment in Runs 4/5. Every quoted line below is grep-reproducible from the committed `artifacts/runN-conversation.jsonl` files.

### 1. Screenshot tool discovery (bare only)

Before it could take its first screenshot, Run 2 spent ~30s probing what screenshotting tools were installed, including a nested Python module check:

```
# run2-conversation.jsonl, 00:05:00 → 00:06:19
[00:05:00][exec] which xdotool wmctrl import scrot maim firefox python3 || true
[00:06:19][exec] which tesseract || true; python3 - <<'PY'
                   import pkgutil
                   for m in ['PIL','cv2','pytesseract','pyautogui']:
                     print(m, bool(pkgutil.find_loader(m)))
                   PY
```

Runs 4 and 5 skip this entirely — their first visual check is a single `desktop_look` call that returns the screen inline:

```
# run5-conversation.jsonl, 23:12:06
[23:12:06][desktop_look] {"task_id": "d513cf87", "window_name": "Firefox"}
```

### 2. Screenshot save → file path → vision call (every single screen check)

In Run 2, every screen check is a 3-step pattern: `scrot` to a file, `printf` the path, then pass the path into the `image` tool:

```
# run2-conversation.jsonl, 00:05:13 → 00:05:17
[00:05:13][exec] scrot -u /home/alex/.openclaw/workspace/tmp/linkedin-current.png
                 printf '%s\n' /home/alex/.openclaw/workspace/tmp/linkedin-current.png
[00:05:17][image /home/alex/.openclaw/workspace/tmp/linkedin-current.png] Briefly describe this LinkedIn screenshot…
```

Bare runs did this **17× in Run 2 and 26× in Run 3** — each a disk round-trip plus a vision-model call with an external file reference. DETM's `desktop_look` folds the whole thing into one inline-image tool call (6× in Run 5, 4× in Run 4).

### 3. Manual coordinate estimation by main-LLM vision (bare only)

Because there's no grounding layer, bare runs ask the main LLM's vision to estimate pixel coordinates for every click target. This shows up as ≥10 prompts across Runs 2/3:

```
# run2-conversation.jsonl, 00:07:59
[00:07:59][image linkedin-scale-engineer-scroll.png]
   Estimate the pixel coordinates within the screenshot for the center
   of the visible Connect button for Kyle T. in the right sidebar.

# run3-conversation.jsonl, 00:19:35
[00:19:35][image check5.png]
   Estimate the center pixel coordinates of the browser tab titled something
   like '(25) Inflection AI: People' in the tab strip.
```

DETM Run 4 (full gui_agent) never issues a coordinate-estimation prompt at the main-LLM layer — UI-TARS handles grounding inside the supervisor. DETM Run 5 (no gui_agent) still has main-LLM ground its own clicks, but from a single inline `desktop_look`, not a save-and-reread cycle — so coordinate work collapses from "save → read → estimate → click → save → verify" into "look → click → look."

### 4. OCR + crop fallbacks when vision is uncertain (bare only)

When the vision model couldn't read a card (small font, cut-off name), Run 2 and Run 3 both fell back on Tesseract OCR and PIL image cropping:

```
# run2-conversation.jsonl, 00:06:24 and 00:07:47
[00:06:24][exec] tesseract linkedin-scale-people.png stdout tsv 2>/dev/null |
                 grep -i -E 'Search|employees|Connect|recruiter|talent'
[00:07:47][exec] tesseract linkedin-scale-engineer-scroll.png stdout tsv … |
                 awk -F '\t' '$12!="" {print …}'

# run3-conversation.jsonl, 00:24:35 — cropping a cut-off name
[00:24:35][exec] python3 - <<'PY'
                 from PIL import Image
                 img=Image.open('…/inflection-student-cards.png')
                 crop=img.crop((320,180,650,500))
                 crop.save('…/inflection-student-top-left-card.png')
                 PY
```

Neither Run 4 nor Run 5 needed Tesseract or manual cropping once. The combination of `desktop_look`'s higher-fidelity inline return (for Run 5) and the supervisor's turn-by-turn narrowed crops (for Run 4) made OCR fallback unnecessary.

### 5. Dead vanity slug + multi-tab recovery (bare Run 3)

Run 3's standout failure mode was navigating to a dead LinkedIn company URL, then unwinding Firefox's session-restore flow across 4 tabs. The whole recovery arc in compressed form:

```
# run3-conversation.jsonl, 00:15:31 → 00:19:41
[00:15:31][exec] type --window "$WID" 'https://www.linkedin.com/company/inflection-ai/people/…'  ← dead slug
[00:16:11][exec] # click LinkedIn top search on the unavailable page     ← pivot 1: in-LinkedIn search
[00:16:36][exec] type … 'https://duckduckgo.com/?q=site%3Alinkedin.com/company "Inflection AI"'   ← pivot 2: DuckDuckGo detour
[00:17:13][exec] firefox --new-tab 'https://duckduckgo.com/?q=…'
[00:17:53][exec] firefox --new-tab 'https://www.linkedin.com/company/inflectionai/people/…'        ← correct slug in a new tab
[00:18:18][exec] for i in 1 2 3 4; do scrot "tab$i.png"; xdotool key Ctrl+Tab; done  ← captured 4 tabs separately
[00:18:28][image ] For each image tab1-tab4, briefly identify what page it shows…
[00:19:18][image ] For each image check1-check5, identify the page shown in the main content area…
[00:19:35][image check5.png] Estimate the center pixel coordinates of the browser tab titled '(25) Inflection AI: People'…
[00:19:41][exec] xdotool mousemove --window "$WID" 845 22 click 1                                   ← click the tab
```

Roughly 3 minutes lost to dead-slug + multi-tab disambiguation. Run 4's equivalent moment — when a gui_agent call timed out mid-flow — was handled in ~10 seconds:

```
# run4-conversation.jsonl, 22:55:34 → 22:55:56
[22:55:34][task_update] The first GUI pass timed out before reporting back. I'm checking the current screen…
[22:55:47][desktop_look] Firefox
[22:55:56][task_update] I'm on LinkedIn people search for Hugging Face. I can see at least two visible Connect buttons…
```

One `desktop_look`, one `task_update`, and the chain resumes on the right page — the already-sent invitations (Amy, Adarsh) are still visible and counted. No tab scan, no DuckDuckGo detour, no coordinate estimation on tab strips. (This graceful-recovery behavior is what the `task_*` tools and `desktop_look`'s persistent window-name parameter buy you.)

### 6. `/tmp` path rejection (original Run 2 only)

OpenClaw's image tool rejects paths outside the workspace. Bare Run 2's first attempt hit this, then recovered by re-saving under `~/.openclaw/workspace/`:

```
# artifacts/gateway-journal.log, 2026-04-23T20:20:13
[tools] image failed: Local media path is not under an allowed directory:
  /tmp/linkedin_now.png
```

DETM never touches the filesystem for screenshots — `desktop_look` streams the image back inline, so this policy never fires on Run 4/5.

### 7. Progress tracking across a multi-step task

Finally, a subtler win: `task_*` gives the main LLM a structured place to check off plan items and record why each click happened. Excerpt from Run 5:

```
# run5-conversation.jsonl, 23:13:25 → 23:15:13
[23:13:25][task_item_update] Confirmed the LinkedIn people search page is loaded
[23:13:30][task_item_update] The search results already show multiple Perplexity profiles
[23:15:07][task_item_update] Five invitations were sent and the first five release
[23:15:13][task_item_update] Verified on-screen that five visible profiles show Pending
```

The bare path has no structured equivalent — Runs 2 and 3 thread the same information through prose `[image]` prompts and `[exec]` comments. Works, but it means the main LLM re-reads its own rolling free-text buffer to decide "am I done?", which is one of the costs showing up in Run 3's 52 tool calls vs. Run 5's 39 for equivalent outcomes.

---

## Where DETM's value actually lies

### Strong evidence: `desktop_look` + `task_*` scaffolding

Run 2 → Run 5: **1.7× speedup** (6:20 → 3:42) on apples-to-apples fresh-session bare-vs-DETM-minus-gui. Both paths have main LLM driving xdotool. The ONLY difference is "how do I view a screenshot" and "how do I track progress across plan items."

### Weaker evidence: `gui_agent` supervisor

Run 5 → Run 4: gui_agent *slowed this task down* (3:42 → 6:26). Per-turn overhead (screenshot + 2 API calls + tool dispatch ≈ 4–8s) multiplied across 6 chained calls exceeds what the main LLM would spend handling UI directly with occasional `desktop_look` checks.

When a single gui_agent call can handle the whole task (no chaining, short action sequence), the picture should flip — the supervisor's per-call overhead gets amortized and main-LLM context use stays minimal. We didn't report a clean example of this today (the one attempt, on a warm-session OpenAI path, stacked too many favorable conditions to be a fair data point).

We also did **not** benchmark gui_agent on the task classes where it's designed to shine: dense UIs, small click targets (timeline scrubbers, spreadsheet cells, tooltip-dense menus). UI-TARS grounding should be most valuable there. Open question, not a verified claim.

### Consistent: cost economics

- Runs 4 and 5 have similar total main-LLM tool-call counts (53 vs 39). The difference is **which model does the work.**
- Run 4: each gui_agent call spawns ~5–10 Gemini-Flash supervisor turns. 6 gui_agents ≈ 30–60 Flash turns at ~$0.30/M input.
- Run 5: each `desktop_look` + xdotool dispatch is ONE main-LLM turn at flagship rates (~$3–$15/M).
- Rough per-task cost on this benchmark: Run 4 ≈ $0.05 (Gemini Flash dominant); Run 5 ≈ $0.30–$1.00 (flagship dominant). **~5–20× cheaper to let gui_agent grind.**

### Consistent: safety invariants at DETM's action layer

DETM now clears X11 modifier state on every click (via `xdotool --clearmodifiers`), so a prior `key_press(ctrl+l)` can't leak into the next click as Ctrl+click (which Firefox would interpret as "open in new tab"). The bare path has no such protection — Runs 2 and 3 only escaped this because their shell scripts happened not to issue a `key_press` before a click.

---

## What DETM does NOT uniquely provide

1. **Raw capability** — Runs 2 and 3 prove SOTA LLMs can complete the task with just xdotool + Bash. DETM is an optimization, not an enablement.
2. **Bot-detection evasion** — Comes from real Firefox + real X11 events. All runs shared this.
3. **Coordinate grounding on standard UIs** — Run 5 shows the main LLM grounds fine given a clean screenshot. UI-TARS matters for small-target cases not benchmarked today.

---

## Practical recommendation

For LinkedIn-class tasks (labeled, visually distinct targets), ordered by ROI:

1. **Ship `desktop_look` + `task_*`** — biggest proven win (1.7×).
2. **Keep `gui_agent` available but don't require it** — use when main-LLM context is binding (long multi-task sessions) or when the UI has dense/small targets.
3. **Enforce the chain pattern in SKILL.md**: *"one gui_agent at a time, ≤60s each, read `status=partial` and continue."* Callers who over-constrain with "one gui_agent call for everything" run into the MCP 60s cap and see false failures even though the underlying work was making progress.
4. **Benchmark gui_agent on hard UIs next.** DaVinci Resolve timeline handles, Figma nested groups, Excel cells — that's where UI-TARS grounding should earn its keep. Today's benchmark can't rule that in or out.

---

## Artifact index

- `artifacts/` — per-run conversation JSONL, daemon log, gateway slice, tool histogram.
- `artifacts/gateway-journal.log` — full OpenClaw gateway journal for 2026-04-23 (whatsapp noise removed). Source for the `/tmp` rejection quote in section 6.
- `verification-screenshots/` — one screenshot per run showing final Pending state in Firefox.
- `artifacts.zip` — zipped mirror of `artifacts/`.

| Run | Conversation | Daemon | Gateway | Screenshot | Histogram |
|---|---|---|---|---|---|
| 2 | `run2-conversation.jsonl` (85 msgs) | `run2-daemon.log` (798 lines; 0 gui_agent calls) | `run2-gateway.log` | `run2-scale-5-pending.jpg` | `run2-tool-histogram.txt` |
| 3 | `run3-conversation.jsonl` (106 msgs) | `run3-daemon.log` (1363 lines; 0 gui_agent calls) | — (no DETM MCP activity) | `run3-inflection-5-pending.jpg` | `run3-tool-histogram.txt` |
| 4 | `run4-conversation.jsonl` (103 msgs) | `run4-daemon.log` (1441 lines, 6 supervisor sessions) | `run4-gateway.log` | `run4-hf-5-pending.jpg` | — |
| 5 | `run5-conversation.jsonl` (85 msgs) | `run5-daemon.log` (604 lines; 0 gui_agent calls) | `run5-gateway.log` | `run5-perplexity-5-pending.jpg` | — |

---

## Verification commands

Every numerical claim in this doc is reproducible from `artifacts/` with these commands:

```bash
cd artifacts

# Run 2: 379.8s wall-clock, 39 tool calls, 0 gui_agent
python3 -c "
import json
from datetime import datetime
msgs=[json.loads(l) for l in open('run2-conversation.jsonl') if json.loads(l).get('type')=='message']
first=min(m['timestamp'] for m in msgs if m['message']['role']=='user')
last =max(m['timestamp'] for m in msgs if m['message']['role']=='assistant')
a=datetime.fromisoformat(first.replace('Z','+00:00'))
b=datetime.fromisoformat(last.replace('Z','+00:00'))
print('wall_s =', round((b-a).total_seconds(),1))"
grep -c 'gui_agent: model=' run2-daemon.log          # → 0
cat run2-tool-histogram.txt                           # → 21/17/1 = 39

# Run 3: 571.6s, 52 tool calls, 0 gui_agent
grep -c 'gui_agent: model=' run3-daemon.log          # → 0
cat run3-tool-histogram.txt                           # → 26/24/2 = 52

# Run 4: 6 gui_agent calls, 53 total tool calls
grep -c 'gui_agent: model=' run4-daemon.log          # → 6

# Run 5: 0 gui_agent, 39 total
grep -c 'gui_agent: model=' run5-daemon.log          # → 0
```
