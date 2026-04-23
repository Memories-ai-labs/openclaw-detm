"""xdotool-based GUI action execution — shared across live providers."""
import logging
import os
import subprocess
import time

from .. import humanize

log = logging.getLogger(__name__)

_MOUSE_BUTTON = {"left": "1", "middle": "2", "right": "3"}
_SCROLL_BUTTON = {"up": "4", "down": "5", "left": "6", "right": "7"}

# Steps and per-step delay for smooth mouse animation.
# 10 steps × 18 ms ≈ 180 ms total — fast enough to feel instant, slow enough
# to be visible as a glide in the live frame stream.
_SMOOTH_STEPS = 10
_SMOOTH_STEP_MS = 0.018


def _xdotool(args: list, display: str, timeout: int = 5) -> str:
    """Run xdotool with the given display env. Returns "ok" or an error string."""
    env = {**os.environ, "DISPLAY": display}
    try:
        result = subprocess.run(
            ["xdotool"] + args,
            capture_output=True, text=True, timeout=timeout, env=env,
        )
        if result.returncode != 0:
            log.warning(f"xdotool {args[0]} failed: {result.stderr.strip()[:200]}")
            return f"error: {result.stderr.strip()[:200]}"
        return "ok"
    except FileNotFoundError:
        return "error: xdotool not found"
    except subprocess.TimeoutExpired:
        return "error: timeout"


def _current_mouse_pos(display: str) -> tuple[int, int] | None:
    env = {**os.environ, "DISPLAY": display}
    try:
        loc = subprocess.run(
            ["xdotool", "getmouselocation"],
            capture_output=True, text=True, timeout=2, env=env,
        )
        parts = loc.stdout.strip().split()
        return int(parts[0].split(":")[1]), int(parts[1].split(":")[1])
    except Exception:
        return None


def _screen_bounds(display: str) -> tuple[int, int, int, int] | None:
    env = {**os.environ, "DISPLAY": display}
    try:
        r = subprocess.run(["xdpyinfo"], capture_output=True, text=True, timeout=2, env=env)
        if r.returncode != 0:
            return None
        for line in r.stdout.splitlines():
            if "dimensions:" in line:
                dim = line.strip().split("dimensions:", 1)[1].strip().split()[0]
                w, h = (int(v) for v in dim.split("x"))
                return (0, 0, w, h)
    except Exception:
        pass
    return None


def _smooth_mousemove(x: int, y: int, display: str) -> str:
    """Glide the cursor to (x, y).

    - humanize OFF (legacy): 10 linear steps × 18 ms = ~180 ms constant glide.
    - humanize ON: Bezier arc with Fitts-scaled duration; endpoint is exact.
    """
    env = {**os.environ, "DISPLAY": display}
    pos = _current_mouse_pos(display) or (x, y)
    cx, cy = pos

    if humanize.is_enabled():
        bounds = _screen_bounds(display)
        for wp in humanize.bezier_path(cx, cy, x, y, screen_bounds=bounds):
            subprocess.run(
                ["xdotool", "mousemove", str(wp.x), str(wp.y)],
                capture_output=True, timeout=2, env=env,
            )
            if wp.sleep_before_next_s > 0:
                time.sleep(wp.sleep_before_next_s)
        return "ok"

    steps = _SMOOTH_STEPS
    for i in range(1, steps + 1):
        ix = cx + (x - cx) * i // steps
        iy = cy + (y - cy) * i // steps
        subprocess.run(
            ["xdotool", "mousemove", str(ix), str(iy)],
            capture_output=True, timeout=2, env=env,
        )
        if i < steps:
            time.sleep(_SMOOTH_STEP_MS)

    return "ok"


def _humanized_click(btn: str, display: str) -> str:
    """Click with mousedown/dwell/mouseup when humanize is on.

    --clearmodifiers on click/mousedown/mouseup prevents stuck Ctrl/Shift/etc. from a prior
    key_press (e.g. ctrl+l) turning this click into a modified click — which in Firefox
    opens links in new windows/tabs instead of activating them.
    """
    if not humanize.is_enabled():
        return _xdotool(["click", "--clearmodifiers", btn], display)
    env = {**os.environ, "DISPLAY": display}
    subprocess.run(["xdotool", "mousedown", "--clearmodifiers", btn], capture_output=True, timeout=2, env=env)
    time.sleep(humanize.click_dwell_s())
    r = subprocess.run(["xdotool", "mouseup", "--clearmodifiers", btn], capture_output=True, timeout=2, env=env)
    return "ok" if r.returncode == 0 else f"error: mouseup failed"


def _humanized_type(text: str, display: str) -> str:
    """Per-char typing with lognormal delay when humanize is on."""
    if not humanize.is_enabled():
        return _xdotool(["type", "--clearmodifiers", "--", text], display)
    env = {**os.environ, "DISPLAY": display}
    for ch, wait_s in zip(text, humanize.typing_delays(text)):
        r = subprocess.run(
            ["xdotool", "type", "--delay", "0", "--clearmodifiers", "--", ch],
            capture_output=True, timeout=3, env=env,
        )
        if r.returncode != 0:
            return f"error: {r.stderr.decode('utf-8', 'replace').strip()[:120]}"
        if wait_s > 0:
            time.sleep(wait_s)
    return "ok"


def _normalize_key(key: str) -> str:
    """Normalize modifier key case for xdotool: Ctrl+c → ctrl+c."""
    modifiers = {"ctrl", "alt", "shift", "super", "meta"}
    parts = key.split("+")
    return "+".join(p.lower() if p.lower() in modifiers else p for p in parts)


def execute_action(name: str, args: dict, display: str) -> str:
    """Execute a named GUI action via xdotool. Returns 'ok' or an error string."""
    if name == "move_mouse":
        x, y = int(args["x"]), int(args["y"])
        return _smooth_mousemove(x, y, display)

    if name == "click":
        x, y = int(args["x"]), int(args["y"])
        btn = _MOUSE_BUTTON.get(str(args.get("button", "left")), "1")
        clicks = int(args.get("clicks", 1))
        if humanize.is_enabled():
            x, y = humanize.jitter_click_coords(x, y)
        _smooth_mousemove(x, y, display)
        if clicks > 1:
            if humanize.is_enabled():
                # Separate clicks each with dwell so keydown-keydown intervals vary.
                for i in range(clicks):
                    r = _humanized_click(btn, display)
                    if r != "ok":
                        return r
                    if i < clicks - 1:
                        time.sleep(humanize.click_dwell_s() + 0.02)
                return "ok"
            return _xdotool(["click", "--clearmodifiers", "--repeat", str(clicks), btn], display)
        return _humanized_click(btn, display)

    if name == "double_click":
        x, y = int(args["x"]), int(args["y"])
        if humanize.is_enabled():
            x, y = humanize.jitter_click_coords(x, y)
        _smooth_mousemove(x, y, display)
        if humanize.is_enabled():
            _humanized_click("1", display)
            time.sleep(humanize.click_dwell_s() + 0.02)
            return _humanized_click("1", display)
        return _xdotool(["click", "--clearmodifiers", "--repeat", "2", "1"], display)

    if name == "type_text":
        text = args.get("text", "")
        return _humanized_type(text, display)

    if name == "key_press":
        key = _normalize_key(args.get("key", "Return"))
        return _xdotool(["key", "--clearmodifiers", key], display)

    if name == "scroll":
        x, y = int(args["x"]), int(args["y"])
        direction = args.get("direction", "down")
        amount = max(1, int(args.get("amount", 3)))
        btn = _SCROLL_BUTTON.get(direction, "5")
        _smooth_mousemove(x, y, display)
        return _xdotool(["click", "--clearmodifiers", "--repeat", str(amount), btn], display)

    if name == "drag":
        x1, y1 = int(args["start_x"]), int(args["start_y"])
        x2, y2 = int(args["end_x"]), int(args["end_y"])
        # Parse optional waypoints: list of {x, y} dicts
        waypoints = args.get("waypoints") or []
        env = {**os.environ, "DISPLAY": display}
        _smooth_mousemove(x1, y1, display)
        # Mouse down
        subprocess.run(["xdotool", "mousedown", "1"], capture_output=True, timeout=2, env=env)
        # Move through waypoints
        for wp in waypoints:
            wx, wy = int(wp["x"]), int(wp["y"])
            _smooth_mousemove(wx, wy, display)
        # Move to end
        _smooth_mousemove(x2, y2, display)
        # Mouse up
        subprocess.run(["xdotool", "mouseup", "1"], capture_output=True, timeout=2, env=env)
        return "ok"

    if name == "mouse_down":
        x, y = int(args["x"]), int(args["y"])
        btn = _MOUSE_BUTTON.get(str(args.get("button", "left")), "1")
        _smooth_mousemove(x, y, display)
        return _xdotool(["mousedown", btn], display)

    if name == "mouse_up":
        btn = _MOUSE_BUTTON.get(str(args.get("button", "left")), "1")
        return _xdotool(["mouseup", btn], display)

    log.warning(f"Unknown live action: {name}")
    return f"error: unknown action {name}"


def execute_action_logged(name: str, args: dict, display: str) -> str:
    """Execute a GUI action with success/failure logging and timing."""
    t0 = time.monotonic()
    result = execute_action(name, args, display)
    elapsed_ms = (time.monotonic() - t0) * 1000
    if result == "ok":
        coords = f" at ({args.get('x')},{args.get('y')})" if "x" in args else ""
        log.info(f"Action {name}{coords} — ok ({elapsed_ms:.0f}ms)")
    else:
        log.warning(f"Action {name} — {result} ({elapsed_ms:.0f}ms)")
    return result
