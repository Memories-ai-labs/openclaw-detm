"""Export Chrome's cookies as a Playwright `storage state` JSON.

Why not just copy the Cookies sqlite file?
  - Chrome on Linux uses v11 encryption: AES-128-CBC with a key from
    libsecret/gnome-keyring (NOT the hardcoded "peanuts" key from v10).
  - When Playwright Chromium opens a copied profile, it tries the same
    keyring lookup. Even if libsecret is available, Chromium can't
    locate the right entry and silently DROPS every encrypted cookie.
  - Result: the bench profile ends up with only unencrypted cookies
    (e.g. `bcookie`), and tier-1 LinkedIn tasks hit the auth wall.

Solution: decrypt cookies in Python (using the keyring entry directly),
write a Playwright storage-state JSON, and pass `--storage-state PATH`
to @playwright/mcp. Playwright loads pre-decrypted cookies into the
context — no re-encryption / keyring lookup needed.

Usage:
  python -m gui_comparison.runners.cookie_sync                # export
  python -m gui_comparison.runners.cookie_sync --check        # report
  python -m gui_comparison.runners.cookie_sync --out PATH     # custom path
  python -m gui_comparison.runners.cookie_sync --src PATH     # custom Chrome
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
from pathlib import Path
from typing import Optional

DEFAULT_SRC = Path.home() / ".config/google-chrome"
# Per-profile storage state lives next to the bench profile dir.
DEFAULT_OUT = Path(os.environ.get(
    "BENCH_STORAGE_STATE",
    str(Path.home() / ".bench-storage-state.json"),
))

# Chrome's epoch is 1601-01-01 UTC; unix epoch is 1970-01-01.
# Chrome stores expires_utc as microseconds since 1601.
_CHROME_TO_UNIX_OFFSET_S = 11644473600


# ── Decryption ───────────────────────────────────────────────────────────

def _get_keyring_password(label: str = "Chrome Safe Storage") -> Optional[bytes]:
    """Pull Chrome's encryption secret from libsecret / gnome-keyring."""
    try:
        import secretstorage
    except ImportError:
        return None
    try:
        bus = secretstorage.dbus_init()
        for col in secretstorage.get_all_collections(bus):
            for item in col.get_all_items():
                if item.get_label() == label:
                    return item.get_secret()
    except Exception:
        return None
    return None


def _derive_key(password: bytes) -> bytes:
    """Chrome on Linux: PBKDF2-SHA1(password, 'saltysalt', 1 iteration, 16 bytes)."""
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    return PBKDF2HMAC(
        algorithm=hashes.SHA1(),
        length=16,
        salt=b"saltysalt",
        iterations=1,
    ).derive(password)


def _decrypt_value(encrypted: bytes,
                   keyring_key: Optional[bytes],
                   basic_key: bytes) -> Optional[bytes]:
    """Decrypt a single Chrome cookie. Returns plaintext bytes or None."""
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    if not encrypted:
        return b""
    prefix = encrypted[:3]
    if prefix not in (b"v10", b"v11"):
        # Unencrypted (older Chrome / some platforms). Take as-is.
        return encrypted
    body = encrypted[3:]
    iv = b" " * 16
    # v10 → basic key; v11 → keyring key.
    candidates: list[bytes] = []
    if prefix == b"v10":
        candidates.append(basic_key)
    elif prefix == b"v11" and keyring_key is not None:
        candidates.append(keyring_key)
    # Always try basic_key as fallback (some setups encrypt v11 with peanuts).
    if basic_key not in candidates:
        candidates.append(basic_key)
    for key in candidates:
        try:
            d = Cipher(algorithms.AES(key), modes.CBC(iv)).decryptor()
            plain = d.update(body) + d.finalize()
            if not plain:
                continue
            pad = plain[-1]
            if pad < 1 or pad > 16:
                continue
            plain = plain[:-pad]
            # v11 prepends a 32-byte SHA256 integrity hash — strip it.
            if prefix == b"v11" and len(plain) >= 32:
                plain = plain[32:]
            return plain
        except Exception:
            continue
    return None


# ── Cookie → storage state row conversion ────────────────────────────────

_SAMESITE_MAP = {
    -1: "None", 0: "None", 1: "None", 2: "Lax", 3: "Strict",
}


def _chrome_expires_to_unix(expires_utc: int) -> float:
    """Chrome microseconds since 1601 → unix seconds. Returns -1 for
    session cookies (expires_utc == 0)."""
    if not expires_utc:
        return -1
    return (expires_utc / 1_000_000) - _CHROME_TO_UNIX_OFFSET_S


def _row_to_cookie(row: sqlite3.Row, keyring_key: Optional[bytes],
                   basic_key: bytes) -> Optional[dict]:
    enc = row["encrypted_value"]
    if not enc and not row["value"]:
        return None
    if enc:
        plain = _decrypt_value(enc, keyring_key, basic_key)
        if plain is None:
            return None
        try:
            value = plain.decode("utf-8")
        except UnicodeDecodeError:
            return None
    else:
        value = row["value"]

    return {
        "name": row["name"],
        "value": value,
        "domain": row["host_key"],
        "path": row["path"],
        "expires": _chrome_expires_to_unix(row["expires_utc"]),
        "httpOnly": bool(row["is_httponly"]),
        "secure": bool(row["is_secure"]),
        "sameSite": _SAMESITE_MAP.get(row["samesite"], "Lax"),
    }


# ── Public API ───────────────────────────────────────────────────────────

def export_storage_state(
    src: Path = DEFAULT_SRC,
    out_path: Path = DEFAULT_OUT,
    verbose: bool = True,
) -> int:
    """Decrypt every cookie in src/Default/Cookies and write a Playwright
    storage state JSON to out_path. Returns the number of cookies exported.
    """
    cookies_db = src / "Default" / "Cookies"
    if not cookies_db.exists():
        raise RuntimeError(f"Source Chrome cookies not found at {cookies_db}")

    keyring_key_raw = _get_keyring_password("Chrome Safe Storage")
    if keyring_key_raw is None and verbose:
        print("  ⚠ Chrome Safe Storage not found in libsecret — v11 cookies "
              "may fail to decrypt (you'll get unencrypted cookies only).")

    keyring_key = _derive_key(keyring_key_raw) if keyring_key_raw else None
    basic_key = _derive_key(b"peanuts")

    # Snapshot the sqlite to avoid lock contention with running Chrome.
    tmp = Path(f"/tmp/_cookies_export_{os.getpid()}.db")
    shutil.copy2(cookies_db, tmp)
    try:
        con = sqlite3.connect(str(tmp))
        con.row_factory = sqlite3.Row
        rows = con.execute("""
            SELECT host_key, name, value, encrypted_value, path,
                   expires_utc, is_httponly, is_secure, samesite
            FROM cookies
        """).fetchall()
        con.close()
    finally:
        try:
            tmp.unlink()
        except FileNotFoundError:
            pass

    cookies = []
    n_total = 0
    n_decrypted = 0
    n_skipped = 0
    for row in rows:
        n_total += 1
        c = _row_to_cookie(row, keyring_key, basic_key)
        if c is None:
            n_skipped += 1
            continue
        n_decrypted += 1
        cookies.append(c)

    state = {"cookies": cookies, "origins": []}

    # Don't clobber a previously-good storage state with a degraded
    # version: if the new export has NO li_at but a prior export DID,
    # something's wrong (keyring access broke?) and we'd silently lose
    # logged-in state. Fail-safe: keep the old file.
    new_li_at = sum(
        1 for c in cookies
        if c.get("name") == "li_at" and "linkedin" in (c.get("domain") or "")
    )
    if out_path.exists() and new_li_at == 0:
        try:
            old_state = json.loads(out_path.read_text())
            old_li_at = sum(
                1 for c in (old_state.get("cookies") or [])
                if c.get("name") == "li_at"
                and "linkedin" in (c.get("domain") or "")
            )
            if old_li_at > 0:
                if verbose:
                    print(
                        f"  ⚠ New export has no li_at but existing "
                        f"{out_path} does — keeping the existing file. "
                        f"(Likely keyring access transient failure.)"
                    )
                return n_decrypted
        except Exception:
            pass

    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write: tmp + rename so a reader never sees a half-written
    # state file.
    tmp = out_path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.chmod(0o600)
    tmp.replace(out_path)

    if verbose:
        n_li = sum(1 for c in cookies if "linkedin" in c["domain"])
        n_li_at = sum(1 for c in cookies
                      if "linkedin" in c["domain"] and c["name"] == "li_at")
        print(f"  exported {n_decrypted}/{n_total} cookies "
              f"({n_li} linkedin, li_at={'yes' if n_li_at else 'NO'}) → {out_path}")
        if n_skipped:
            print(f"  skipped {n_skipped} cookies (decryption failed or empty)")
    return n_decrypted


def report(src: Path = DEFAULT_SRC, out_path: Path = DEFAULT_OUT) -> None:
    cookies_db = src / "Default" / "Cookies"
    if cookies_db.exists():
        # Quick sqlite count.
        tmp = Path(f"/tmp/_cookies_report_{os.getpid()}.db")
        shutil.copy2(cookies_db, tmp)
        try:
            con = sqlite3.connect(str(tmp))
            total = con.execute("SELECT COUNT(*) FROM cookies").fetchone()[0]
            li = con.execute(
                "SELECT COUNT(*) FROM cookies WHERE host_key LIKE '%linkedin%'"
            ).fetchone()[0]
            li_at = con.execute(
                "SELECT COUNT(*) FROM cookies WHERE name='li_at'"
            ).fetchone()[0]
            con.close()
            print(f"  Source Chrome    ({src}):  {total} cookies, "
                  f"{li} linkedin, li_at={'yes' if li_at else 'NO'}")
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass
    else:
        print(f"  Source Chrome    ({src}):  (no Cookies file)")

    if out_path.exists():
        try:
            state = json.loads(out_path.read_text())
            cs = state.get("cookies") or []
            li = sum(1 for c in cs if "linkedin" in c.get("domain", ""))
            li_at = sum(1 for c in cs
                        if c.get("name") == "li_at" and "linkedin" in c.get("domain", ""))
            print(f"  Storage state    ({out_path}):  {len(cs)} cookies, "
                  f"{li} linkedin, li_at={'yes' if li_at else 'NO'}")
        except Exception as e:
            print(f"  Storage state    ({out_path}):  unreadable ({e})")
    else:
        print(f"  Storage state    ({out_path}):  (does not exist)")


# Backward-compat alias for the previous file-copy API name.
def sync(*args, **kwargs):
    """Deprecated alias — calls export_storage_state."""
    return export_storage_state(*args, **kwargs)


def main() -> int:
    p = argparse.ArgumentParser(
        description="Export Chrome cookies as a Playwright storage state JSON"
    )
    p.add_argument("--src", type=Path, default=DEFAULT_SRC)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT,
                   help=f"Path to write storage state (default: {DEFAULT_OUT})")
    p.add_argument("--check", action="store_true",
                   help="Report cookie counts, don't export")
    args = p.parse_args()

    if args.check:
        report(args.src, args.out)
        return 0
    try:
        export_storage_state(args.src, args.out)
        report(args.src, args.out)
        return 0
    except RuntimeError as e:
        print(f"\n✗ {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
