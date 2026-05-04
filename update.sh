#!/usr/bin/env bash
# update.sh — fast in-place update of an existing DETM install.
#
# Use this for day-to-day `git pull` updates: it skips system packages,
# browser install, service creation, and XFCE config (all of which
# install.sh handles on first install). It only does what's needed to
# pick up new code:
#
#   1. git fetch + pull  (warns if working tree is dirty)
#   2. pip install -e .  (incremental — only re-installs changed deps)
#   3. systemctl restart detm-daemon
#   4. detm-doctor --quiet  (verify post-restart health)
#
# When in doubt, fall back to ./install.sh — it's idempotent and does
# the same thing, just slower.
#
# Usage:
#   ./update.sh             # update + restart + verify
#   ./update.sh --no-restart # pull + pip only, leave daemon running stale code
#   ./update.sh --check      # show what's new on origin/master, don't pull

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="$REPO_DIR/.venv"
DAEMON_PORT=18790

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
DIM='\033[2m'
NC='\033[0m'

step() { echo -e "\n${CYAN}▶${NC} $*"; }
ok()   { echo -e "${GREEN}  ✓${NC} $*"; }
warn() { echo -e "${YELLOW}  ⚠${NC} $*"; }
err()  { echo -e "${RED}  ✗${NC} $*"; }

NO_RESTART=0
CHECK_ONLY=0
for arg in "$@"; do
    case "$arg" in
        --no-restart) NO_RESTART=1 ;;
        --check)      CHECK_ONLY=1 ;;
        -h|--help)
            sed -n '3,21p' "$0" | sed 's/^# \?//'
            exit 0 ;;
        *) err "Unknown flag: $arg"; exit 2 ;;
    esac
done

cd "$REPO_DIR"

# ── 1. Sanity ────────────────────────────────────────────────────
[ -d .git ] || { err "Not a git repo: $REPO_DIR"; exit 1; }
[ -x "$VENV_DIR/bin/python3" ] || { err "venv missing — run ./install.sh first"; exit 1; }

# ── 2. Check what's new ──────────────────────────────────────────
step "Fetching from origin"
git fetch --quiet origin

LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse @{u} 2>/dev/null || echo "")
if [ -z "$REMOTE" ]; then
    err "No upstream branch tracked — set one with: git branch --set-upstream-to=origin/master"
    exit 1
fi

if [ "$LOCAL" = "$REMOTE" ]; then
    ok "Already up to date at $(git log -1 --format='%h %s' HEAD)"
    AHEAD=0
else
    AHEAD=$(git rev-list --count "$LOCAL".."$REMOTE")
    ok "$AHEAD commit(s) behind origin"
    echo
    git log --oneline "$LOCAL".."$REMOTE" | sed 's/^/    /'
fi

if [ "$CHECK_ONLY" = "1" ]; then
    exit 0
fi

# ── 3. Pull (only if there's something to pull) ──────────────────
if [ "$AHEAD" -gt 0 ]; then
    if ! git diff --quiet || ! git diff --cached --quiet; then
        warn "Working tree has uncommitted changes — git pull --rebase may conflict."
        warn "Stash or commit first if you want a clean update."
        exit 1
    fi
    step "Pulling $AHEAD commit(s)"
    git pull --rebase --quiet origin "$(git rev-parse --abbrev-ref HEAD)"
    ok "Now at $(git log -1 --format='%h %s' HEAD)"
fi

# ── 3.5. Audit existing systemd unit for stale env vars ─────────
# When DETM rolled from supervised to bash + gpt-5.4 (May 2026), some installs
# kept stale ACU_LIVE_UI_BACKEND / ACU_HOLO3_* / ACU_OPENROUTER_GUI_DIRECT_MODEL
# values in their unit. Surface them so the user knows what's in effect — the
# code default would be bash + openai/gpt-5.4 if these were absent.
UNIT_FILE="/etc/systemd/system/detm-daemon.service"
if [ -f "$UNIT_FILE" ]; then
    UNIT_ENV=$(sudo -n grep -E '^Environment=ACU_(LIVE_UI_BACKEND|OPENROUTER_GUI_DIRECT_MODEL|HOLO3_|GUI_AGENT_BACKEND)' "$UNIT_FILE" 2>/dev/null || true)
    if [ -n "$UNIT_ENV" ]; then
        step "Existing systemd unit overrides for gui_agent backend:"
        echo "$UNIT_ENV" | sed 's/^/    /'
        # Warn on known-stale values
        if echo "$UNIT_ENV" | grep -qE 'ACU_LIVE_UI_BACKEND=supervised'; then
            warn "ACU_LIVE_UI_BACKEND=supervised is set — production default is bash. Re-run install.sh to reset, or edit $UNIT_FILE."
        fi
        if echo "$UNIT_ENV" | grep -qE '^Environment=ACU_HOLO3_'; then
            warn "ACU_HOLO3_* found — Holo3 backend is no longer in master (lives on feat/multi-backend-gui-agent). Safe to remove."
        fi
    else
        ok "No backend overrides in systemd unit (will use code defaults: bash + openai/gpt-5.4)"
    fi
fi

# ── 4. Reinstall package (incremental) ───────────────────────────
step "Updating Python package (incremental)"
PIP_OUT=$("$VENV_DIR/bin/pip" install --disable-pip-version-check -e . 2>&1)
if echo "$PIP_OUT" | grep -qE "Installing collected packages|Successfully installed"; then
    echo "$PIP_OUT" | grep -E "Installing collected packages|Successfully installed" | sed 's/^/    /'
    ok "Dependencies updated"
else
    ok "No new dependencies"
fi

# ── 5. Restart daemon ────────────────────────────────────────────
if [ "$NO_RESTART" = "1" ]; then
    warn "Skipping daemon restart (--no-restart). Daemon is still running old code."
    exit 0
fi

step "Restarting detm-daemon"
if sudo -n systemctl restart detm-daemon 2>/dev/null; then
    ok "Restarted"
else
    warn "sudo not available non-interactively — prompting"
    if sudo systemctl restart detm-daemon; then
        ok "Restarted"
    else
        err "Failed to restart daemon. Try: sudo systemctl restart detm-daemon"
        exit 1
    fi
fi

# ── 6. Verify ────────────────────────────────────────────────────
step "Verifying daemon health"
for i in 1 2 3 4 5; do
    if curl -fs "http://127.0.0.1:$DAEMON_PORT/health" >/dev/null 2>&1; then
        ok "Daemon is responding"
        break
    fi
    [ "$i" = "5" ] && { err "Daemon not responding after 5s"; exit 1; }
    sleep 1
done

if [ -x "$REPO_DIR/bin/detm-doctor" ]; then
    "$REPO_DIR/bin/detm-doctor" --quiet
    DOC_RC=$?
    if [ "$DOC_RC" = "0" ]; then
        ok "All systems green"
    elif [ "$DOC_RC" = "1" ]; then
        warn "Doctor reports warnings (see above)"
    else
        err "Doctor reports failures (see above) — investigate before relying on the upgrade"
        exit "$DOC_RC"
    fi
fi

echo
echo -e "${GREEN}DETM update complete.${NC}"
