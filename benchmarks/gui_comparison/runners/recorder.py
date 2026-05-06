"""Unified screen recorder for benchmark trials.

ffmpeg captures display :99 to <run_dir>/recording.mp4 for the duration
of a trial. Single recorder works across all 3 families because they
all act (visibly or not) on :99:

  - DETM: Chrome on :99
  - Agent S3: pyautogui on :99
  - Playwright MCP: ONLY when BENCH_PLAYWRIGHT_HEADLESS=0 (otherwise
    Chromium renders off-screen and the recording is blank). Headless
    Playwright would need its own recordVideo support — out of scope.

Set BENCH_RECORD_VIDEO=1 to enable. Disabled by default to keep result
folders small (recordings can be 10-50MB each, × 165 trials = a lot).

Usage:
    from .recorder import DisplayRecorder
    rec = DisplayRecorder(out_path=run_dir / "recording.mp4")
    rec.start()
    try:
        ... run task ...
    finally:
        rec.stop()
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

DISPLAY = os.environ.get("BENCH_DISPLAY", ":99")
ENABLED = os.environ.get("BENCH_RECORD_VIDEO", "0") == "1"
FRAMERATE = int(os.environ.get("BENCH_RECORD_FPS", "8"))


class DisplayRecorder:
    """ffmpeg x11grab wrapper. Stop is best-effort — we send SIGTERM and
    give ffmpeg up to 10s to flush the moov atom."""

    def __init__(self, out_path: Path, display: str = DISPLAY,
                 framerate: int = FRAMERATE, video_size: str = "1920x1080"):
        self.out_path = Path(out_path)
        self.display = display
        self.framerate = framerate
        self.video_size = video_size
        self.proc: subprocess.Popen | None = None

    def start(self) -> bool:
        """Start recording. Returns False if disabled or ffmpeg missing."""
        if not ENABLED:
            return False
        if not shutil.which("ffmpeg"):
            print("  ⚠ ffmpeg not found — skipping screen recording")
            return False
        self.out_path.parent.mkdir(parents=True, exist_ok=True)
        # ultrafast preset + libx264 keeps file sizes manageable while
        # avoiding CPU contention with the agent loop.
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error", "-nostdin",
            "-f", "x11grab",
            "-framerate", str(self.framerate),
            "-video_size", self.video_size,
            "-i", self.display,
            "-c:v", "libx264", "-preset", "ultrafast",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            str(self.out_path),
        ]
        try:
            self.proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
        except Exception as e:
            print(f"  ⚠ recorder start failed: {e}")
            return False
        return True

    def stop(self) -> None:
        if not self.proc:
            return
        if self.proc.poll() is not None:
            # Already exited (probably crashed)
            return
        try:
            # SIGINT = ffmpeg's "graceful stop" — flushes the moov atom.
            self.proc.send_signal(2)  # SIGINT
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        except Exception:
            pass
        self.proc = None

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *args):
        self.stop()
        return False
