# AI Agent Landscape Comparison — February 2026

> **Snapshot from February 2026.** The "Our System" column describes DETM as it was then: supervised backend (Gemini Flash + UI-TARS-7B). DETM's production path has since changed to **bash + `openai/gpt-5.4`** (May 2026 bake-off). See `docs/GUI-AGENT-BACKENDS.md` for current state.

## TL;DR Table

| | **Perplexity Comet** | **OpenClaw + Browser Plugin** | **ChatGPT Atlas** | **Claude Cowork** | **Manus AI** | **Our System** |
|---|---|---|---|---|---|---|
| **Operating level** | Browser only | Browser (DOM) | Browser only | Local desktop + VM sandbox | Cloud VM (system-level) | Full desktop OS — headless Linux + Xvfb |
| **Browser control** | Accessibility tree (primary), pixel coords (fallback) | DOM / HTML / JS execution via MCP | Visual/screenshots (CUA) + tight IPC page context | Accessibility tree + text extraction + screenshots | DOM / HTML parsing via Browser Use, screenshot available | Visual grounding (UI-TARS) + iterative narrowing. Browser MCP gives DOM access too. |
| **Desktop apps** | No | No | No | Limited (within VM, no native apps) | Via cloud VM shell | Yes — any X11 app via xdotool |
| **Failure recovery** | Pauses for user; no auto-retry | Limited — no task state | Pauses on sensitive sites; no auto-retry | Sub-agent isolation; hard stop on 5-hr rate limit | 3-attempt retry + strategy change + intermediate saves; hard stop on credit depletion | Task hierarchy + stuck detection + resume packets + iterative narrowing fallback |

## Dimension Comparison

### Operating Level

```
System-level (cloud)  │  Manus AI
──────────────────────┤
System-level (local)  │  Our System (headless Linux, any X11 app)
                      │  Claude Cowork (VM sandbox, local files + browser)
──────────────────────┤
Browser + desktop     │  (nothing here yet)
──────────────────────┤
Browser only          │  Perplexity Comet
                      │  ChatGPT Atlas
                      │  OpenClaw + browser plugin (without DETM)
```

### Browser Control Architecture

| System | Primary mechanism | Fallback | JS execution |
|---|---|---|---|
| Perplexity Comet | Accessibility tree (CDP `getFullAXTree`) | Pixel coordinate click | No |
| OpenClaw + browser MCP | HTML / DOM selectors | Screenshot | Yes |
| ChatGPT Atlas | Visual / screenshots (CUA) | — | No |
| Claude Cowork | Accessibility tree (`read_page`) | Screenshots | Yes (via extension) |
| Manus AI | DOM / HTML parsing (Browser Use) | Screenshots | Yes (Playwright code) |
| Our System (`gui_agent`) | Visual grounding (UI-TARS, iterative narrowing + MVP) | Falls back through passes | No (use browser MCP for JS) |

**Rule of thumb:**
- Accessibility tree = fast, reliable on standard HTML, breaks on unlabeled/canvas elements
- DOM/HTML = most robust for well-structured pages, breaks on heavily obfuscated or canvas-heavy UIs
- Visual (CUA / UI-TARS) = works on anything visual regardless of DOM structure, slower, best for native apps

### Failure Recovery

| System | Strategy | Hard limit |
|---|---|---|
| Perplexity Comet | Pause for user confirmation | Session end |
| OpenClaw + browser MCP | Minimal | Context window |
| ChatGPT Atlas | Pause on sensitive sites | Session end |
| Claude Cowork | Sub-agent isolation | 5-hour rate limit window |
| Manus AI | 3-attempt retry + strategy change + file saves | Credit depletion |
| Our System | Task hierarchy + stuck detection + resume packets + narrowing fallback | None (self-resumes) |

### Proactiveness

| System | Wakes itself | Background tasks | Schedules | Multi-concurrent |
|---|---|---|---|---|
| Perplexity Comet | No | Yes (Max tier) | Yes (Max tier) | Yes (background assistant) |
| OpenClaw + browser MCP | Yes (heartbeat) | No | No | No |
| ChatGPT Atlas | No | No | No | No |
| Claude Cowork | No | No | No | No |
| Manus AI | No (initiated by user) | Yes (runs offline) | Yes | Yes (up to 20) |
| Our System | Yes (heartbeat + stuck detection) | Yes (smart_wait in background) | Via OpenClaw | Yes (shared display) |

### Task Scope

| | Web research | Form filling | Local files | Desktop apps | Native video editing | Code execution | Messaging |
|---|---|---|---|---|---|---|---|
| Perplexity Comet | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| OpenClaw + browser MCP | ✅ | ✅ | Partial | ❌ | ❌ | ✅ (shell) | ✅ (12+ platforms) |
| ChatGPT Atlas | ✅ | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Claude Cowork | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ (in VM) | ❌ |
| Manus AI | ✅ | ✅ | Cloud only | Cloud shell | ❌ | ✅ (cloud VM) | ❌ |
| **Our System** | ✅ | ✅ | ✅ | **✅** | **✅** | ✅ (shell) | ✅ (12+ platforms) |
