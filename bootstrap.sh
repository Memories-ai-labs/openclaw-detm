#!/usr/bin/env bash
set -euo pipefail

# ─── DETM one-line installer ────────────────────────────────────
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/Memories-ai-labs/openclaw-detm/master/bootstrap.sh | bash
#   curl -fsSL <url>/bootstrap.sh | OPENROUTER_API_KEY=sk-or-... bash
#
# Requires: Linux, Python 3.11+, sudo access

INSTALL_DIR="${DETM_DIR:-$HOME/.detm}"
REPO_URL="https://github.com/Memories-ai-labs/openclaw-detm.git"

if [ "$(uname)" != "Linux" ]; then
    echo "[detm] ERROR: DETM requires Linux."
    exit 1
fi

if [ -d "$INSTALL_DIR/.git" ]; then
    echo "[detm] Updating existing install at $INSTALL_DIR..."
    cd "$INSTALL_DIR" && git pull --quiet
else
    echo "[detm] Installing to $INSTALL_DIR..."
    git clone --depth 1 "$REPO_URL" "$INSTALL_DIR"
fi

exec "$INSTALL_DIR/install.sh" "$@"
