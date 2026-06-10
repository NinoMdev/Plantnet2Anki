#!/usr/bin/env bash
set -e

echo "========================================"
echo "  PlantNet2Anki - Installation (Unix)"
echo "========================================"
echo

# ── Find Python 3.8+ ─────────────────────────────────────────────────────────
PYTHON=""
for cmd in python3 python python3.12 python3.11 python3.10 python3.9 python3.8; do
    if command -v "$cmd" &>/dev/null; then
        version=$("$cmd" -c "import sys; print(sys.version_info.major * 10 + sys.version_info.minor)" 2>/dev/null || echo 0)
        if [ "$version" -ge 38 ]; then
            PYTHON="$cmd"
            break
        fi
    fi
done

if [ -z "$PYTHON" ]; then
    echo "[ERROR] Python 3.8 or higher not found."
    echo
    echo "Install it with:"
    echo "  macOS:  brew install python3"
    echo "  Ubuntu/Debian: sudo apt install python3 python3-pip"
    echo "  Fedora: sudo dnf install python3 python3-pip"
    exit 1
fi

PYVER=$("$PYTHON" --version 2>&1)
echo "[OK] Found $PYVER (using: $PYTHON)"
echo

# ── Upgrade pip ───────────────────────────────────────────────────────────────
echo "Upgrading pip..."
"$PYTHON" -m pip install --upgrade pip --quiet || echo "[WARNING] Could not upgrade pip, continuing..."
echo

# ── Install dependencies ──────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REQ_FILE="$SCRIPT_DIR/requirements.txt"

if [ ! -f "$REQ_FILE" ]; then
    echo "[ERROR] requirements.txt not found in $SCRIPT_DIR"
    exit 1
fi

echo "Installing dependencies from requirements.txt..."
"$PYTHON" -m pip install -r "$REQ_FILE"

echo
echo "========================================"
echo "  Installation complete!"
echo "========================================"
echo
echo "To launch the app, run:"
echo "  $PYTHON plantnet2anki_gui.py"
echo
echo "Or make the launcher executable and run it:"
echo "  chmod +x launch.sh && ./launch.sh"
echo
