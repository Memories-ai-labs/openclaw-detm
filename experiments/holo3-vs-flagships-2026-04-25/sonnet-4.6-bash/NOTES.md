# claude-sonnet-4.6 + bash → Stability AI

**Status:** FAILED — MCP timeout cascade
**Wall-clock:** 12m 38s (758s)
**Verified Pending:** 0/5
**bash calls:** 62 across multiple gui_agent dispatches

## Difference from sonnet-4.6 + direct.py (also 0/5)

direct.py: looped on `click(740,120)` 6 times — action-loop pathology
bash: didn't loop. Got to filtered Stability AI search results, identified 3 connectable candidates (Amirreza Kazemi, Chun-Han Yao, Vahid Zehtab as Follow-only). Then MCP timeouts started.

So bash backend fixed the loop pathology, but sonnet's conservative bash style (one action per call + diagnostic checks) made each gui_agent call slow enough to hit the 60s MCP cap repeatedly.

## sonnet vs gpt-5.4 — what the bash composition style looked like

gpt-5.4 (succeeded 5/5):
```
xdotool key --clearmodifiers Alt+Left; sleep 2
xdotool mousemove --sync 1195 201 click; sleep 1; xdotool mousemove 1144 878 click; sleep 2
```

sonnet-4.6 (failed 0/5):
```
xdotool click --clearmodifiers 380 509
xdotool getwindowfocus && xdotool getmouselocation     # diagnostic only
xdotool key --clearmodifiers ctrl+Home; sleep 0.5
```

sonnet was more cautious — verifying with `getwindowfocus` and `getmouselocation` between actions, doing one action per bash call. That's defensible behavior but means fewer actions fit in the 60s gui_agent budget.

## Implication

Bash backend isn't a magic fix — it depends on the model's composition style. gpt-5.4 naturally writes efficient shell sequences; sonnet-4.6 writes cautious ones. The MCP 60s cap punishes cautious models.

A bigger gui_agent budget cap would likely help sonnet.

## Final main-agent message

> "I got Firefox to the LinkedIn people results filtered to Current company: Stability AI, with these visible:
>  - Amirreza Kazemi: Connect
>  - Vahid Zehtab: Follow only
>  - Chun-Han Yao: Connect
> But after that, gui_agent/desktop_look started timing out repeatedly, so I could not verify any sends landed. Confirmed sent: 0/5."
