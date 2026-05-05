"""Persistent daemon for the DETM — wait engine, task manager, GUI agent.

Runs as a background HTTP server so the MCP server (spawned per-session by OpenClaw)
can submit jobs and query state without losing the async event loop.
"""
import asyncio
import io
import json
import logging
import os
import pathlib
import time
from datetime import datetime, timezone
from aiohttp import web

from . import config, db, debug
from .wait.engine import WaitEngine, WaitJob
from .wait.poller import AdaptivePoller
from .task import manager as task_mgr
from .capture import frame_recorder

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

DAEMON_HOST = os.environ.get("ACU_DAEMON_HOST", "127.0.0.1")
DAEMON_PORT = int(os.environ.get("ACU_DAEMON_PORT", "18790"))

wait_engine = WaitEngine()


async def _parse_body(request: web.Request) -> dict:
    """Parse JSON body, normalizing keys that have trailing colons (legacy MCP artifact)."""
    if not request.can_read_body:
        return {}
    raw = await request.json()
    # Legacy MCP clients may send key:=value as {"key:": value} — strip trailing colons
    return {k.rstrip(":"): v for k, v in raw.items()} if isinstance(raw, dict) else raw


async def _resolve_task_display(task_id: str | None) -> str:
    """Look up the per-task display, falling back to the system display.

    Only trusts a stored display that differs from the system display if the
    task explicitly opted into isolation — prevents stale Xvfb metadata from
    pre-shared-display era returning dead displays.
    """
    if task_id:
        display, isolated = await task_mgr.get_task_display_info(task_id)
        if display:
            if display == config.DISPLAY or isolated:
                return display
    return config.DISPLAY


# ─── Wait handlers ───────────────────────────────────────────────

async def handle_smart_wait(request: web.Request) -> web.Response:
    args = await _parse_body(request)
    target_str = args["target"]
    if ":" in target_str:
        target_type, target_id = target_str.split(":", 1)
    else:
        target_type, target_id = target_str, "full"

    if target_type not in ("window", "screen"):
        return web.json_response({"error": f"Invalid target type: {target_type}. Use window:<name> or screen."}, status=400)

    task_id = args.get("task_id")
    if task_id and not await task_mgr.task_exists(task_id):
        return web.json_response({"error": f"Task {task_id} not found"}, status=404)

    display = await _resolve_task_display(task_id)

    job_id = db.new_id()
    job = WaitJob(
        id=job_id,
        target_type=target_type,
        target_id=target_id,
        criteria=args["wake_when"],
        timeout=args.get("timeout", config.DEFAULT_TIMEOUT),
        task_id=task_id,
        display=display,
        poll_interval=float(args.get("poll_interval", config.DEFAULT_POLL_INTERVAL)),
    )

    conn = await db.get_db()
    try:
        await conn.execute(
            "INSERT INTO wait_jobs (id, task_id, target_type, target_id, criteria, timeout_seconds, poll_interval, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (job.id, job.task_id, job.target_type, job.target_id, job.criteria, job.timeout, job.poll_interval, "watching", db.now_iso())
        )
        await conn.commit()
    finally:
        await conn.close()

    wait_engine.add_job(job)

    if task_id:
        # Capture "before" screenshot for the wait action
        screenshot_refs = None
        try:
            from .capture.screen import capture_screen as _cap_screen
            from .screenshots import save_screenshot
            before_frame = _cap_screen(display=display)
            if before_frame is not None:
                screenshot_refs = save_screenshot(job_id, "before", before_frame)
        except Exception:
            pass

        await task_mgr.on_wait_created(
            task_id=task_id,
            wait_id=job_id,
            target=f"{target_type}:{target_id}",
            criteria=job.criteria,
            screenshot_refs=screenshot_refs,
            timeout=job.timeout,
        )

    return web.json_response({
        "wait_id": job_id,
        "status": "watching",
        "target": f"{target_type}:{target_id}",
        "message": f"Monitoring. I'll wake you when: {job.criteria}. Timeout: {job.timeout}s.",
    })


async def handle_wait_status(request: web.Request) -> web.Response:
    args = await _parse_body(request)
    wait_id = args.get("wait_id")
    import time
    if wait_id:
        if wait_id in wait_engine.jobs:
            job = wait_engine.jobs[wait_id]
            elapsed = time.time() - job.context.started_at
            return web.json_response({
                "wait_id": job.id, "status": job.status,
                "target": f"{job.target_type}:{job.target_id}",
                "criteria": job.criteria,
                "elapsed_seconds": round(elapsed),
                "poll_interval": job.poll_interval,
            })
        conn = await db.get_db()
        try:
            rows = await conn.execute_fetchall(
                "SELECT * FROM wait_jobs WHERE id = ? LIMIT 1",
                (wait_id,),
            )
        finally:
            await conn.close()
        if rows:
            job = dict(rows[0])
            elapsed = None
            try:
                start_ts = datetime.fromisoformat(job["created_at"]).timestamp()
                end_iso = job.get("resolved_at") or db.now_iso()
                end_ts = datetime.fromisoformat(end_iso).timestamp()
                elapsed = round(max(0.0, end_ts - start_ts))
            except Exception:
                elapsed = None
            payload = {
                "wait_id": job["id"],
                "status": job["status"],
                "target": f"{job['target_type']}:{job['target_id']}",
                "criteria": job["criteria"],
                "poll_interval": job["poll_interval"],
            }
            if elapsed is not None:
                payload["elapsed_seconds"] = elapsed
            if job.get("result_message"):
                payload["detail"] = job["result_message"]
            if job.get("resolved_at"):
                payload["resolved_at"] = job["resolved_at"]
            return web.json_response(payload)
        return web.json_response({"error": f"Wait job {wait_id} not found"}, status=404)

    jobs = []
    for job in wait_engine.jobs.values():
        elapsed = time.time() - job.context.started_at
        jobs.append({
            "wait_id": job.id, "status": job.status,
            "target": f"{job.target_type}:{job.target_id}",
            "criteria": job.criteria, "elapsed_seconds": round(elapsed),
        })
    return web.json_response({"active_jobs": jobs, "count": len(jobs)})


async def handle_wait_update(request: web.Request) -> web.Response:
    args = await _parse_body(request)
    wait_id = args["wait_id"]
    if wait_id not in wait_engine.jobs:
        return web.json_response({"error": f"Wait job {wait_id} not found"}, status=404)

    job = wait_engine.jobs[wait_id]
    import time
    if args.get("wake_when"):
        job.criteria = args["wake_when"]
    if args.get("timeout"):
        job.timeout = args["timeout"]
        job.context.started_at = time.time()
    if args.get("message"):
        log.info(f"Wait update note for {wait_id}: {args['message']}")
    job.next_check_at = 0

    return web.json_response({
        "wait_id": wait_id, "status": "watching",
        "message": f"Resumed. Watching for: {job.criteria}. Timeout: {job.timeout}s.",
    })


async def handle_wait_cancel(request: web.Request) -> web.Response:
    args = await _parse_body(request)
    reason = args.get("reason", "cancelled by agent")
    job = wait_engine.cancel_job(args["wait_id"], reason)
    if not job:
        return web.json_response({"error": "Wait job not found"}, status=404)

    conn = await db.get_db()
    try:
        await conn.execute(
            "UPDATE wait_jobs SET status = ?, result_message = ?, resolved_at = ? WHERE id = ?",
            ("cancelled", reason, db.now_iso(), job.id),
        )
        await conn.commit()
    finally:
        await conn.close()

    if job.task_id:
        await task_mgr.on_wait_finished(
            task_id=job.task_id,
            wait_id=job.id,
            state="cancelled",
            detail=reason,
        )

    return web.json_response({"wait_id": args["wait_id"], "status": "cancelled"})


# ─── Task handlers ───────────────────────────────────────────────

async def handle_task_register(request: web.Request) -> web.Response:
    args = await _parse_body(request)
    metadata = args.get("metadata") or {}
    agent_id = args.get("agent_id") or None
    result = await task_mgr.register_task(name=args["name"], plan=args["plan"], metadata=metadata, agent_id=agent_id)
    task_id = result.get("task_id")
    if task_id:
        # Only allocate an isolated Xvfb if explicitly requested via metadata
        if metadata.get("isolated_display"):
            from .display.manager import allocate_display
            w = metadata.get("display_width")
            h = metadata.get("display_height")
            info = allocate_display(task_id, width=w, height=h)
            await task_mgr.set_task_display(task_id, info.display_str)
        display = await _resolve_task_display(task_id)
        frame_recorder.start(task_id, display)
        _ensure_frame_buffer(display)
    return web.json_response(result)


async def handle_task_update(request: web.Request) -> web.Response:
    args = await _parse_body(request)
    result = await task_mgr.update_task(
        task_id=args["task_id"],
        message=args.get("message"),
        query=args.get("query"),
        status=args.get("status"),
    )
    # Stop frame recording when task reaches a terminal state
    status = (args.get("status") or "").strip().lower().replace("canceled", "cancelled")
    if status in ("completed", "cancelled", "failed"):
        frame_recorder.stop(args["task_id"])
        if status == "failed":
            frame_recorder.cleanup(args["task_id"])
        else:
            asyncio.ensure_future(frame_recorder.create_video(args["task_id"]))
    return web.json_response(result)


async def handle_task_item_update(request: web.Request) -> web.Response:
    args = await _parse_body(request)
    result = await task_mgr.update_plan_item(
        task_id=args["task_id"],
        ordinal=int(args["ordinal"]),
        status=args["status"],
        note=args.get("note"),
    )
    return web.json_response(result)


async def handle_task_plan_append(request: web.Request) -> web.Response:
    args = await _parse_body(request)
    result = await task_mgr.append_plan_items(
        task_id=args["task_id"],
        items=args["items"],
        note=args.get("note"),
    )
    return web.json_response(result)


async def handle_task_log_action(request: web.Request) -> web.Response:
    args = await _parse_body(request)
    result = await task_mgr.log_action(
        task_id=args["task_id"],
        action_type=args["action_type"],
        summary=args["summary"],
        input_data=args.get("input_data"),
        output_data=args.get("output_data"),
        status=args.get("status", "completed"),
        ordinal=args.get("ordinal"),
    )
    return web.json_response(result)


async def handle_task_summary(request: web.Request) -> web.Response:
    args = await _parse_body(request)
    result = await task_mgr.get_task_summary(
        task_id=args["task_id"],
        detail_level=args.get("detail_level", "items"),
    )
    return web.json_response(result)


async def handle_task_drill_down(request: web.Request) -> web.Response:
    args = await _parse_body(request)
    result = await task_mgr.get_task_detail(
        task_id=args["task_id"],
        ordinal=int(args["ordinal"]),
    )
    return web.json_response(result)


async def handle_task_thread(request: web.Request) -> web.Response:
    args = await _parse_body(request)
    result = await task_mgr.get_thread(args["task_id"], limit=args.get("limit", 50))
    return web.json_response(result)


async def handle_task_list(request: web.Request) -> web.Response:
    args = await _parse_body(request)
    result = await task_mgr.list_tasks(status=args.get("status", "active"), limit=args.get("limit", 10))
    return web.json_response(result)


async def handle_api_task_status(request: web.Request) -> web.Response:
    task_id = request.match_info["task_id"]
    body = await request.json()
    status = body.get("status")
    if not status:
        return web.json_response({"error": "status is required"}, status=400)
    result = await task_mgr.update_task(task_id=task_id, status=status)
    if status in ("completed", "cancelled", "failed"):
        frame_recorder.stop(task_id)
        if status == "failed":
            frame_recorder.cleanup(task_id)
        else:
            asyncio.ensure_future(frame_recorder.create_video(task_id))
    return web.json_response(result)


async def handle_api_task_delete(request: web.Request) -> web.Response:
    task_id = request.match_info["task_id"]
    frame_recorder.stop(task_id)
    # Delete video if it exists
    vp = frame_recorder.video_path(task_id)
    if vp.exists():
        vp.unlink(missing_ok=True)
    result = await task_mgr.delete_task(task_id)
    if result.get("ok"):
        return web.json_response(result)
    return web.json_response(result, status=404)


async def handle_api_task_video(request: web.Request) -> web.Response:
    """Download the MP4 recording for a completed/cancelled task."""
    task_id = request.match_info["task_id"]
    if frame_recorder.is_encoding(task_id):
        return web.json_response({"status": "encoding"}, status=202)
    vp = frame_recorder.video_path(task_id)
    if not vp.exists():
        return web.json_response({"error": "No video available"}, status=404)
    return web.FileResponse(
        vp,
        headers={"Content-Disposition": f'attachment; filename="task-{task_id}.mp4"'},
    )


async def handle_api_dump(request: web.Request) -> web.Response:
    """Central logs dump: DETM + OpenClaw + manifest. Windowed by since/until."""
    from . import export as _export
    since = request.query.get("since")
    until = request.query.get("until")
    try:
        data, fname = await _export.build_logs_dump(since=since, until=until)
    except Exception as e:
        log.exception("dump failed")
        return web.json_response({"error": str(e)}, status=500)
    return web.Response(
        body=data,
        content_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


async def handle_api_task_export(request: web.Request) -> web.Response:
    """Download a ZIP bundle containing task.json, messages, journal slice,
    debug.log, screenshots, recordings, and any live_sessions for this task.
    """
    from . import export as _export
    task_id = request.match_info["task_id"]
    try:
        data, fname = await _export.build_task_bundle(task_id)
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=404)
    return web.Response(
        body=data,
        content_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


async def handle_api_logs(request: web.Request) -> web.Response:
    """Download a systemd-journal slice for detm-daemon.

    Query params:
      since=<iso|epoch>   start of window (default: 24h ago)
      until=<iso|epoch>   end of window  (default: now)
      task_id=<id>        (optional) narrow window to task's time range
      lines=<n>           cap; defaults to 200k
      format=text|json    text returns .log attachment; json returns {"log": "..."}
    """
    from . import export as _export
    since = request.query.get("since")
    until = request.query.get("until")
    task_id = request.query.get("task_id")
    lines = request.query.get("lines")
    fmt = (request.query.get("format") or "text").lower()

    if task_id and not since and not until:
        # Derive window from task actions
        from .task import manager as _mgr
        summary = await _mgr.get_task_summary(task_id=task_id, detail_level="actions")
        if summary.get("error"):
            return web.json_response({"error": summary["error"]}, status=404)
        since = _export._earliest_action_ts(summary)
        until = summary.get("last_update") or summary.get("updated_at")
        if since: since = _export._maybe_pad(since, -60)
        if until: until = _export._maybe_pad(until, 60)
    elif not since and not until:
        # Default window: last 24h
        since = time.time() - 86400

    try:
        lines_int = int(lines) if lines else None
    except ValueError:
        lines_int = None

    text = _export.read_journal(since=since, until=until, lines=lines_int)

    if fmt == "json":
        return web.json_response({
            "since": str(since) if since else None,
            "until": str(until) if until else None,
            "task_id": task_id,
            "lines": len(text.splitlines()),
            "bytes": len(text),
            "log": text,
        })

    fname_stem = f"detm-journal-{task_id}" if task_id else "detm-journal"
    fname = f"{fname_stem}-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}.log"
    return web.Response(
        body=text,
        content_type="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


async def handle_api_task_messages(request: web.Request) -> web.Response:
    task_id = request.match_info["task_id"]
    limit = int(request.query.get("limit", 50))
    result = await task_mgr.get_thread(task_id, limit=limit)
    return web.json_response(result)


# ─── Health ──────────────────────────────────────────────────────

async def handle_health(request: web.Request) -> web.Response:
    from .wait_vision import check_health
    from .gui_agent.agent import _get_backend
    from . import humanize as _humanize
    vision_health = await check_health()
    gui_backend = _get_backend()

    # The multi-step gui_agent loop (what MCP `gui_agent` actually dispatches) is
    # the live_ui path. Default production: bash + openai/gpt-5.4.
    live_ui_backend = (config.LIVE_UI_BACKEND or "bash").lower()
    if live_ui_backend == "bash":
        live_ui_model = config.OPENROUTER_GUI_DIRECT_MODEL
    elif live_ui_backend == "supervised":
        live_ui_model = config.OPENROUTER_LIVE_MODEL  # Gemini supervisor
    else:
        live_ui_model = config.OPENROUTER_GUI_DIRECT_MODEL

    return web.json_response({
        "server": "ok", "daemon": True,
        "vision": vision_health,
        "vision_backend": config.VISION_BACKEND,
        # Live-UI / multi-step gui_agent loop — this is what MCP `gui_agent` dispatches.
        "live_ui_backend": live_ui_backend,
        "live_ui_model": live_ui_model,
        # Legacy single-shot grounder (gui_agent/agent.py — used by desktop_action only).
        # Kept for backward compat. NOTE: these are NOT what the multi-step gui_agent
        # tool uses — see live_ui_* fields above for that.
        "gui_agent_backend": config.GUI_AGENT_BACKEND,
        "gui_agent_provider": gui_backend.provider,
        "gui_agent_model": config.OPENROUTER_LIVE_MODEL,
        "active_wait_jobs": len(wait_engine.jobs),
        "data_dir": str(config.DATA_DIR),
        "display": config.DISPLAY,
        "humanize": {"enabled": _humanize.is_enabled()},
    })


async def handle_humanize_get(request: web.Request) -> web.Response:
    from . import humanize as _humanize
    return web.json_response(_humanize.snapshot())


async def handle_humanize_set(request: web.Request) -> web.Response:
    from . import humanize as _humanize
    try:
        body = await request.json()
    except Exception:
        return web.json_response({"error": "invalid JSON body"}, status=400)
    if "enabled" not in body:
        return web.json_response({"error": "missing 'enabled'"}, status=400)
    enabled = bool(body["enabled"])
    reason = str(body.get("reason") or "")
    _humanize.set_enabled(enabled, reason=reason)
    return web.json_response(_humanize.snapshot())


# ─── Async recording state ───────────────────────────────────────

_active_recordings: dict = {}  # task_id -> AsyncRecording

# ─── Live UI session registry ────────────────────────────────────

# Plain dict is safe here: aiohttp runs everything in one event loop, so reads and
# writes to this map are not concurrent. If you ever add a worker thread, switch
# to a per-key asyncio.Lock instead.
#
# Invariant: at most ONE entry at any time. There is one shared display :99 on
# this box, so two parallel gui_agent invocations (even on different task_ids)
# would scramble each other's UI. Cross-task calls are also rejected with 409
# busy. The agent decides whether to wait or cancel via gui_agent_cancel.
#
# Untagged calls use the synthetic key "__untagged__" so they collide with each
# other and with tagged calls under the same global rule.
_UNTAGGED_KEY = "__untagged__"
_active_gui_sessions: dict[str, dict] = {}
# task_id (or "__untagged__") → {"session_id": str, "started_at": float, "instruction": str}


# ─── Recording handlers ─────────────────────────────────────────

async def handle_record_start(request: web.Request) -> web.Response:
    task_id = request.match_info["task_id"]
    if task_id in _active_recordings:
        return web.json_response({"error": "Already recording"}, status=409)

    result = await task_mgr.get_task_summary(task_id=task_id, detail_level="items")
    if result.get("error"):
        return web.json_response(result, status=404)

    display = await _resolve_task_display(task_id)

    from .video.recorder import start_recording
    recording = start_recording(task_id=task_id, display=display)
    _active_recordings[task_id] = recording
    return web.json_response({"ok": True})


async def handle_record_stop(request: web.Request) -> web.Response:
    task_id = request.match_info["task_id"]
    recording = _active_recordings.pop(task_id, None)
    if not recording:
        return web.json_response({"error": "Not recording"}, status=404)

    result = recording.stop()

    # Log as a task action
    try:
        await task_mgr.log_action(
            task_id=task_id,
            action_type="recording",
            summary=f"Screen recording ({result['duration']}s, {result['size_mb']}MB)",
            output_data=result,
            status="completed",
        )
    except Exception:
        log.warning(f"Failed to log recording action for task {task_id}")

    return web.json_response(result)


async def handle_record_status(request: web.Request) -> web.Response:
    task_id = request.match_info["task_id"]
    recording = _active_recordings.get(task_id)
    if recording and recording.is_running:
        return web.json_response({"recording": True, "elapsed": round(recording.elapsed, 1)})
    # Clean up stale entry
    _active_recordings.pop(task_id, None)
    return web.json_response({"recording": False, "elapsed": 0})


# ─── Dashboard GET API endpoints ─────────────────────────────────

DASHBOARD_DIR = pathlib.Path(__file__).parent / "dashboard"


async def handle_api_screenshot(request: web.Request) -> web.Response:
    """Serve screenshot files from SCREENSHOTS_DIR with cache headers."""
    filename = request.match_info["filename"]
    # Sanitize: only allow simple filenames (no path traversal)
    if "/" in filename or "\\" in filename or ".." in filename:
        return web.json_response({"error": "invalid filename"}, status=400)
    path = config.SCREENSHOTS_DIR / filename
    if not path.is_file():
        return web.json_response({"error": "not found"}, status=404)
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


async def handle_api_recording(request: web.Request) -> web.Response:
    """Serve recording files from RECORDINGS_DIR with cache headers."""
    filename = request.match_info["filename"]
    if "/" in filename or "\\" in filename or ".." in filename:
        return web.json_response({"error": "invalid filename"}, status=400)
    from .video.recorder import RECORDINGS_DIR
    path = RECORDINGS_DIR / filename
    if not path.is_file():
        return web.json_response({"error": "not found"}, status=404)
    return web.FileResponse(path, headers={"Cache-Control": "public, max-age=86400"})


MJPEG_FPS = 2
MJPEG_BOUNDARY = b"frame"

_NO_SIGNAL_JPEG: bytes | None = None

# ─── Frame buffer — pre-captured JPEG per display ───────────────
# Background loop keeps this fresh at ~4fps so snapshot/desktop_look
# read from memory instead of blocking on Xlib.
_frame_buffer: dict[str, bytes] = {}         # display → latest JPEG bytes
_frame_buffer_dims: dict[str, tuple] = {}    # display → (width, height) of original frame
_frame_buffer_tasks: dict[str, asyncio.Task] = {}


async def _frame_buffer_loop(display: str) -> None:
    from .capture.screen import capture_screen_with_cursor, frame_to_jpeg
    loop = asyncio.get_event_loop()
    while True:
        try:
            frame = await loop.run_in_executor(None, capture_screen_with_cursor, display)
            if frame is not None:
                jpeg = await loop.run_in_executor(
                    None, frame_to_jpeg, frame,
                    config.DESKTOP_LOOK_MAX_DIM, config.DESKTOP_LOOK_JPEG_QUALITY,
                )
                _frame_buffer[display] = jpeg
                _frame_buffer_dims[display] = (frame.shape[1], frame.shape[0])
        except Exception:
            pass
        await asyncio.sleep(0.5)  # ~2fps — halved to reduce Xlib load


def _ensure_frame_buffer(display: str) -> None:
    if display not in _frame_buffer_tasks or _frame_buffer_tasks[display].done():
        _frame_buffer_tasks[display] = asyncio.ensure_future(_frame_buffer_loop(display))


def _get_frame_jpeg(display: str) -> bytes:
    _ensure_frame_buffer(display)
    return _frame_buffer.get(display) or _get_no_signal_jpeg()


def _make_no_signal_jpeg() -> bytes:
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (640, 360), color=(20, 20, 20))
    d = ImageDraw.Draw(img)
    d.text((240, 170), "display not available", fill=(100, 100, 100))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=50)
    return buf.getvalue()


def _get_no_signal_jpeg() -> bytes:
    global _NO_SIGNAL_JPEG
    if _NO_SIGNAL_JPEG is None:
        try:
            _NO_SIGNAL_JPEG = _make_no_signal_jpeg()
        except Exception:
            _NO_SIGNAL_JPEG = b""
    return _NO_SIGNAL_JPEG


async def handle_dashboard_index(request: web.Request) -> web.Response:
    index_path = DASHBOARD_DIR / "index.html"
    return web.FileResponse(index_path)


async def handle_api_tasks(request: web.Request) -> web.Response:
    status = request.query.get("status", "active")
    limit = int(request.query.get("limit", 20))
    result = await task_mgr.list_tasks(status=status, limit=limit)
    return web.json_response(result)


async def handle_api_task_detail(request: web.Request) -> web.Response:
    task_id = request.match_info["task_id"]
    detail = request.query.get("detail", "actions")
    result = await task_mgr.get_task_summary(task_id=task_id, detail_level=detail)
    return web.json_response(result)


async def handle_api_task_item(request: web.Request) -> web.Response:
    task_id = request.match_info["task_id"]
    ordinal = int(request.match_info["ordinal"])
    result = await task_mgr.get_task_detail(task_id=task_id, ordinal=ordinal)
    return web.json_response(result)


async def handle_api_waits(request: web.Request) -> web.Response:
    jobs = []
    for job in wait_engine.jobs.values():
        elapsed = time.time() - job.context.started_at
        jobs.append({
            "wait_id": job.id, "status": job.status,
            "target": f"{job.target_type}:{job.target_id}",
            "criteria": job.criteria, "elapsed_seconds": round(elapsed),
        })
    return web.json_response({"active_jobs": jobs, "count": len(jobs)})


async def handle_api_health(request: web.Request) -> web.Response:
    return await handle_health(request)


async def handle_doctor(request: web.Request) -> web.Response:
    """Full diagnostic report — richer than /health.

    Used by the MCP `health_check` tool and the dashboard. /health stays
    minimal for install.sh compatibility; anything wanting the full
    per-subsystem view should hit /doctor or /api/doctor.
    """
    from dataclasses import asdict
    from .doctor import run_diagnostics, summarize, exit_code
    results = await run_diagnostics()
    return web.json_response({
        "summary": summarize(results),
        "exit_code": exit_code(results),
        "results": [asdict(r) for r in results],
    })


async def handle_api_usage_stats(request: web.Request) -> web.Response:
    """GET /api/usage/stats?since=<days>&task_id=<id> — return aggregated AI API usage and cost."""
    from . import usage as _usage
    since_days = request.query.get("since")
    task_id = request.query.get("task_id")
    since_ts = None
    if since_days:
        try:
            since_ts = time.time() - float(since_days) * 86400
        except ValueError:
            pass
    stats = await _usage.get_stats(since_ts=since_ts, task_id=task_id or None)
    return web.json_response(stats)


async def handle_api_task_frames(request: web.Request) -> web.Response:
    """List recorded frame indices for a task."""
    task_id = request.match_info["task_id"]
    frames = frame_recorder.list_frames(task_id)
    return web.json_response({"task_id": task_id, "frames": frames, "count": len(frames)})


async def handle_api_task_frame(request: web.Request) -> web.Response:
    """Return a single recorded JPEG frame by index."""
    task_id = request.match_info["task_id"]
    frame_n = int(request.match_info["frame_n"])
    data = frame_recorder.get_frame(task_id, frame_n)
    if data is None:
        return web.Response(status=404, text="Frame not found")
    return web.Response(body=data, content_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


async def handle_api_task_snapshot(request: web.Request) -> web.Response:
    """Single JPEG frame for a task's display — served from frame buffer (no blocking Xlib call)."""
    task_id = request.match_info["task_id"]
    display = await _resolve_task_display(task_id)
    # If the task's display has no buffered frames (stale isolated Xvfb, dead display, etc.)
    # fall back to the system display so the human sees something useful.
    task_display = display
    jpeg = _frame_buffer.get(display)
    fell_back = False
    if not jpeg:
        jpeg = _frame_buffer.get(config.DISPLAY) or _get_no_signal_jpeg()
        fell_back = display != config.DISPLAY
    if display not in _frame_buffer:
        _ensure_frame_buffer(display)  # start it for next time if it's a live display
    headers = {"Cache-Control": "no-cache, no-store"}
    if fell_back:
        headers["X-Display-Fallback"] = "true"
    return web.Response(
        body=jpeg,
        content_type="image/jpeg",
        headers=headers,
    )


async def handle_api_snapshot(request: web.Request) -> web.Response:
    """Single JPEG frame of the global display — served from frame buffer."""
    jpeg = _get_frame_jpeg(config.DISPLAY)
    return web.Response(
        body=jpeg,
        content_type="image/jpeg",
        headers={"Cache-Control": "no-cache, no-store"},
    )


async def handle_api_screen_stream(request: web.Request) -> web.StreamResponse:
    """MJPEG stream of the full desktop."""
    from .capture.screen import capture_screen, frame_to_jpeg
    return await _mjpeg_stream(request, capture_fn=capture_screen, jpeg_fn=frame_to_jpeg)


async def handle_api_task_screen(request: web.Request) -> web.StreamResponse:
    """MJPEG stream for a task's per-task display (full screen of that display)."""
    from .capture.screen import capture_screen, frame_to_jpeg

    task_id = request.match_info["task_id"]
    result = await task_mgr.get_task_summary(task_id=task_id, detail_level="items")
    if result.get("error"):
        return web.json_response(result, status=404)

    display = await _resolve_task_display(task_id)
    capture_fn = lambda: capture_screen(display=display)

    return await _mjpeg_stream(request, capture_fn=capture_fn, jpeg_fn=frame_to_jpeg)


async def _mjpeg_stream(request, capture_fn, jpeg_fn):
    """Generic MJPEG streaming helper."""
    response = web.StreamResponse()
    response.content_type = f"multipart/x-mixed-replace; boundary={MJPEG_BOUNDARY.decode()}"
    response.headers["Cache-Control"] = "no-cache"
    await response.prepare(request)

    interval = 1.0 / MJPEG_FPS
    try:
        while True:
            frame = capture_fn()
            jpeg = jpeg_fn(frame, max_dim=1280, quality=60) if frame is not None else _get_no_signal_jpeg()
            if jpeg:
                await response.write(
                    b"--" + MJPEG_BOUNDARY + b"\r\n"
                    b"Content-Type: image/jpeg\r\n"
                    b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                    + jpeg + b"\r\n"
                )
            await asyncio.sleep(interval)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return response


# ─── Desktop & Vision handlers ──────────────────────────────────

async def handle_desktop_look(request: web.Request) -> web.Response:
    args = await _parse_body(request)
    import base64
    from .capture.screen import capture_screen, frame_to_jpeg

    task_id = args.get("task_id")
    display = await _resolve_task_display(task_id)

    if args.get("window_name"):
        from .desktop import control as desktop
        wid = desktop.find_window(args["window_name"], display=display)
        if wid:
            desktop.focus_window(wid, display=display)
            await asyncio.sleep(0.05)
        # Fresh capture needed after focus — fall through to live capture
        loop = asyncio.get_event_loop()
        frame = await loop.run_in_executor(None, capture_screen, display)
        if frame is None:
            return web.json_response({"error": "Failed to capture screen"}, status=500)
        jpeg = await loop.run_in_executor(None, frame_to_jpeg, frame,
                                          config.DESKTOP_LOOK_MAX_DIM, config.DESKTOP_LOOK_JPEG_QUALITY)
        w, h = frame.shape[1], frame.shape[0]
    else:
        # Use pre-captured frame buffer — instant, no Xlib round-trip
        _ensure_frame_buffer(display)
        jpeg = _frame_buffer.get(display) or _get_no_signal_jpeg()
        w, h = _frame_buffer_dims.get(display, (0, 0))

    # Compute actual JPEG pixel dimensions (may differ from screen due to downscaling)
    if w and h:
        ratio = config.DESKTOP_LOOK_MAX_DIM / max(w, h)
        if ratio < 1.0:
            img_w, img_h = int(w * ratio), int(h * ratio)
        else:
            img_w, img_h = w, h
    else:
        img_w, img_h = w, h

    if task_id:
        asyncio.ensure_future(task_mgr.log_action(
            task_id, "vision",
            f"screenshot ({img_w}x{img_h})",
        ))
    return web.json_response({
        "image_b64": base64.b64encode(jpeg).decode(),
        "mime_type": "image/jpeg",
        "media_type": "image/jpeg",
        "screen_size": {"width": w, "height": h},
        "image_size": {"width": img_w, "height": img_h},
    })


def _get_screen_dims(display: str) -> tuple[int, int]:
    """Return the actual display dimensions, falling back to xdpyinfo query if buffer not ready."""
    screen_w, screen_h = _frame_buffer_dims.get(display, (0, 0))
    if screen_w and screen_h:
        return screen_w, screen_h
    # Buffer not warmed yet — query X11 directly
    try:
        import subprocess
        result = subprocess.run(
            ["xdpyinfo", "-display", display],
            capture_output=True, text=True, timeout=3,
        )
        for line in result.stdout.split("\n"):
            if "dimensions:" in line:
                # "  dimensions:    1920x1080 pixels"
                parts = line.split()
                if parts:
                    w, h = parts[1].split("x")
                    dims = (int(w), int(h))
                    _frame_buffer_dims[display] = dims  # cache for next time
                    return dims
    except Exception:
        pass
    return 1920, 1080  # reasonable fallback


def _img_to_screen(x: int, y: int, display: str) -> tuple[int, int]:
    """Scale image-space coordinates to screen-space coordinates.

    desktop_look serves a downscaled JPEG (DESKTOP_LOOK_MAX_DIM). The LLM gives
    coordinates in that image's pixel space. We need to scale them back to actual
    screen resolution before passing to xdotool.
    """
    screen_w, screen_h = _get_screen_dims(display)
    if screen_w and screen_h:
        actual_max = max(screen_w, screen_h)
        if actual_max > config.DESKTOP_LOOK_MAX_DIM:
            scale = actual_max / config.DESKTOP_LOOK_MAX_DIM
            return int(round(x * scale)), int(round(y * scale))
    return x, y


async def handle_desktop_action(request: web.Request) -> web.Response:
    args = await _parse_body(request)
    from .desktop import control as desktop

    task_id = args.get("task_id")
    display = await _resolve_task_display(task_id)

    action = args["action"]
    result = {}

    if action == "click":
        x, y = args.get("x"), args.get("y")
        btn = args.get("button", 1)
        if x is not None and y is not None:
            sx, sy = _img_to_screen(x, y, display)
            ok = desktop.mouse_click_at(sx, sy, btn, display=display)
            result = {"ok": ok, "action": "click", "x": sx, "y": sy}
        else:
            ok = desktop.mouse_click(btn, display=display)
            result = {"ok": ok, "action": "click"}
    elif action == "double_click":
        x, y = args.get("x"), args.get("y")
        if x is not None and y is not None:
            x, y = _img_to_screen(x, y, display)
        ok = desktop.mouse_double_click(x, y, display=display)
        result = {"ok": ok, "action": "double_click"}
    elif action == "type":
        ok = desktop.type_text(args.get("text", ""), display=display)
        result = {"ok": ok, "action": "type"}
    elif action == "press_key":
        ok = desktop.press_key(args.get("text", "Return"), display=display)
        result = {"ok": ok, "action": "press_key"}
    elif action == "mouse_move":
        x, y = _img_to_screen(args["x"], args["y"], display)
        ok = desktop.mouse_move(x, y, display=display)
        result = {"ok": ok, "action": "mouse_move"}
    elif action == "drag":
        x1, y1 = _img_to_screen(args["x"], args["y"], display)
        x2, y2 = _img_to_screen(args["x2"], args["y2"], display)
        ok = desktop.mouse_drag(x1, y1, x2, y2, args.get("button", 1), display=display)
        result = {"ok": ok, "action": "drag"}
    elif action == "screenshot":
        path = desktop.take_screenshot(display=display)
        result = {"ok": path is not None, "path": path}
    elif action == "list_windows":
        windows = desktop.list_windows(display=display)
        result = {"windows": windows, "count": len(windows)}
    elif action == "find_window":
        wid = desktop.find_window(args.get("window_name", ""), display=display)
        if wid:
            info = desktop.get_window_info(wid, display=display)
            result = {"found": True, "window": info}
        else:
            result = {"found": False}
    elif action == "focus_window":
        wid = args.get("window_id") or desktop.find_window(args.get("window_name", ""), display=display)
        ok = desktop.focus_window(wid, display=display) if wid else False
        result = {"ok": ok}
    elif action == "resize_window":
        wid = args.get("window_id") or desktop.find_window(args.get("window_name", ""), display=display)
        ok = desktop.resize_window(wid, args["width"], args["height"], display=display) if wid else False
        result = {"ok": ok}
    elif action == "move_window":
        wid = args.get("window_id") or desktop.find_window(args.get("window_name", ""), display=display)
        ok = desktop.move_window(wid, args["x"], args["y"], display=display) if wid else False
        result = {"ok": ok}
    elif action == "close_window":
        wid = args.get("window_id") or desktop.find_window(args.get("window_name", ""), display=display)
        ok = desktop.close_window(wid, display=display) if wid else False
        result = {"ok": ok}
    elif action == "get_mouse_position":
        pos = desktop.get_mouse_position(display=display)
        result = {"x": pos[0], "y": pos[1]} if pos else {"error": "Failed"}
    else:
        result = {"error": f"Unknown action: {action}"}

    if task_id and "error" not in result:
        if action == "type":
            summary = f"type: {repr(args.get('text', ''))}"
        elif action == "press_key":
            summary = f"press_key: {args.get('text', '')}"
        elif action in ("click", "double_click"):
            summary = f"{action} at ({args.get('x')}, {args.get('y')})"
        elif action == "drag":
            summary = f"drag ({args.get('x')},{args.get('y')}) → ({args.get('x2')},{args.get('y2')})"
        else:
            summary = action
        import json as _json
        asyncio.ensure_future(task_mgr.log_action(
            task_id, "gui", summary,
            input_data=_json.dumps({k: v for k, v in args.items() if k != "task_id"}),
            output_data=_json.dumps(result),
        ))
    return web.json_response(result)


async def handle_video_record(request: web.Request) -> web.Response:
    args = await _parse_body(request)
    from .video.recorder import record_screen
    from .desktop.control import find_window

    task_id = args.get("task_id")
    display = await _resolve_task_display(task_id)

    target = args.get("target", "screen")
    duration = min(max(args.get("duration", 10), 1), 120)
    fps = args.get("fps", 5)
    window_id = None

    if target.startswith("window:"):
        window_id = find_window(target[7:], display=display)
        if not window_id:
            return web.json_response({"error": "Window not found"}, status=404)

    path = record_screen(duration=duration, fps=fps, window_id=window_id, display=display)
    if path:
        size_mb = os.path.getsize(path) / 1024 / 1024
        return web.json_response({"ok": True, "path": path, "duration": duration, "fps": fps, "size_mb": round(size_mb, 2)})
    return web.json_response({"error": "Recording failed"}, status=500)


# ─── GUI Agent (unified: Gemini supervisor + UI-TARS grounding) ─

async def handle_gui_agent(request: web.Request) -> web.Response:
    args = await _parse_body(request)
    task_id = args.get("task_id")
    instruction = args.get("instruction")

    if not instruction:
        return web.json_response({"error": "instruction is required"}, status=400)

    # Cap gui_agent budget at 240s so we always finish BEFORE OpenClaw's MCP call timeout
    # (~300s) fires and the caller stops listening. Exceeding this caused today's
    # "main LLM reports paused/failed while daemon is still successfully running" bug.
    timeout = min(int(args.get("timeout", 180)), 240)
    context = args.get("context", "")
    display = await _resolve_task_display(task_id)

    from .live_ui import get_provider
    from .live_ui.session import LiveUISession
    from . import db as _db

    # Global serialization: only ONE gui_agent runs at a time, regardless of task_id.
    # The single shared display :99 means parallel calls scramble UI state. Caller
    # decides: await the active result, or cancel it via gui_agent_cancel and retry.
    if _active_gui_sessions:
        active_key, active_rec = next(iter(_active_gui_sessions.items()))
        elapsed_s = round(time.time() - active_rec["started_at"], 1)
        active_task_id = None if active_key == _UNTAGGED_KEY else active_key
        active_sid = active_rec["session_id"]
        return web.json_response({
            "success": False,
            "status": "busy",
            "error": "another gui_agent is already running",
            "summary": (
                f"Rejected: gui_agent session {active_sid[:8]} has been running for {elapsed_s}s "
                f"on task={active_task_id or '<untagged>'}: {active_rec['instruction'][:120]!r}. "
                "To wait, retry after task_summary shows it completed. "
                "To preempt, call gui_agent_cancel and retry."
            ),
            "active_session_id": active_sid,
            "active_task_id": active_task_id,
            "active_started_at": active_rec["started_at"],
            "active_elapsed_s": elapsed_s,
            "active_instruction": active_rec["instruction"][:200],
            "actions_taken": 0,
            "actions_log": [],
        }, status=409)

    try:
        provider = get_provider()
    except ValueError as e:
        return web.json_response({"error": str(e)}, status=400)

    session_id = _db.new_id()
    session = LiveUISession(
        session_id=session_id,
        task_id=task_id,
        instruction=instruction,
        context=context,
        timeout=timeout,
    )

    registry_key = task_id or _UNTAGGED_KEY
    _active_gui_sessions[registry_key] = {
        "session_id": session_id,
        "started_at": time.time(),
        "instruction": instruction,
    }
    try:
        result = await provider.run(
            instruction=instruction,
            timeout=timeout,
            task_id=task_id,
            display=display,
            context=context,
            session=session,
        )
    finally:
        _active_gui_sessions.pop(registry_key, None)

    result["session_id"] = session_id

    if task_id:
        import json as _json
        actions = result.get("actions_taken", 0)
        gui_status = result.get("status")
        if gui_status == "partial":
            action_status = "partial"
        elif result.get("success"):
            action_status = "completed"
        else:
            action_status = "failed"
        input_data = _json.dumps({
            "session_id": session_id,
            "instruction": instruction[:200],
            "timeout": timeout,
        })
        summary = result.get("summary") or result.get("error") or "gui_agent finished without a summary"
        asyncio.ensure_future(task_mgr.log_action(
            task_id, "gui",
            f"gui_agent ({actions} actions, {gui_status or 'unknown'}): {instruction[:100]}",
            input_data=input_data,
            output_data=_json.dumps({
                **result,
                "summary": summary,
            }),
            status=action_status,
        ))

    return web.json_response(result)


async def handle_gui_agent_cancel(request: web.Request) -> web.Response:
    """POST /gui_agent/cancel — signal a running gui_agent to stop at its next turn boundary.

    Body: {"session_id": "..."} OR {"task_id": "..."} OR {"all": true}.
    Returns: {"cancelled": bool, "count": int}.
    """
    args = await _parse_body(request)
    from .live_ui.openrouter import cancel_session, cancel_all, _cancel_events

    if args.get("all"):
        n = cancel_all()
        return web.json_response({"cancelled": n > 0, "count": n, "active_before": n})

    target = args.get("session_id") or args.get("task_id")
    if not target:
        return web.json_response(
            {"error": "provide session_id or task_id (or set all=true)"}, status=400
        )
    hit = cancel_session(target)
    return web.json_response({
        "cancelled": hit,
        "target": target,
        "active_sessions": sorted(k for k in _cancel_events.keys() if not k.startswith("task:")),
    })


# ─── Live session API ────────────────────────────────────────────

async def handle_api_live_session(request: web.Request) -> web.Response:
    """GET /api/live_sessions/{session_id} — return session events."""
    session_id = request.match_info["session_id"]
    from .live_ui.session import load_session
    data = load_session(session_id)
    if data is None:
        return web.json_response({"error": "session not found"}, status=404)
    # Include file sizes so openLive() can jump to live edge
    from .live_ui.session import LIVE_SESSIONS_DIR
    session_dir = LIVE_SESSIONS_DIR / session_id
    events_path = session_dir / "events.jsonl"
    pcm_path = session_dir / "audio.pcm"
    data["events_size"] = events_path.stat().st_size if events_path.exists() else 0
    data["audio_size"] = pcm_path.stat().st_size if pcm_path.exists() else 0
    return web.json_response(data)


async def handle_api_live_session_frame(request: web.Request) -> web.Response:
    """GET /api/live_sessions/{session_id}/frames/{n} — return JPEG frame."""
    session_id = request.match_info["session_id"]
    n = int(request.match_info["n"])
    from .live_ui.session import get_frame
    jpeg = get_frame(session_id, n)
    if jpeg is None:
        return web.Response(status=404, text="Frame not found")
    return web.Response(
        body=jpeg,
        content_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


async def handle_api_live_session_audio(request: web.Request) -> web.Response:
    """GET /api/live_sessions/{session_id}/audio — return current audio as WAV (live or complete)."""
    session_id = request.match_info["session_id"]
    from .live_ui.session import get_audio_wav
    wav_bytes = get_audio_wav(session_id)
    if wav_bytes is None:
        return web.Response(status=404, text="No audio for this session")
    return web.Response(
        body=wav_bytes,
        content_type="audio/wav",
        # No cache — content grows during active sessions
        headers={"Cache-Control": "no-cache"},
    )


async def handle_api_live_sessions_active(request: web.Request) -> web.Response:
    """GET /api/live_sessions/active — return map of task_id → session_id for running sessions."""
    return web.json_response({"sessions": dict(_active_gui_sessions)})


async def handle_api_live_session_events_stream(request: web.Request) -> web.StreamResponse:
    """GET /api/live_sessions/{session_id}/events/stream — SSE tail of events.jsonl."""
    from .live_ui.session import LIVE_SESSIONS_DIR
    session_id = request.match_info["session_id"]
    events_path = LIVE_SESSIONS_DIR / session_id / "events.jsonl"
    response = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
    await response.prepare(request)
    # Support ?from=<byte_offset> so clients can resume after reconnect
    byte_offset = int(request.query.get("from", 0))
    try:
        while True:
            if events_path.exists():
                with open(events_path, "rb") as f:
                    f.seek(byte_offset)
                    chunk = f.read()
                if chunk:
                    byte_offset += len(chunk)
                    for line in chunk.decode(errors="replace").split("\n"):
                        line = line.strip()
                        if not line:
                            continue
                        # Include byte offset so client can resume on reconnect
                        await response.write(f"id: {byte_offset}\ndata: {line}\n\n".encode())
                        try:
                            ev = json.loads(line)
                            if ev.get("type") in ("done", "error", "escalate"):
                                await response.write(b"event: done\ndata: {}\n\n")
                                return response
                        except Exception:
                            pass
            # Terminate if session is no longer active and no terminal event was found
            active = any(rec.get("session_id") == session_id for rec in _active_gui_sessions.values())
            if not active and events_path.exists():
                # Session ended without terminal event (crash) — stop streaming
                break
            await asyncio.sleep(0.2)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return response


async def handle_api_live_session_audio_stream(request: web.Request) -> web.StreamResponse:
    """GET /api/live_sessions/{session_id}/audio/stream — chunked PCM stream."""
    from .live_ui.session import LIVE_SESSIONS_DIR
    session_id = request.match_info["session_id"]
    pcm_path = LIVE_SESSIONS_DIR / session_id / "audio.pcm"
    response = web.StreamResponse(headers={
        "Content-Type": "audio/pcm;rate=24000;channels=1",
        "Cache-Control": "no-cache",
    })
    await response.prepare(request)
    # Support ?from=<byte_offset> so clients can resume mid-session
    offset = int(request.query.get("from", 0))
    try:
        while True:
            if pcm_path.exists():
                with open(pcm_path, "rb") as f:
                    f.seek(offset)
                    new_data = f.read()
                if new_data:
                    await response.write(new_data)
                    offset += len(new_data)
            active = any(rec.get("session_id") == session_id for rec in _active_gui_sessions.values())
            if not active and (not pcm_path.exists() or pcm_path.stat().st_size <= offset):
                break
            await asyncio.sleep(0.05)
    except (ConnectionResetError, asyncio.CancelledError):
        pass
    return response


# ─── GUI Agent handlers (legacy gui_do/gui_find removed — use /gui_agent) ─


# ─── Memory handlers ────────────────────────────────────────────

WORKSPACE_DIR = os.environ.get("ACU_WORKSPACE", os.path.expanduser("~/.openclaw/workspace"))


def _is_within_workspace(full_path: str) -> bool:
    """True iff `full_path` is inside WORKSPACE_DIR.

    Uses commonpath rather than startswith because startswith treats
    /foo/workspace_evil as a subpath of /foo/workspace.
    """
    try:
        ws = os.path.abspath(WORKSPACE_DIR)
        fp = os.path.abspath(full_path)
        return os.path.commonpath([fp, ws]) == ws
    except (ValueError, OSError):
        return False


async def handle_memory_search(request: web.Request) -> web.Response:
    import glob, re
    args = await _parse_body(request)
    query = args.get("query", "").strip()
    max_results = int(args.get("max_results", 10))
    if not query:
        return web.json_response({"error": "query is required"}, status=400)

    workspace = WORKSPACE_DIR
    search_files = [
        os.path.join(workspace, "MEMORY.md"),
        *sorted(glob.glob(os.path.join(workspace, "memory", "*.md"))),
    ]

    terms = re.split(r"\s+", query.lower())
    results = []

    for fpath in search_files:
        if not os.path.exists(fpath):
            continue
        try:
            with open(fpath, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except Exception:
            continue

        rel_path = os.path.relpath(fpath, workspace)
        for i, line in enumerate(lines):
            lower = line.lower()
            if all(t in lower for t in terms):
                start = max(0, i - 1)
                end = min(len(lines), i + 3)
                snippet = "".join(lines[start:end]).strip()
                results.append({
                    "path": rel_path,
                    "line": i + 1,
                    "snippet": snippet[:400],
                    "score": sum(lower.count(t) for t in terms),
                })
                if len(results) >= max_results * 3:
                    break

    results.sort(key=lambda r: -r["score"])
    seen = set()
    deduped = []
    for r in results:
        key = (r["path"], r["line"] // 5)
        if key not in seen:
            seen.add(key)
            deduped.append(r)
        if len(deduped) >= max_results:
            break

    return web.json_response({"query": query, "results": deduped, "count": len(deduped)})


async def handle_memory_read(request: web.Request) -> web.Response:
    args = await _parse_body(request)
    path = args.get("path", "MEMORY.md").lstrip("/")
    offset = int(args.get("offset", 1))
    limit = int(args.get("limit", 100))

    full_path = os.path.join(WORKSPACE_DIR, path)
    if not _is_within_workspace(full_path):
        return web.json_response({"error": "path outside workspace"}, status=403)
    if not os.path.exists(full_path):
        return web.json_response({"error": f"file not found: {path}"}, status=404)

    try:
        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        sliced = lines[offset - 1: offset - 1 + limit]
        return web.json_response({
            "path": path, "offset": offset, "limit": limit,
            "total_lines": len(lines), "content": "".join(sliced),
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


async def handle_memory_append(request: web.Request) -> web.Response:
    args = await _parse_body(request)
    note = args.get("note", "").strip()
    path = args.get("path", "")

    if not note:
        return web.json_response({"error": "note is required"}, status=400)

    if path:
        full_path = os.path.join(WORKSPACE_DIR, path.lstrip("/"))
        if not _is_within_workspace(full_path):
            return web.json_response({"error": "path outside workspace"}, status=403)
    else:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        full_path = os.path.join(WORKSPACE_DIR, "memory", f"{today}.md")

    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
    entry = f"\n## [{ts}] Note\n{note}\n"

    try:
        with open(full_path, "a", encoding="utf-8") as f:
            f.write(entry)
        rel = os.path.relpath(full_path, WORKSPACE_DIR)
        return web.json_response({"ok": True, "path": rel, "appended": entry.strip()})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# ─── App setup ──────────────────────────────────────────────────

@web.middleware
async def debug_middleware(request: web.Request, handler):
    import time as _time
    start = _time.time()
    try:
        response = await handler(request)
        elapsed = (_time.time() - start) * 1000
        debug.log_http(request.method, request.path, response.status, elapsed)
        return response
    except Exception:
        elapsed = (_time.time() - start) * 1000
        debug.log_http(request.method, request.path, 500, elapsed)
        raise


def create_app() -> web.Application:
    app = web.Application(middlewares=[debug_middleware])
    # Wait
    app.router.add_post("/smart_wait", handle_smart_wait)
    app.router.add_post("/wait_status", handle_wait_status)
    app.router.add_post("/wait_update", handle_wait_update)
    app.router.add_post("/wait_cancel", handle_wait_cancel)
    # Tasks (hierarchical)
    app.router.add_post("/task_register", handle_task_register)
    app.router.add_post("/task_update", handle_task_update)
    app.router.add_post("/task_item_update", handle_task_item_update)
    app.router.add_post("/task_plan_append", handle_task_plan_append)
    app.router.add_post("/task_log_action", handle_task_log_action)
    app.router.add_post("/task_summary", handle_task_summary)
    app.router.add_post("/task_drill_down", handle_task_drill_down)
    app.router.add_post("/task_thread", handle_task_thread)
    app.router.add_post("/task_list", handle_task_list)
    # Health
    app.router.add_get("/health", handle_health)
    # Desktop
    app.router.add_post("/desktop_look", handle_desktop_look)
    app.router.add_post("/desktop_action", handle_desktop_action)
    app.router.add_post("/video_record", handle_video_record)
    # GUI agent (unified)
    app.router.add_post("/gui_agent", handle_gui_agent)
    app.router.add_post("/gui_agent/cancel", handle_gui_agent_cancel)
    app.router.add_post("/live_ui", handle_gui_agent)  # backward compat
    # Memory
    app.router.add_post("/memory_search", handle_memory_search)
    app.router.add_post("/memory_read", handle_memory_read)
    app.router.add_post("/memory_append", handle_memory_append)
    # Dashboard (GET APIs + static files)
    app.router.add_get("/dashboard", handle_dashboard_index)
    app.router.add_get("/api/tasks", handle_api_tasks)
    app.router.add_get("/api/tasks/{task_id}", handle_api_task_detail)
    app.router.add_post("/api/tasks/{task_id}/status", handle_api_task_status)
    app.router.add_delete("/api/tasks/{task_id}", handle_api_task_delete)
    app.router.add_get("/api/tasks/{task_id}/messages", handle_api_task_messages)
    app.router.add_get("/api/tasks/{task_id}/items/{ordinal}", handle_api_task_item)
    app.router.add_get("/api/tasks/{task_id}/screen", handle_api_task_screen)
    app.router.add_get("/api/tasks/{task_id}/snapshot", handle_api_task_snapshot)
    app.router.add_get("/api/tasks/{task_id}/frames", handle_api_task_frames)
    app.router.add_get("/api/tasks/{task_id}/frames/{frame_n}", handle_api_task_frame)
    app.router.add_get("/api/tasks/{task_id}/video", handle_api_task_video)
    app.router.add_get("/api/tasks/{task_id}/export", handle_api_task_export)
    app.router.add_get("/api/logs", handle_api_logs)
    app.router.add_get("/api/dump", handle_api_dump)
    app.router.add_get("/api/snapshot", handle_api_snapshot)
    app.router.add_get("/api/waits", handle_api_waits)
    app.router.add_get("/api/health", handle_api_health)
    app.router.add_get("/doctor", handle_doctor)
    app.router.add_get("/api/doctor", handle_doctor)
    app.router.add_get("/humanize", handle_humanize_get)
    app.router.add_post("/humanize", handle_humanize_set)
    app.router.add_get("/api/humanize", handle_humanize_get)
    app.router.add_post("/api/humanize", handle_humanize_set)
    app.router.add_get("/api/usage/stats", handle_api_usage_stats)
    app.router.add_get("/api/screen", handle_api_screen_stream)
    app.router.add_get("/api/screenshots/{filename}", handle_api_screenshot)
    app.router.add_get("/api/recordings/{filename}", handle_api_recording)
    # Live session viewer — /active must be registered before {session_id} routes
    app.router.add_get("/api/live_sessions/active", handle_api_live_sessions_active)
    app.router.add_get("/api/live_sessions/{session_id}/events/stream", handle_api_live_session_events_stream)
    app.router.add_get("/api/live_sessions/{session_id}/audio/stream", handle_api_live_session_audio_stream)
    app.router.add_get("/api/live_sessions/{session_id}", handle_api_live_session)
    app.router.add_get("/api/live_sessions/{session_id}/frames/{n}", handle_api_live_session_frame)
    app.router.add_get("/api/live_sessions/{session_id}/audio", handle_api_live_session_audio)
    # Recording start/stop/status
    app.router.add_post("/api/tasks/{task_id}/record/start", handle_record_start)
    app.router.add_post("/api/tasks/{task_id}/record/stop", handle_record_stop)
    app.router.add_get("/api/tasks/{task_id}/record/status", handle_record_status)
    app.router.add_static("/dashboard/static", DASHBOARD_DIR)
    return app


async def storage_cleanup_loop():
    """Background loop that enforces ACU_MAX_RECORDINGS_MB by pruning oldest task recordings."""
    if config.MAX_RECORDINGS_MB <= 0:
        return
    max_bytes = config.MAX_RECORDINGS_MB * 1024 * 1024
    while True:
        await asyncio.sleep(3600)  # check every hour
        try:
            current = frame_recorder.recordings_size_bytes()
            if current > max_bytes:
                freed = frame_recorder.prune_oldest_recordings(max_bytes)
                if freed:
                    debug.log("STORAGE", f"Pruned {freed // (1024*1024)}MB of old recordings (cap: {config.MAX_RECORDINGS_MB}MB)")
        except Exception as e:
            debug.log("ERROR", f"Storage cleanup error: {e}")


async def stuck_detection_loop():
    """Background loop that checks for stuck tasks and wakes OpenClaw."""
    import subprocess

    debug.log("DAEMON", "Stuck detection loop started (checking every 60s)")
    while True:
        await asyncio.sleep(task_mgr.STUCK_CHECK_INTERVAL)
        try:
            alerts = await task_mgr.check_stuck_tasks()
            for alert in alerts:
                packet = alert.get("packet") or {
                    "task_id": alert.get("task_id"),
                    "name": alert.get("name"),
                    "reason": alert.get("reason"),
                }
                msg = f"[task_stuck_resume] {json.dumps(packet, ensure_ascii=False)}"
                debug.log_openclaw_event("stuck_alert", msg)
                try:
                    subprocess.run(
                        [config.OPENCLAW_CLI, "system", "event", "--text", msg, "--mode", "now"],
                        capture_output=True, text=True, timeout=10,
                    )
                except Exception as e:
                    debug.log("ERROR", f"Failed to inject stuck alert: {e}")
        except Exception as e:
            debug.log("ERROR", f"Stuck detection error: {e}")


def main():
    import sys as _sys
    enable_debug = "--debug" in _sys.argv or os.environ.get("ACU_DEBUG", "0") in ("1", "true", "yes")
    config.ensure_data_dir()
    debug.init(enabled=enable_debug)
    debug.log("DAEMON", f"Starting DETM daemon on {DAEMON_HOST}:{DAEMON_PORT}")
    debug.log("DAEMON", f"Display: {config.DISPLAY}, Vision: {config.VISION_BACKEND}/{config.VISION_MODEL}")
    debug.log("DAEMON", f"Data dir: {config.DATA_DIR}")
    log.info(f"Starting DETM daemon on {DAEMON_HOST}:{DAEMON_PORT}")
    app = create_app()

    async def on_startup(app):
        if config.STUCK_DETECTION_ENABLED:
            app['stuck_detector'] = asyncio.create_task(stuck_detection_loop())
        else:
            app['stuck_detector'] = None
        app['storage_cleanup'] = asyncio.create_task(storage_cleanup_loop())
        # Pre-warm frame buffer for the system display so the dashboard
        # shows a live screen immediately, even before any task is created.
        _ensure_frame_buffer(config.DISPLAY)
        # Resume frame recording for any tasks that are still active.
        # Without this, a daemon restart silently drops recording for in-progress tasks.
        try:
            result = await task_mgr.list_tasks(status="active", limit=50)
            for t in result.get("tasks", []):
                tid = t.get("id") or t.get("task_id")
                if tid:
                    display = await _resolve_task_display(tid)
                    frame_recorder.start(tid, display)
                    log.info(f"Resumed frame recording for active task {tid}")
        except Exception as e:
            log.warning(f"Failed to resume frame recording on startup: {e}")

    async def on_cleanup(app):
        if app.get('stuck_detector'):
            app['stuck_detector'].cancel()
            try:
                await app['stuck_detector']
            except asyncio.CancelledError:
                pass
        if app.get('storage_cleanup'):
            app['storage_cleanup'].cancel()
            try:
                await app['storage_cleanup']
            except asyncio.CancelledError:
                pass
        from .display.manager import cleanup_all
        cleanup_all()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    web.run_app(app, host=DAEMON_HOST, port=DAEMON_PORT, print=lambda msg: log.info(msg))


if __name__ == "__main__":
    main()
