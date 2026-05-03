# claude-opus-4.7 + bash → Runway

**Status:** SUCCESS — 5/5 verified
**Wall-clock:** 4m 44s (283.6s)
**Connections:** Dominik Schmidt, Gretchen Chen, Tarek Ayed, Lauren Saltus, Julia Martins
**Cost:** ~$0.30 estimate

## What opus-4.7 did differently from opus-4.6

opus-4.7 didn't loop. Where opus-4.6 clicked `(262, 154)` 5 times in a row, opus-4.7 walked down the list with **different y-coordinates** for each Connect button:

```
bash#3:  xdotool mousemove 1143 298 click 1; sleep 2
bash#4:  xdotool mousemove 1131 363 click 1; sleep 2     ← different row
bash#5:  xdotool mousemove 1143 402 click 1; sleep 2     ← different row
bash#6:  xdotool mousemove 1131 363 click 1; sleep 2     ← back to second row (modal verify)
```

It's also actively *adapting* mid-run when something failed. From the final summary:
> "I had to switch from the search results page because LinkedIn hit a profile search limit, but the Runway company People page worked."

That's the core behavior we wanted: notice failure, switch strategy, succeed.

## opus-4.6 vs opus-4.7 contrast

Same Anthropic family, same backend, same prompt, different outcome:

| | opus-4.6 + bash | opus-4.7 + bash |
|---|---|---|
| Wall-clock | 10m 42s | 4m 44s |
| Result | 0/5 (looped) | 5/5 |
| Click pattern | 5× identical coords (262, 154) | walked down list (298, 363, 402…) |

Could be the 4.7 model genuinely improved its self-correction. Could also be the target (Runway has a richer LinkedIn presence than Sakana). One run isn't enough to assert "4.7 fixed the loop"; would need to retest opus-4.7 against the same Sakana target to know for sure.

## Sample composition style — sophisticated but efficient

```
bash#1 (initial probe):
  wmctrl -a "Firefox" && sleep 0.3 && scrot /tmp/current.png && echo "focused"

bash#2 (tab navigation with verification):
  xdotool key --clearmodifiers ctrl+2 && sleep 0.8 && scrot /tmp/tab2.png

bash#1 (URL navigation):
  xdotool key --clearmodifiers ctrl+l; sleep 0.3;
  xdotool type --clearmodifiers "https://www.linkedin.com/search/results/people/?currentCompany=%5B%221081616%22%5D";
  xdotool key --clearmodifiers Return
```

opus-4.7 even constructed proper LinkedIn URL-encoded query strings (`currentCompany=%5B%221081616%22%5D` — that's the company-ID filter format LinkedIn uses internally). That's task-specific knowledge the model brought without prompting.
