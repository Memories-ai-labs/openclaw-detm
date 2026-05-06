"""Sync cookies from the user's main Chrome profile (the one DETM
operates on display :99) into the bench Chromium profile that
Playwright MCP uses.

Why this matters: Family A (Playwright MCP) and Family B (DETM) need to
hit the same logged-in state for tier-1 LinkedIn tasks to be a fair
comparison. DETM uses whatever Chrome is up on :99; that profile lives
at ~/.config/google-chrome/. Playwright MCP launches its own Chromium
with --user-data-dir=~/.bench-chromium-profile/. Without sync,
Playwright sees a cookieless browser and hits the LinkedIn login wall.

Cookies in Chrome are encrypted with a key in `Local State` derived
from the OS user keyring. Both Chromium instances run as the same user
so the same keyring backs them — copying `Local State` along with
`Cookies` lets the bench Chromium decrypt the copied cookies.

Caveats:
  - If the source Chrome is actively writing the Cookies file at the
    moment of copy, you may get a slightly stale snapshot. SQLite read
    locks don't block reads, so we won't corrupt anything.
  - If the bench Chromium is RUNNING, do not call this — it holds an
    exclusive lock and you'll get permission errors. The runner cleans
    up its Chromium between trials, so calling sync between sweeps is
    safe.
  - Copies the leveldb-based local storage dirs too (best-effort), so
    auth-token-based sites (some Google services) survive.

Usage as a CLI:
    python -m gui_comparison.runners.cookie_sync                    # sync once
    python -m gui_comparison.runners.cookie_sync --check            # report only
    python -m gui_comparison.runners.cookie_sync --src PATH --dst PATH  # custom
"""
from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import sys
from pathlib import Path

DEFAULT_SRC = Path.home() / ".config/google-chrome"
DEFAULT_DST = Path(os.environ.get(
    "BENCH_CHROMIUM_PROFILE",
    str(Path.home() / ".bench-chromium-profile"),
))

# Auth-relevant files to mirror src → dst.
# `Local State` is at the profile root (NOT under Default/) and holds
# the encryption key. Without it, the copied Cookies are unreadable.
_FILES = [
    ("Local State", False),                    # path, is_dir
    ("Default/Cookies", False),
    ("Default/Login Data", False),
    ("Default/Local Storage/leveldb", True),   # for token-based sites
    ("Default/Session Storage", True),
]


def _bench_chromium_running(dst: Path) -> bool:
    """Heuristic: if a SingletonLock pid file exists and points at a live
    process, the profile is in use."""
    lock = dst / "SingletonLock"
    if not lock.is_symlink() and not lock.exists():
        return False
    try:
        target = os.readlink(lock) if lock.is_symlink() else None
        # Symlink format is "hostname-pid".
        if target and "-" in target:
            pid = int(target.rsplit("-", 1)[-1])
            os.kill(pid, 0)  # Raises if no such process
            return True
    except (OSError, ValueError):
        pass
    return False


def _count_cookies(db_path: Path) -> tuple[int, int]:
    """Returns (total_cookies, linkedin_cookies). 0,0 if anything fails."""
    if not db_path.exists():
        return 0, 0
    try:
        # Copy first to avoid sqlite lock contention.
        tmp = Path(f"/tmp/_cookies_inspect_{os.getpid()}.db")
        shutil.copy2(db_path, tmp)
        try:
            con = sqlite3.connect(str(tmp))
            cur = con.cursor()
            total = cur.execute("SELECT COUNT(*) FROM cookies").fetchone()[0]
            li = cur.execute(
                "SELECT COUNT(*) FROM cookies WHERE host_key LIKE '%linkedin%'"
            ).fetchone()[0]
            con.close()
            return total, li
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
    except Exception:
        return 0, 0


def report(src: Path = DEFAULT_SRC, dst: Path = DEFAULT_DST) -> None:
    src_total, src_li = _count_cookies(src / "Default/Cookies")
    dst_total, dst_li = _count_cookies(dst / "Default/Cookies")
    print(f"  Source Chrome     ({src}):  {src_total} cookies, {src_li} linkedin")
    print(f"  Bench Chromium    ({dst}):  {dst_total} cookies, {dst_li} linkedin")
    if _bench_chromium_running(dst):
        print(f"  ⚠ Bench Chromium appears to be RUNNING — sync will fail")


def sync(src: Path = DEFAULT_SRC, dst: Path = DEFAULT_DST,
         verbose: bool = True) -> None:
    """Copy cookies + encryption state from src → dst. Bench Chromium
    must NOT be running."""
    if _bench_chromium_running(dst):
        raise RuntimeError(
            f"Bench Chromium profile {dst} is in use. Stop Playwright MCP "
            f"or close the Chromium window, then re-run."
        )
    if not src.exists():
        raise RuntimeError(f"Source Chrome profile not found at {src}")
    dst.mkdir(parents=True, exist_ok=True)

    copied = 0
    for rel, is_dir in _FILES:
        s = src / rel
        d = dst / rel
        if not s.exists():
            if verbose:
                print(f"  skip (missing): {rel}")
            continue
        d.parent.mkdir(parents=True, exist_ok=True)
        try:
            if is_dir:
                if d.exists():
                    shutil.rmtree(d)
                shutil.copytree(s, d)
            else:
                shutil.copy2(s, d)
            copied += 1
            if verbose:
                print(f"  ✓ {rel}")
        except Exception as e:
            if verbose:
                print(f"  ✗ {rel}: {e}")

    if verbose:
        print(f"\nSynced {copied}/{len(_FILES)} entries.")
        report(src, dst)


def main() -> int:
    p = argparse.ArgumentParser(description="Sync Chrome cookies → bench Chromium")
    p.add_argument("--src", type=Path, default=DEFAULT_SRC,
                   help=f"Source Chrome profile dir (default: {DEFAULT_SRC})")
    p.add_argument("--dst", type=Path, default=DEFAULT_DST,
                   help=f"Bench Chromium profile dir (default: {DEFAULT_DST})")
    p.add_argument("--check", action="store_true",
                   help="Report cookie counts in both profiles, don't copy")
    args = p.parse_args()

    if args.check:
        report(args.src, args.dst)
        return 0
    try:
        sync(args.src, args.dst)
        return 0
    except RuntimeError as e:
        print(f"\n✗ {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
