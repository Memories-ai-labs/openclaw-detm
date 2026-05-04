"""Per-task background frame recorder — captures screenshots at a fixed interval."""
import asyncio
import logging
import shutil
import subprocess
from pathlib import Path
from .. import config

log = logging.getLogger(__name__)

FRAME_INTERVAL = 0.5   # seconds between frames
FRAME_QUALITY  = 50    # lower quality to save disk
FRAME_MAX_DIM  = 1280

_tasks: dict[str, asyncio.Task] = {}  # task_id → asyncio capture loop
_encoding: set[str] = set()           # task_ids currently being encoded to video


def frames_dir(task_id: str) -> Path:
    return config.DATA_DIR / "recordings" / task_id


def video_path(task_id: str) -> Path:
    return config.DATA_DIR / "recordings" / f"{task_id}.mp4"


def is_encoding(task_id: str) -> bool:
    return task_id in _encoding


def start(task_id: str, display: str) -> None:
    """Start background frame capture for a task (idempotent)."""
    if task_id in _tasks:
        return
    d = frames_dir(task_id)
    d.mkdir(parents=True, exist_ok=True)
    loop_task = asyncio.ensure_future(_loop(task_id, display, d))
    _tasks[task_id] = loop_task
    log.debug(f"Frame recorder started: task={task_id} display={display}")


def stop(task_id: str) -> None:
    """Stop frame capture for a task."""
    t = _tasks.pop(task_id, None)
    if t:
        t.cancel()
        log.debug(f"Frame recorder stopped: task={task_id}")


def cleanup(task_id: str) -> None:
    """Delete the frames directory for a task (call after stop)."""
    d = frames_dir(task_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
        log.debug(f"Frame recorder cleaned up recordings: task={task_id}")


async def create_video(task_id: str) -> bool:
    """Encode all frames for a task into a compressed MP4, then delete the frames dir."""
    d = frames_dir(task_id)
    if not d.exists() or not list(d.glob("*.jpg")):
        log.debug(f"create_video: no frames for task={task_id}")
        return False

    out = video_path(task_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    _encoding.add(task_id)
    try:
        loop = asyncio.get_event_loop()
        cmd = [
            "ffmpeg", "-y",
            "-framerate", str(round(1.0 / FRAME_INTERVAL)),  # 2fps
            "-i", str(d / "%06d.jpg"),
            "-c:v", "libx264",
            "-crf", "28",
            "-preset", "fast",
            "-pix_fmt", "yuv420p",
            str(out),
        ]

        def _run() -> int:
            result = subprocess.run(cmd, capture_output=True, timeout=300)
            return result.returncode

        rc = await loop.run_in_executor(None, _run)
        if rc == 0 and out.exists():
            shutil.rmtree(d, ignore_errors=True)
            log.info(f"Video created: task={task_id} size={out.stat().st_size // 1024}KB")
            return True
        else:
            log.warning(f"ffmpeg failed for task={task_id} rc={rc}")
            return False
    except Exception as e:
        log.warning(f"create_video error task={task_id}: {e}")
        return False
    finally:
        _encoding.discard(task_id)


def recordings_size_bytes() -> int:
    """Return total bytes used by all task recordings (MP4s + in-progress JPEG frames).

    Completed tasks are encoded to <task_id>.mp4 and the JPEG frame dir is deleted, so
    a JPG-only sum under-reports retained storage by the size of every encoded video.
    """
    root = config.DATA_DIR / "recordings"
    if not root.exists():
        return 0
    total = 0
    for f in root.rglob("*"):
        if f.is_file() and f.suffix.lower() in (".jpg", ".mp4"):
            try:
                total += f.stat().st_size
            except OSError:
                pass
    return total


def prune_oldest_recordings(max_bytes: int) -> int:
    """Delete oldest task recording dirs until total size is under max_bytes. Returns bytes freed."""
    root = config.DATA_DIR / "recordings"
    if not root.exists():
        return 0
    task_dirs = sorted(
        (d for d in root.iterdir() if d.is_dir()),
        key=lambda d: d.stat().st_mtime,
    )
    freed = 0
    current = recordings_size_bytes()
    for td in task_dirs:
        if current <= max_bytes:
            break
        dir_size = sum(f.stat().st_size for f in td.rglob("*.jpg") if f.is_file())
        shutil.rmtree(td, ignore_errors=True)
        freed += dir_size
        current -= dir_size
        log.info(f"Storage cap: pruned old recordings {td.name} ({dir_size // (1024*1024)}MB)")
    return freed


def list_frames(task_id: str) -> list[int]:
    d = frames_dir(task_id)
    if not d.exists():
        return []
    return sorted(int(f.stem) for f in d.glob("*.jpg"))


def get_frame(task_id: str, frame_n: int) -> bytes | None:
    path = frames_dir(task_id) / f"{frame_n:06d}.jpg"
    return path.read_bytes() if path.exists() else None


def frame_count(task_id: str) -> int:
    d = frames_dir(task_id)
    return len(list(d.glob("*.jpg"))) if d.exists() else 0


async def _loop(task_id: str, display: str, d: Path) -> None:
    from .screen import capture_screen_with_cursor, frame_to_jpeg
    import concurrent.futures
    n = frame_count(task_id)   # resume numbering if restarted
    loop = asyncio.get_event_loop()
    try:
        while True:
            try:
                frame = await loop.run_in_executor(None, capture_screen_with_cursor, display)
                if frame is not None:
                    jpeg = await loop.run_in_executor(
                        None, frame_to_jpeg, frame, FRAME_MAX_DIM, FRAME_QUALITY
                    )
                    path = d / f"{n:06d}.jpg"
                    await loop.run_in_executor(None, path.write_bytes, jpeg)
                    n += 1
            except concurrent.futures.CancelledError:
                raise asyncio.CancelledError
            except Exception as e:
                log.warning(f"Frame recorder error task={task_id}: {e}")
            await asyncio.sleep(FRAME_INTERVAL)
    except asyncio.CancelledError:
        pass
