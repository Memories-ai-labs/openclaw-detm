#!/usr/bin/env bash
# dev.sh — run DETM daemon in a tmux session with live debug logs
#
# Usage:
#   ./dev.sh          — start (or reattach to) the dev session
#   ./dev.sh stop     — kill the session
#   ./dev.sh logs     — tail the log file in the current terminal
#   ./dev.sh status   — show daemon HTTP health
#
# Logs are visible to both you and Claude Code:
#   • You   → the tmux session shows colored live logs
#   • Claude→ reads ~/.agentic-computer-use/logs/debug.log

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV="$REPO_DIR/.venv/bin/python3"
SESSION="detm-dev"
DAEMON_PORT=18790
LOG_FILE="${ACU_DATA_DIR:-$HOME/.agentic-computer-use}/logs/debug.log"

# ── helpers ──────────────────────────────────────────────────────────────────

color() { printf '\033[%sm%s\033[0m\n' "$1" "$2"; }
info()  { color "36" "  $*"; }
ok()    { color "32" "✓ $*"; }
err()   { color "31" "✗ $*"; exit 1; }

session_running() { tmux has-session -t "$SESSION" 2>/dev/null; }

build_run_cmd() {
    local env_parts=(
        "ACU_DEBUG=1"
        "DISPLAY=${DISPLAY:-:99}"
        "PYTHONPATH=$REPO_DIR/src"
    )
    local passthrough_vars=(
        ACU_DATA_DIR
        ACU_OPENROUTER_API_KEY
        ACU_OPENROUTER_LIVE_MODEL
        ACU_OPENROUTER_VISION_MODEL
        ACU_VISION_BACKEND
        ACU_GUI_AGENT_BACKEND
        ACU_GEMINI_BACKEND
        ACU_GEMINI_API_KEY
        ACU_MERGE_REASONING
        ACU_DISABLED_TOOLS
        OPENROUTER_API_KEY
        ANTHROPIC_API_KEY
        GEMINI_API_KEY
        MAVI_API_KEY
        OLLAMA_URL
        OPENCLAW_GATEWAY_PORT
    )

    # Load ~/.agentic-computer-use/.env if present (so GEMINI_API_KEY etc. are visible)
    local env_file="${ACU_DATA_DIR:-$HOME/.agentic-computer-use}/.env"
    if [ -f "$env_file" ]; then
        set -a; source "$env_file"; set +a
    fi
    local var=""
    for var in "${passthrough_vars[@]}"; do
        if [ "${!var+x}" = x ]; then
            env_parts+=("$var=${!var}")
        fi
    done

    printf 'env '
    printf '%q ' "${env_parts[@]}"
    printf '%q ' "$VENV" -m agentic_computer_use.daemon
}

# ── subcommands ───────────────────────────────────────────────────────────────

cmd_stop() {
    if session_running; then
        tmux kill-session -t "$SESSION"
        ok "Session '$SESSION' stopped."
    else
        info "Session '$SESSION' is not running."
    fi
}

cmd_logs() {
    if [ ! -f "$LOG_FILE" ]; then
        info "Log file not yet created: $LOG_FILE"
        info "Start the daemon first: ./dev.sh"
        exit 0
    fi
    info "Tailing $LOG_FILE  (Ctrl-C to stop)"
    tail -f "$LOG_FILE"
}

cmd_status() {
    echo ""
    info "DETM daemon health check (port $DAEMON_PORT):"
    curl -s "http://127.0.0.1:$DAEMON_PORT/health" 2>/dev/null | python3 -m json.tool || \
        color "31" "  Daemon not responding on port $DAEMON_PORT"
    echo ""
    if session_running; then
        ok "tmux session '$SESSION' is running"
        info "Attach: tmux attach -t $SESSION"
    else
        color "33" "  tmux session '$SESSION' is not running"
    fi
}

ensure_virtual_desktop() {
    # Virtual desktop services required for :99 display
    local services=("xvfb-99" "xfce4-99" "xfwm4-99" "x11vnc-99" "novnc-99")
    local started=0

    for svc in "${services[@]}"; do
        if ! systemctl is-active --quiet "$svc" 2>/dev/null; then
            info "Starting $svc..."
            sudo systemctl start "$svc" 2>/dev/null || true
            started=1
        fi
    done

    # User-level services
    if ! systemctl --user is-active --quiet openclaw-gateway 2>/dev/null; then
        info "Starting openclaw-gateway..."
        systemctl --user start openclaw-gateway 2>/dev/null || true
        started=1
    fi

    if [ "$started" -eq 1 ]; then
        sleep 3  # give XFCE time to initialize
    fi

    if systemctl is-active --quiet xvfb-99 2>/dev/null; then
        ok "Virtual desktop :99 is running  (VNC → port 5999)"
    else
        color "33" "  Warning: Xvfb :99 not running — live screen may be unavailable"
    fi
}

# shellcheck source=scripts/deploy-agents.sh
source "$REPO_DIR/scripts/deploy-agents.sh"

cmd_start() {
    ensure_virtual_desktop
    deploy_agents

    if session_running; then
        info "Killing existing session '$SESSION'..."
        tmux kill-session -t "$SESSION"
        sleep 0.5
    fi

    if lsof -ti tcp:$DAEMON_PORT &>/dev/null 2>&1; then
        info "Killing stale process on port $DAEMON_PORT..."
        kill "$(lsof -ti tcp:$DAEMON_PORT)" 2>/dev/null || true
        sleep 0.5
    fi

    mkdir -p "$(dirname "$LOG_FILE")"

    RUN_CMD="$(build_run_cmd)"

    tmux new-session -d -s "$SESSION" -x 220 -y 50 \; \
        send-keys "$RUN_CMD" Enter

    sleep 1

    if session_running; then
        echo ""
        ok "DETM daemon started in tmux session '$SESSION'"
        echo ""
        info "Attach to watch live:    tmux attach -t $SESSION"
        info "Detach (stay running):   Ctrl-B  D"
        info "Tail log file:           ./dev.sh logs"
        info "Health check:            ./dev.sh status"
        info "Stop:                    ./dev.sh stop"
        echo ""
        info "Log file (for Claude):   $LOG_FILE"
        info "VNC desktop (:99):       <server-ip>:5999  (native VNC)"
        info "noVNC browser view:      http://<server-ip>:6080/vnc.html"
        echo ""
    else
        err "Failed to start tmux session. Check: tmux ls"
    fi
}

# ── dispatch ──────────────────────────────────────────────────────────────────

case "${1:-start}" in
    start)  cmd_start  ;;
    stop)   cmd_stop   ;;
    logs)   cmd_logs   ;;
    status) cmd_status ;;
    *)
        echo "Usage: $0 {start|stop|logs|status}"
        exit 1
        ;;
esac
