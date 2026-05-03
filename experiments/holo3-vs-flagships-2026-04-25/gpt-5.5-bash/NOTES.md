# gpt-5.5 + bash → Together AI

**Status:** PARTIAL — 2/5 verified
**Wall-clock:** 17m 1s (1021s)
**Verified Pending:** Soroush Bassam, Tianyi Zhang
**Cost:** ~$1.50

## Why slower than gpt-5.4 + bash on the same backend

gpt-5.5 has a different bash style. Where gpt-5.4 wrote efficient xdotool sequences, gpt-5.5 wrote elaborate Python and JavaScript-via-address-bar tricks:

```
bash#3: xdotool key ctrl+l; xdotool type "javascript:document.title='BTN '+
        Array.from(document.querySelectorAll('button')).map(b=>b.innerText)..."
```

It was trying to enumerate page buttons by injecting JavaScript that rewrites the document title — clever, but:
1. Slower per call
2. Increases the chance of perturbing Firefox state in unexpected ways
3. Doesn't help when most LinkedIn cards are already visible in the screenshot

Sample of single-action calls (no composition):
```
bash#1: xdotool --clearmodifiers key Ctrl+4    (single key, did nothing)
bash#2: xdotool key --clearmodifiers Ctrl+4    (repeat)
bash#3: xdotool key --clearmodifiers Ctrl+4    (repeat — same loop pathology)
```

## Pattern emerging

Among the bash-backend runs:
- gpt-5.4: aggressive composition → 5/5
- gpt-5.5: overengineered Python/JS approach → 2/5
- sonnet-4.6: cautious diagnostics → 0/5

The model's *bash style* matters more than raw capability for this task. Models that compose efficient action sequences fit more useful work in 60s gui_agent windows. Models that probe/diagnose/overthink each step run out of clock.

## Final main-agent message

> "I got 2 verified requests through. Verified Pending: Soroush Bassam, Tianyi Zhang. Blocker: The DETM gui_agent became unreliable on the remaining results, repeatedly timing out and occasionally perturbing Firefox state, so I could not honestly verify 5 landed."
