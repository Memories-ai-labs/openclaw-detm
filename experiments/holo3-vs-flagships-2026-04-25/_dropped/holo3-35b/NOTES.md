# H1 — Holo3-35B Together AI

**Status:** FAILED — rate limit
**Why:** Free-tier API for `holo3-35b-a3b` is capped at 10 RPM. The model emits actions roughly every 1.5–2s, so a multi-step LinkedIn task hits the cap within ~10 actions. The 429 response causes the gui_agent to error out, and the OpenClaw main agent retries / spirals.

**Daemon log signature** (in `daemon.log`):
```
[137e15a8] Holo3 API HTTP 429: {"error":{"message":"Too Many Requests for holo3-35b-a3b
  (tier: default, allowed RPM: 10). Please visit https://portal.hcompany.ai
  to add credits for higher tier access..."}}
```

**Options for re-running this config:**
1. Add credits to lift the 35B tier limit (per H Company portal)
2. Add 429 retry-with-backoff to `live_ui/holo3.py` so the agent waits 6+ seconds and retries
3. Fall back to `holo3-122b-a10b` (paid model — higher RPM, also better OSWorld score)

This run is documented to capture the rate-limit observation, not as a benchmark data point.
