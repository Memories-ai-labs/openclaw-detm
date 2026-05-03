# gpt-5.4 → Replicate

**Status:** PARTIAL — 4/5 verified
**Wall-clock:** 17m 18s (1038s)
**Verified Pending:** Rena L., Gandalf Hernandez, Keera Kirkpatrick-Silberman, ZhiZhen (Lilibet) Z.
**Cost:** ~$0.80 estimate

## What worked

gpt-5.4 broke the action-loop pattern that killed sonnet-4.6, holo3-35b, holo3-122b, and gemini-3.1-pro. It actually navigated LinkedIn, sent connection requests, verified Pending state, and recognized blockers:

> "Nikhil Sinha, LinkedIn said 'Invitation not sent'"
> "Aron C., invite required an email"
> "many others were already Pending, View, or Follow only"

It stopped honestly at 4 instead of forging ahead and falsely claiming 5/5.

## Comparison: why gpt-5.4 succeeded where sonnet-4.6 failed

Both use the same `direct.py` provider, same OpenClaw chain pattern, same 60s gui_agent cap. The differences must be model-side:

- **gpt-5.4**: didn't loop on click misses; recognized "Invitation not sent" failure modes; stopped honestly
- **sonnet-4.6**: 6 consecutive identical clicks at (740, 120), subtly-rephrased thoughts on each, never broke the loop

It's not raw speed (both are similar latency). It's not OSWorld score either (sonnet 4.6 scores higher than gpt-5.4 on most desktop benchmarks). It seems to be **how each model interprets "screen looks unchanged after my action"**:

- gpt-5.4: "the click didn't have effect — let me try a different target"
- sonnet-4.6: "I see the dropdown — let me click it again" (loops)

This is consistent with the supervised pipeline finding: even though sonnet-4.6 has chat-history awareness, in practice it doesn't always *use* the history to break loops. gpt-5.4's training distribution may include more "verify your action worked" patterns.

## Caveats

- Two of the 4/5 were sent via LinkedIn search-result Connect buttons; one via profile-page Connect.
- The model did about 80 main-LLM tool calls (chain pattern dispatching gui_agent + desktop_look + task_*).
- No MCP timeouts — sessions stayed under 60s each.
