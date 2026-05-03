# claude-sonnet-4.6 → Stability AI

**Status:** FAILED — MCP timeout cascade
**Wall-clock:** 9m 44s (584s)
**Verified Pending:** 0/5
**MCP timeouts:** 3 `gui_agent failed: MCP error -32001`
**Cost:** ~$0.50 estimate

## Trace summary

- 5 distinct gui_agent sessions: 2 long ones (47 actions each), 3 short
- Final main-agent message: *"DETM calls started failing with MCP error -32001"*
- Sonnet 4.6 was actually making progress mid-run (47 actions = fast per-turn) before MCP killed sessions

## Diagnostic

Sonnet 4.6 turns out to be **fast enough per turn** for the chain pattern. The 47-action sessions show ~1.3s per turn, comparable to Holo3. But OpenClaw's MCP layer still cuts gui_agent calls at ~60s — Sonnet's session went past that, OpenClaw said "request timed out", main agent saw the failure and gave up.

This is the same bug we documented in the original benchmark (bug #2 in the experiments/detm-vs-bare-2026-04-23 README): gui_agent MCP timeout (60s) < internal supervisor budget. Supposedly we capped daemon-side at 240s, but the OpenClaw caller still aborts at ~60s.

So Sonnet 4.6 isn't structurally broken — it's hitting the existing caller-side timeout. With a higher MCP cap on the OpenClaw side (or shorter gui_agent budgets per call) it would likely work. Needs investigation.
