# gpt-5.4 + bash backend → Cognition AI

**Status:** SUCCESS — 5/5 verified
**Wall-clock:** 3m 32s (211.8s)
**Connections:** Ashiq Sazid, Aniket Nemade, Aman Prakash, Hasan Noor, Owen Hochwald
**Cost:** ~$0.20 estimate (much lower than direct.py because fewer turns)

## Why this worked when direct.py only got 4/5 in 17m

The backend changed. Same model, same task shape, dramatically different outcome:

| | direct.py (structured `click(x,y)` + `type_text`) | bash backend (raw `xdotool` via `bash(command)`) |
|---|---|---|
| Wall-clock | 17m 18s | **3m 32s** |
| Result | 4/5 | **5/5** |
| Tool result feedback | `"ok"` | real exit_code/stdout/stderr |
| Multi-step composition | one tool per turn | many actions per bash call |
| Schema fights | `{x:[a,b]}` coercion etc. | none — model writes shell directly |

## What gpt-5.4 did naturally with bash

It wrote shell sequences that match how a human would automate the task:

```
bash#2: xdotool key --clearmodifiers Alt+Left; sleep 2
bash#3: xdotool mousemove --sync 1144 878 click --clearmodifiers 1; sleep 2
bash#4: xdotool mousemove --sync 1195 201 click --clearmodifiers 1; sleep 1; \
        xdotool mousemove --sync 1144 878 click --clearmodifiers 1; sleep 2
```

Notice it picked up `--clearmodifiers` from the one-line system-prompt hint and used it on every click and key. It also used `--sync` for mousemove (good practice we didn't even mention). Plus it composed multi-step sequences instead of one-action-per-turn — fits more useful work in each gui_agent call's 60s budget.

## Why it switched to one-at-a-time mid-run

Final main-agent message:
> "There was some page drift during the run, so I switched to one-at-a-time gui_agent steps to keep verification reliable."

The main agent (gpt-5.4 in OpenClaw) noticed something was drifting (probably the LinkedIn page state changed unpredictably between bash batches), and adjusted strategy: smaller bash calls, more verification between. This is the recovery behavior we wanted — the model self-correcting based on real feedback (stdout / actual screenshot diffs), which the direct.py "ok" feedback couldn't trigger.

## Implications

The user's hypothesis was right: **frontier LLMs are good enough at GUI use that the structured wrapper was hurting more than helping**. By matching their training distribution (shell + screenshots + freeform composition), we got 4-5× better wall-clock and the difference between partial and complete success.
