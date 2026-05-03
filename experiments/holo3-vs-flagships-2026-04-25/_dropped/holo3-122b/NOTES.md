# holo3-122b → Cursor

**Status:** FAILED — action-loop pathology + tab confusion
**Wall-clock:** 8m 03s (483s)
**Verified Pending in this run:** 0
**Cost:** ~$0.10–0.20 estimate (Holo3-122B at $0.40/$3.00 per M)

## Why it failed

Holo3-122B is fast per turn (~1.4–2s) — speed isn't the problem. The problem is **lack of self-correction inside the loop.**

### The action-loop pathology

A typical chained gui_agent call (session `89bb339f`):
```
action#1   click(311, 148)         ← LinkedIn search bar
action#2   type_text("Cursor")
action#3   type_text("Cursor")     ← repeat — model didn't notice the first one didn't appear
action#4   type_text("Cursor")
action#5   type_text("Cursor")
...
action#21  click(312, 148)         ← clicked search bar 5× in a row
action#22  click(312, 148)
action#23  click(312, 148)
action#24  click(312, 148)
action#25  click(312, 148)
hard timeout after 60s — never produced new behavior
```

The model emits an action, the screen doesn't change visibly (because the click missed, focus shifted, or the search bar dropdown intercepted), and Holo3 just **hammers the same intent again**. It has reasoning content per turn — but the reasoning is "I need to click the search bar to focus it" repeated, not diagnostic.

### Compare with Gemini 3.1 Pro

Gemini-3.1-Pro at least *recognized the problem*:
> "I'm tracing a recent click sequence, specifically one that unexpectedly led me to Felix Wu's profile page instead of Haoran Li's Connect button. The coordinates don't align."

It still failed (because of speed), but the model knew something was off and tried to course-correct. Holo3-122B doesn't even try — it loops.

## Final main-agent message

> "I got to LinkedIn people results for 'Cursor' and confirmed connectable profiles were visible, but the DETM `gui_agent` was too unreliable to finish safely. It repeatedly timed out, switched to the wrong tabs, and navigated away from the intended rows, so I could not verify 5 requests."

## Real finding

OSWorld score doesn't predict robustness in our setup. Holo3-122B is the highest-scoring model on OSWorld-Verified (78.85%) but fails our LinkedIn benchmark not because of grounding accuracy but because:

1. **Holo3 is a stateless-per-call model by training** — each /chat/completions request is a fresh "(task, screenshot) → action" decision. Across our turns, we feed back action results and updated screenshots, but Holo3's training distribution probably doesn't cover "I just emitted X and the result was no change — do something different."

2. **Browser state confusion** — the LinkedIn search bar autocomplete dropdown probably intercepted the first click and the model can't distinguish "search field focused with dropdown open" from "search field unfocused, need to click again."

3. **Stateless-per-turn assumption breaks down for compounding errors** — without a planner-style supervisor (which is what `supervised` backend has via Gemini Flash), the action loop has no escape mechanism.

## Implication

The single-model direct-action backends (Holo3 native, OpenRouter direct) work great for **simple tasks** (Firefox URL probe earlier worked perfectly in 3.3s) but degrade on **anything that requires noticing your last action didn't produce the expected outcome**. The supervised pipeline's planner-grounder split, while slower per turn, has more recovery affordance because the planner can reason "the cursor is still on the wrong place" and explicitly retry with a hint.

This may be the actual lesson from the bake-off: direct-action backends are good for trivial GUI tasks; chain-driven supervised pipelines are better for task chains that need recovery.

## What to try next

1. Continue the bake-off (gpt-5.4, gpt-5.5) to see if any model emits a self-correcting trace
2. Re-run holo3-122b against Cursor from a clean Firefox state (no leftover Felix Wu profile in the session) — this run inherited messy state from the H3 chaos
3. Compare against the supervised baseline on the same target — if supervised completes 5/5, that's strong evidence the planner-grounder split matters
