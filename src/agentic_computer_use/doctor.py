"""
DETM Doctor — end-to-end diagnostic of the DETM install.

Usage:
    python -m agentic_computer_use.doctor              # human-readable
    python -m agentic_computer_use.doctor --json       # machine-readable
    python -m agentic_computer_use.doctor --quiet      # only failures

Exit codes:
    0 = all green
    1 = warnings only
    2 = at least one failure

Also importable: `from agentic_computer_use.doctor import run_diagnostics`
returns a list[CheckResult] so the daemon's /health endpoint (or the MCP
`health_check` tool) can wrap it without shelling out.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Any

import httpx

from . import config

# ── Color helpers (auto-disabled when stdout is not a TTY) ──────
_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None
def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOR else text
def _green(s: str) -> str: return _c("32", s)
def _yellow(s: str) -> str: return _c("33", s)
def _red(s: str) -> str: return _c("31", s)
def _dim(s: str) -> str: return _c("2", s)
def _bold(s: str) -> str: return _c("1", s)

OK = "ok"
WARN = "warn"
FAIL = "fail"
SKIP = "skip"

DAEMON_URL = "http://127.0.0.1:18790"
VNC_PORT = 5901
NOVNC_PORT = 6080
GATEWAY_PORT = 18789
OPENCLAW_CONFIG = Path.home() / ".openclaw" / "openclaw.json"
SYSTEMD_UNIT = Path("/etc/systemd/system/detm-daemon.service")


@dataclass
class CheckResult:
    section: str
    name: str
    status: str            # ok | warn | fail | skip
    detail: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def badge(self) -> str:
        if self.status == OK:   return _green("[ OK ]")
        if self.status == WARN: return _yellow("[WARN]")
        if self.status == FAIL: return _red("[FAIL]")
        return _dim("[SKIP]")


# ── Low-level helpers ───────────────────────────────────────────

def _systemd_is_active(unit: str) -> tuple[bool, str]:
    try:
        r = subprocess.run(
            ["systemctl", "is-active", unit],
            capture_output=True, text=True, timeout=3,
        )
        return r.returncode == 0, r.stdout.strip() or r.stderr.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return False, str(e)


def _port_listening(host: str, port: int, timeout: float = 0.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _read_unit_env(unit_path: Path) -> dict[str, str]:
    """Parse `Environment=KEY=VAL` lines from a systemd unit file."""
    env: dict[str, str] = {}
    try:
        for line in unit_path.read_text().splitlines():
            line = line.strip()
            if line.startswith("Environment="):
                kv = line[len("Environment="):]
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    env[k] = v
    except (OSError, UnicodeDecodeError):
        pass
    return env


def _load_openclaw_config() -> dict | None:
    try:
        return json.loads(OPENCLAW_CONFIG.read_text())
    except (OSError, json.JSONDecodeError):
        return None


# ── Individual checks ───────────────────────────────────────────

async def _check_daemon(client: httpx.AsyncClient) -> CheckResult:
    active, raw = _systemd_is_active("detm-daemon.service")
    try:
        t0 = time.monotonic()
        r = await client.get(f"{DAEMON_URL}/health", timeout=5.0)
        latency_ms = int((time.monotonic() - t0) * 1000)
        if r.status_code != 200:
            return CheckResult("daemon", "http /health", FAIL,
                               f"HTTP {r.status_code} (systemd: {raw})")
        body = r.json()
        status = OK if (active and body.get("daemon")) else WARN
        return CheckResult(
            "daemon", "http /health", status,
            f"systemd={raw}, latency={latency_ms}ms, display={body.get('display')}",
            extra={"health": body, "latency_ms": latency_ms},
        )
    except httpx.HTTPError as e:
        return CheckResult("daemon", "http /health", FAIL,
                           f"unreachable ({e.__class__.__name__}) — systemd: {raw}")


def _check_display() -> CheckResult:
    display = os.environ.get("DISPLAY", config.DISPLAY)
    try:
        r = subprocess.run(
            ["xdpyinfo", "-display", display],
            capture_output=True, text=True, timeout=3,
        )
    except FileNotFoundError:
        return CheckResult("display", f"xdpyinfo {display}", WARN,
                           "xdpyinfo not installed (install x11-utils)")
    except subprocess.TimeoutExpired:
        return CheckResult("display", f"xdpyinfo {display}", FAIL, "timed out")
    if r.returncode != 0:
        return CheckResult("display", f"xdpyinfo {display}", FAIL,
                           (r.stderr or r.stdout).strip().splitlines()[0] if (r.stderr or r.stdout) else "failed")
    dims = ""
    for line in r.stdout.splitlines():
        if "dimensions:" in line:
            dims = line.strip().split("dimensions:", 1)[1].strip().split()[0]
            break
    return CheckResult("display", f"xdpyinfo {display}", OK, f"dimensions={dims}")


def _check_service(unit: str, purpose: str) -> CheckResult:
    active, raw = _systemd_is_active(unit)
    if active:
        return CheckResult("services", unit, OK, purpose)
    # Distinguish "not installed" from "inactive"
    try:
        r = subprocess.run(
            ["systemctl", "list-unit-files", unit],
            capture_output=True, text=True, timeout=3,
        )
        if unit not in r.stdout:
            return CheckResult("services", unit, WARN, f"not installed ({purpose})")
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return CheckResult("services", unit, FAIL, f"{raw} ({purpose})")


def _check_port(label: str, host: str, port: int, required: bool = True) -> CheckResult:
    if _port_listening(host, port):
        return CheckResult("ports", f"{label} {host}:{port}", OK, "listening")
    status = FAIL if required else WARN
    return CheckResult("ports", f"{label} {host}:{port}", status, "not listening")


async def _check_dashboard(client: httpx.AsyncClient) -> CheckResult:
    try:
        r = await client.get(f"{DAEMON_URL}/dashboard", timeout=5.0)
        if r.status_code == 200 and "<html" in r.text.lower()[:2000]:
            return CheckResult("dashboard", "/dashboard", OK, "serving HTML")
        return CheckResult("dashboard", "/dashboard", WARN,
                           f"HTTP {r.status_code}, content={len(r.content)}B")
    except httpx.HTTPError as e:
        return CheckResult("dashboard", "/dashboard", FAIL, str(e))


async def _check_backend_openrouter(key: str) -> CheckResult:
    if not key:
        return CheckResult("backends", "openrouter", WARN, "no key configured")
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(
                "https://openrouter.ai/api/v1/auth/key",
                headers={"Authorization": f"Bearer {key}"},
            )
        if r.status_code == 200:
            data = r.json().get("data", {})
            limit = data.get("limit")
            usage = data.get("usage")
            detail = f"key valid"
            if limit is not None and usage is not None:
                remaining = (limit or 0) - (usage or 0)
                detail += f", credit remaining≈${remaining:.2f}"
            return CheckResult("backends", "openrouter", OK, detail,
                               extra={"usage": usage, "limit": limit})
        return CheckResult("backends", "openrouter", FAIL,
                           f"HTTP {r.status_code}: {r.text[:120]}")
    except httpx.HTTPError as e:
        return CheckResult("backends", "openrouter", FAIL, str(e))


async def _check_openrouter_model(key: str, model: str) -> CheckResult:
    """Verify a specific model id is currently served on OpenRouter.

    The /auth/key probe in `_check_backend_openrouter` only proves the key works.
    A model snapshot (e.g. `openai/gpt-5.4-20260305`) can be retired without the
    key changing, so we hit /models and check membership explicitly.
    """
    if not key:
        return CheckResult("backends", f"openrouter model {model}", WARN,
                           "no OPENROUTER_API_KEY — cannot probe model availability")
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.get(
                "https://openrouter.ai/api/v1/models",
                headers={"Authorization": f"Bearer {key}"},
            )
        if r.status_code != 200:
            return CheckResult("backends", f"openrouter model {model}", WARN,
                               f"models listing HTTP {r.status_code}")
        ids = {m.get("id", "") for m in (r.json().get("data") or [])}
        # OpenRouter prefix-matches dated snapshots (e.g. openai/gpt-5.4 → openai/gpt-5.4-YYYYMMDD)
        if model in ids:
            return CheckResult("backends", f"openrouter model {model}", OK, "served")
        prefix_hits = [i for i in ids if i.startswith(model)]
        if prefix_hits:
            return CheckResult("backends", f"openrouter model {model}", OK,
                               f"served via snapshot: {prefix_hits[0]}")
        return CheckResult("backends", f"openrouter model {model}", FAIL,
                           f"model id not found in {len(ids)} listed (retired?)")
    except httpx.HTTPError as e:
        return CheckResult("backends", f"openrouter model {model}", FAIL, str(e))


def _capture_thumbnail_b64(display: str, max_w: int = 320, timeout_s: float = 1.5) -> str | None:
    """Capture a small JPEG thumbnail from `display`. No trace left behind.

    Uses scrot → temp file → PIL resize → base64 → unlink. No logging, no session,
    no DB write. Returns None on any failure (caller treats as a smoke-test fail).
    """
    import base64
    import io
    import tempfile
    fd, path = tempfile.mkstemp(suffix=".png", prefix=".acu_smoke_")
    try:
        os.close(fd)
        env = {**os.environ, "DISPLAY": display}
        r = subprocess.run(
            ["scrot", "--overwrite", path],
            env=env, capture_output=True, timeout=timeout_s,
        )
        if r.returncode != 0:
            return None
        try:
            from PIL import Image  # type: ignore
            with Image.open(path) as img:
                img.thumbnail((max_w, max_w))
                buf = io.BytesIO()
                img.convert("RGB").save(buf, format="JPEG", quality=55)
                return base64.b64encode(buf.getvalue()).decode("ascii")
        except ImportError:
            # Fallback: send the full PNG (OpenRouter accepts it; just larger upload)
            with open(path, "rb") as f:
                return base64.b64encode(f.read()).decode("ascii")
    except (subprocess.TimeoutExpired, OSError):
        return None
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


async def _check_gui_agent_smoke(
    backend: str, model: str, key: str, display: str, budget_s: float = 4.0,
) -> CheckResult:
    """End-to-end gui_agent round-trip smoke test — bounded at 4s, no traces.

    What it exercises:
      - scrot can capture from `display`
      - OpenRouter is reachable + the configured model serves
      - Vision input is accepted
      - Tool calling is honored (`tool_choice: required`)

    What it does NOT do:
      - Go through the bash_backend.py loop (no _record_usage, no session,
        no _dbg.log, no DB write, no cancel-event registry entry)
      - Execute any bash command (the smoke prompt forces an immediate `done`)
      - Capture more than one frame

    The whole call is bounded by httpx.AsyncClient(timeout=budget_s); on timeout
    or failure we return FAIL/WARN with the elapsed ms so a maintainer can see
    where it slowed down. Skipped if backend != 'bash' (supervised-loop probe
    would need a different shape).
    """
    if backend != "bash":
        return CheckResult("live_ui", "round-trip smoke", SKIP,
                           f"backend={backend} (smoke test is bash-specific)")
    if not key:
        return CheckResult("live_ui", "round-trip smoke", WARN,
                           "no OPENROUTER_API_KEY — cannot probe")

    t0 = time.perf_counter()
    b64 = _capture_thumbnail_b64(display)
    cap_ms = (time.perf_counter() - t0) * 1000
    if not b64:
        return CheckResult("live_ui", "round-trip smoke", FAIL,
                           f"screenshot capture failed ({cap_ms:.0f}ms)")

    body = {
        "model": model,
        "messages": [
            {"role": "system",
             "content": "Smoke test. Call the `done` tool once with summary='ok' and stop. Do not call any other tool."},
            {"role": "user", "content": [
                {"type": "text", "text": "Smoke test. Call done() now."},
                {"type": "image_url",
                 "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
            ]},
        ],
        "tools": [{
            "type": "function",
            "function": {
                "name": "done",
                "description": "Confirm the smoke test round-trip is alive.",
                "parameters": {
                    "type": "object",
                    "properties": {"summary": {"type": "string"}},
                    "required": ["summary"],
                },
            },
        }],
        "tool_choice": "required",
        "max_tokens": 50,
    }
    # Silence httpx INFO line for this single call so the smoke test leaves
    # no log trace at all (DB writes / temp files / sessions are also avoided).
    import logging as _logging
    _httpx_log = _logging.getLogger("httpx")
    _prev_level = _httpx_log.level
    _httpx_log.setLevel(_logging.WARNING)
    try:
        async with httpx.AsyncClient(timeout=budget_s) as c:
            api_t0 = time.perf_counter()
            r = await c.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                json=body,
            )
            api_ms = (time.perf_counter() - api_t0) * 1000
        if r.status_code != 200:
            return CheckResult("live_ui", "round-trip smoke", FAIL,
                               f"HTTP {r.status_code}: {r.text[:120]}")
        data = r.json()
        try:
            msg = data["choices"][0]["message"]
            tool_calls = msg.get("tool_calls") or []
        except (KeyError, IndexError, TypeError):
            return CheckResult("live_ui", "round-trip smoke", FAIL,
                               f"malformed response shape (api_ms={api_ms:.0f})")
        if not tool_calls:
            finish = data["choices"][0].get("finish_reason")
            return CheckResult("live_ui", "round-trip smoke", FAIL,
                               f"no tool_call returned (finish={finish}, api_ms={api_ms:.0f})")
        name = (tool_calls[0].get("function") or {}).get("name", "?")
        if name != "done":
            return CheckResult("live_ui", "round-trip smoke", WARN,
                               f"unexpected tool {name!r} (api_ms={api_ms:.0f})")
        total_ms = (time.perf_counter() - t0) * 1000
        return CheckResult("live_ui", "round-trip smoke", OK,
                           f"{total_ms:.0f}ms (capture={cap_ms:.0f}, api={api_ms:.0f})")
    except httpx.TimeoutException:
        return CheckResult("live_ui", "round-trip smoke", FAIL,
                           f"timed out >{budget_s}s")
    except httpx.HTTPError as e:
        return CheckResult("live_ui", "round-trip smoke", FAIL,
                           f"{type(e).__name__}: {e}")
    finally:
        _httpx_log.setLevel(_prev_level)


def _check_live_ui_backend(unit_env: dict[str, str]) -> CheckResult:
    """Verify the multi-step gui_agent backend is set to a known value.

    LIVE_UI_BACKEND is the production-critical setting — it picks which provider
    runs inside MCP's `gui_agent` tool. Default is `bash` (production) but can be
    `supervised` (legacy). Anything else is a typo that will raise at first call.
    """
    backend = (unit_env.get("ACU_LIVE_UI_BACKEND")
               or os.environ.get("ACU_LIVE_UI_BACKEND")
               or config.LIVE_UI_BACKEND
               or "bash").lower()
    model = (unit_env.get("ACU_OPENROUTER_GUI_DIRECT_MODEL")
             or os.environ.get("ACU_OPENROUTER_GUI_DIRECT_MODEL")
             or config.OPENROUTER_GUI_DIRECT_MODEL)
    if backend not in ("bash", "supervised"):
        return CheckResult("live_ui", "backend", FAIL,
                           f"ACU_LIVE_UI_BACKEND={backend!r} unknown — must be 'bash' or 'supervised'")
    if backend == "bash":
        return CheckResult("live_ui", "backend", OK,
                           f"bash + {model} (production)")
    return CheckResult("live_ui", "backend", WARN,
                       f"supervised (legacy) — production is bash + openai/gpt-5.4")


async def _check_backend_anthropic(key: str) -> CheckResult:
    if not key:
        return CheckResult("backends", "anthropic", SKIP, "no ANTHROPIC_API_KEY set")
    try:
        async with httpx.AsyncClient(timeout=10.0) as c:
            r = await c.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": "claude-haiku-4-5-20251001",
                    "max_tokens": 1,
                    "messages": [{"role": "user", "content": "hi"}],
                },
            )
        if r.status_code in (200, 529):  # 529 = overloaded but auth OK
            return CheckResult("backends", "anthropic", OK, "key valid")
        if r.status_code == 401:
            return CheckResult("backends", "anthropic", FAIL, "invalid key (401)")
        return CheckResult("backends", "anthropic", WARN,
                           f"HTTP {r.status_code}: {r.text[:120]}")
    except httpx.HTTPError as e:
        return CheckResult("backends", "anthropic", FAIL, str(e))


async def _check_backend_ollama(url: str, required_model: str | None) -> CheckResult:
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{url.rstrip('/')}/api/tags")
        if r.status_code != 200:
            return CheckResult("backends", "ollama", FAIL, f"HTTP {r.status_code}")
        tags = r.json().get("models", [])
        names = [t.get("name", "") for t in tags]
        if required_model and required_model not in names:
            return CheckResult("backends", "ollama", WARN,
                               f"reachable; required model '{required_model}' not pulled")
        return CheckResult("backends", "ollama", OK,
                           f"{len(names)} model(s): {', '.join(names[:4])}")
    except httpx.HTTPError as e:
        return CheckResult("backends", "ollama", WARN, f"unreachable: {e}")


async def _check_backend_mavi(key: str) -> CheckResult:
    if not key:
        return CheckResult("backends", "mavi", SKIP, "no MAVI_API_KEY set")
    return CheckResult("backends", "mavi", OK, "key present (not probed)")


def _check_api_keys_present(
    vision_backend: str, gui_backend: str, unit_env: dict[str, str]
) -> list[CheckResult]:
    out: list[CheckResult] = []
    needed: set[str] = set()
    if vision_backend in ("openrouter",) or gui_backend in ("uitars",) or \
       config.OPENROUTER_LIVE_MODEL:
        needed.add("OPENROUTER_API_KEY")
    if vision_backend == "claude" or gui_backend == "claude_cu":
        needed.add("ANTHROPIC_API_KEY")

    for key in sorted(needed):
        unit_val = unit_env.get(key, "").strip()
        if unit_val:
            out.append(CheckResult("keys", key, OK,
                                   f"set in systemd unit (len={len(unit_val)})"))
        else:
            env_val = os.environ.get(key, "").strip()
            if env_val:
                out.append(CheckResult("keys", key, WARN,
                    "set in this process env but NOT in systemd unit — daemon will not see it"))
            else:
                out.append(CheckResult("keys", key, FAIL,
                                       "required by current backend but not set anywhere"))

    # Optional keys — surface as SKIP if unset
    for key in ("MAVI_API_KEY",):
        if key not in needed:
            unit_val = unit_env.get(key, "").strip()
            if unit_val:
                out.append(CheckResult("keys", key, OK, "set in systemd unit (optional)"))
            else:
                out.append(CheckResult("keys", key, SKIP,
                                       "not set (optional — needed for mavi_understand)"))

    # Drift check: .env file at repo root can contain a stale key that load_dotenv()
    # injects into this process — a footgun that masks what the daemon actually uses.
    env_file = Path(__file__).resolve().parent.parent.parent / ".env"
    if env_file.exists():
        drift_keys: list[str] = []
        try:
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k in ("OPENROUTER_API_KEY", "ANTHROPIC_API_KEY", "MAVI_API_KEY"):
                    unit_v = unit_env.get(k, "")
                    if v and unit_v and v != unit_v:
                        drift_keys.append(k)
        except OSError:
            pass
        if drift_keys:
            out.append(CheckResult("keys", ".env drift", WARN,
                f"{env_file} contains stale {', '.join(drift_keys)} that differ from systemd unit — can confuse CLI tooling",
                extra={"drift": drift_keys}))

    return out


def _check_mcp_registration(oc_cfg: dict | None) -> CheckResult:
    if oc_cfg is None:
        return CheckResult("mcp", "registration", WARN,
                           f"openclaw.json unreadable at {OPENCLAW_CONFIG}")
    entry = oc_cfg.get("mcp", {}).get("servers", {}).get("agentic-computer-use")
    if not entry:
        return CheckResult("mcp", "registration", FAIL,
                           "agentic-computer-use not registered in openclaw.json")
    cmd = entry.get("command", "")
    cwd = entry.get("cwd", "")
    missing = []
    if cmd and not Path(cmd).exists():
        missing.append(f"command missing: {cmd}")
    if cwd and not Path(cwd).exists():
        missing.append(f"cwd missing: {cwd}")
    if missing:
        return CheckResult("mcp", "registration", FAIL, "; ".join(missing))
    return CheckResult("mcp", "registration", OK, f"cwd={cwd}")


def _check_sqlite() -> CheckResult:
    db = config.DB_PATH
    if not db.exists():
        return CheckResult("storage", "sqlite data.db", WARN,
                           f"{db} does not exist yet (normal on fresh install)")
    try:
        con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=3)
        try:
            row = con.execute("pragma integrity_check").fetchone()
        finally:
            con.close()
        if row and row[0] == "ok":
            size_mb = db.stat().st_size / (1024 * 1024)
            return CheckResult("storage", "sqlite data.db", OK,
                               f"{size_mb:.1f}MB, integrity_check=ok")
        return CheckResult("storage", "sqlite data.db", FAIL,
                           f"integrity_check={row}")
    except sqlite3.Error as e:
        return CheckResult("storage", "sqlite data.db", FAIL, str(e))


def _check_disk_usage() -> CheckResult:
    data_dir = config.DATA_DIR
    if not data_dir.exists():
        return CheckResult("storage", "data dir", WARN,
                           f"{data_dir} does not exist")
    total = 0
    for p in data_dir.rglob("*"):
        if p.is_file():
            try:
                total += p.stat().st_size
            except OSError:
                pass
    mb = total / (1024 * 1024)
    cap = config.MAX_RECORDINGS_MB
    status = OK if cap == 0 or mb < cap * 0.9 else WARN
    return CheckResult("storage", "data dir size", status,
                       f"{mb:.1f}MB used, cap={cap}MB",
                       extra={"bytes": total, "cap_mb": cap})


def _check_log_errors() -> CheckResult:
    log = config.DATA_DIR / "logs" / "debug.log"
    if not log.exists():
        return CheckResult("storage", "debug.log tail", SKIP,
                           "no debug log yet (ACU_DEBUG=0 in this process)")
    try:
        size = log.stat().st_size
        with log.open("rb") as f:
            tail_n = 8192
            if size > tail_n:
                f.seek(-tail_n, 2)
            tail = f.read().decode("utf-8", "replace")
    except OSError as e:
        return CheckResult("storage", "debug.log tail", WARN, str(e))
    recent_errors = [
        line for line in tail.splitlines()[-200:]
        if " ERROR " in line or " CRITICAL " in line or line.startswith("Traceback")
    ]
    if not recent_errors:
        return CheckResult("storage", "debug.log tail", OK, "no recent errors")
    return CheckResult("storage", "debug.log tail", WARN,
                       f"{len(recent_errors)} error line(s) in last tail",
                       extra={"last": recent_errors[-3:]})


def _check_deps() -> list[CheckResult]:
    """Binary deps that the daemon shells out to."""
    specs = [
        ("xdotool", "GUI actions"),
        ("ffmpeg", "recording"),
        ("scrot", "screenshots"),
        ("Xvfb", "virtual display"),
        ("x11vnc", "VNC export"),
        ("websockify", "noVNC"),
        ("xdpyinfo", "display probing"),
    ]
    out: list[CheckResult] = []
    for bin_name, purpose in specs:
        path = shutil.which(bin_name)
        if path:
            out.append(CheckResult("deps", bin_name, OK, f"{path} ({purpose})"))
        else:
            out.append(CheckResult("deps", bin_name, FAIL, f"missing ({purpose})"))
    return out


def _check_browser(oc_cfg: dict | None) -> CheckResult:
    path = (oc_cfg or {}).get("browser", {}).get("executablePath", "")
    if not path:
        return CheckResult("deps", "browser", WARN, "openclaw.browser.executablePath not set")
    if not Path(path).exists():
        return CheckResult("deps", "browser", FAIL, f"configured but missing: {path}")
    return CheckResult("deps", "browser", OK, path)


def _check_workspace() -> CheckResult:
    ws = Path(os.environ.get("ACU_WORKSPACE",
                             Path.home() / ".openclaw" / "workspace"))
    agents = ws / "AGENTS.md"
    if not ws.exists():
        return CheckResult("workspace", "AGENTS.md", WARN, f"{ws} does not exist")
    if not agents.exists():
        return CheckResult("workspace", "AGENTS.md", WARN, f"missing: {agents}")
    return CheckResult("workspace", "AGENTS.md", OK, str(agents))


def _check_openclaw_gateway() -> CheckResult:
    if _port_listening("127.0.0.1", GATEWAY_PORT):
        return CheckResult("openclaw", f"gateway :{GATEWAY_PORT}", OK, "listening")
    return CheckResult("openclaw", f"gateway :{GATEWAY_PORT}", WARN,
                       "not listening (OpenClaw not running — optional)")


# ── Orchestration ───────────────────────────────────────────────

async def run_diagnostics() -> list[CheckResult]:
    """Run every check and return the full report."""
    oc_cfg = _load_openclaw_config()
    unit_env = _read_unit_env(SYSTEMD_UNIT)

    # Trust the systemd unit as authoritative for "what the daemon actually sees".
    # Falling back to os.environ only if the unit is missing/unreadable — otherwise
    # a stale .env or shell export would mask the real daemon state.
    def _env(key: str, default: str = "") -> str:
        return unit_env.get(key) or os.environ.get(key, default)

    openrouter_key = _env("OPENROUTER_API_KEY", "")
    anthropic_key  = _env("ANTHROPIC_API_KEY", "")
    mavi_key       = _env("MAVI_API_KEY", "")
    vision_backend = _env("ACU_VISION_BACKEND", config.VISION_BACKEND)
    gui_backend    = _env("ACU_GUI_AGENT_BACKEND", config.GUI_AGENT_BACKEND)
    live_ui_backend = _env("ACU_LIVE_UI_BACKEND", config.LIVE_UI_BACKEND or "bash").lower()
    live_ui_model   = _env("ACU_OPENROUTER_GUI_DIRECT_MODEL", config.OPENROUTER_GUI_DIRECT_MODEL)

    results: list[CheckResult] = []

    async with httpx.AsyncClient() as client:
        # Core — daemon first so we surface the canonical failure loudly
        results.append(await _check_daemon(client))
        results.append(_check_display())

        # Services + ports
        for unit, purpose in [
            ("detm-daemon.service",  "DETM HTTP daemon"),
            ("detm-xvfb.service",    "Xvfb virtual display"),
            ("detm-desktop.service", "XFCE session"),
            ("detm-vnc.service",     "x11vnc export"),
            ("detm-novnc.service",   "noVNC websocket proxy"),
        ]:
            results.append(_check_service(unit, purpose))
        results.append(_check_port("daemon",   "127.0.0.1", 18790))
        results.append(_check_port("vnc",      "127.0.0.1", VNC_PORT, required=False))
        results.append(_check_port("novnc",    "0.0.0.0",   NOVNC_PORT, required=False))

        # Dashboard
        results.append(await _check_dashboard(client))

        # Live-UI / multi-step gui_agent backend (production: bash + openai/gpt-5.4).
        # Surfacing this early so it lands above the generic key checks.
        results.append(_check_live_ui_backend(unit_env))

        # Backends — only probe what the install actually uses
        backend_tasks: list = []
        if vision_backend == "openrouter" or gui_backend == "uitars" or live_ui_backend == "bash":
            backend_tasks.append(_check_backend_openrouter(openrouter_key))
        # Probe the specific live_ui model on OpenRouter — catches "snapshot retired"
        # which the generic key probe misses.
        if live_ui_backend == "bash" and openrouter_key:
            backend_tasks.append(_check_openrouter_model(openrouter_key, live_ui_model))
        # End-to-end round-trip smoke test (bash backend only; bounded at 4s).
        if live_ui_backend == "bash" and openrouter_key:
            backend_tasks.append(_check_gui_agent_smoke(
                live_ui_backend, live_ui_model, openrouter_key,
                _env("DISPLAY", config.DISPLAY),
            ))
        if vision_backend == "claude" or gui_backend == "claude_cu":
            backend_tasks.append(_check_backend_anthropic(anthropic_key))
        if vision_backend == "ollama" or gui_backend == "direct":
            # 'direct' doesn't actually use ollama but users often run both; probe if set
            if config.OLLAMA_URL:
                probe_model = config.VISION_MODEL if vision_backend == "ollama" else None
                backend_tasks.append(_check_backend_ollama(config.OLLAMA_URL, probe_model))
        if mavi_key:
            backend_tasks.append(_check_backend_mavi(mavi_key))
        if backend_tasks:
            results.extend(await asyncio.gather(*backend_tasks))

        # Keys
        results.extend(_check_api_keys_present(vision_backend, gui_backend, unit_env))

        # MCP + OpenClaw
        results.append(_check_mcp_registration(oc_cfg))
        results.append(_check_openclaw_gateway())

        # Storage + logs
        results.append(_check_sqlite())
        results.append(_check_disk_usage())
        results.append(_check_log_errors())

        # Deps
        results.extend(_check_deps())
        results.append(_check_browser(oc_cfg))
        results.append(_check_workspace())

    return results


def exit_code(results: list[CheckResult]) -> int:
    if any(r.status == FAIL for r in results): return 2
    if any(r.status == WARN for r in results): return 1
    return 0


def summarize(results: list[CheckResult]) -> dict[str, int]:
    s = {OK: 0, WARN: 0, FAIL: 0, SKIP: 0}
    for r in results:
        s[r.status] = s.get(r.status, 0) + 1
    return s


def print_human(results: list[CheckResult], quiet: bool = False) -> None:
    groups: dict[str, list[CheckResult]] = {}
    for r in results:
        groups.setdefault(r.section, []).append(r)

    for section, items in groups.items():
        if quiet and not any(r.status in (WARN, FAIL) for r in items):
            continue
        print(f"\n{_bold(section)}")
        for r in items:
            if quiet and r.status == OK:
                continue
            print(f"  {r.badge()} {r.name:<28}  {_dim(r.detail)}")

    summary = summarize(results)
    print()
    print(_bold("summary"),
          _green(f"ok={summary[OK]}"),
          _yellow(f"warn={summary[WARN]}"),
          _red(f"fail={summary[FAIL]}"),
          _dim(f"skip={summary[SKIP]}"))

    # LLM cue — matches the skill note
    fails = [r for r in results if r.status == FAIL]
    warns = [r for r in results if r.status == WARN]
    if fails:
        print(_red("\nFAILING: ") + ", ".join(f"{r.section}:{r.name}" for r in fails))
    if warns:
        print(_yellow("WARNINGS: ") + ", ".join(f"{r.section}:{r.name}" for r in warns))
    if not fails and not warns:
        print(_green("\nAll systems green."))


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="detm-doctor",
        description="Diagnose the health of every DETM subsystem.",
    )
    p.add_argument("--json", action="store_true", help="machine-readable JSON output")
    p.add_argument("--quiet", action="store_true", help="only show warnings and failures")
    args = p.parse_args(argv)

    results = asyncio.run(run_diagnostics())

    if args.json:
        payload = {
            "summary": summarize(results),
            "exit_code": exit_code(results),
            "results": [asdict(r) for r in results],
        }
        print(json.dumps(payload, indent=2, default=str))
    else:
        print_human(results, quiet=args.quiet)

    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
