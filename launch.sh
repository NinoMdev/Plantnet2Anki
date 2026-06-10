#!/usr/bin/env bash
# PlantNet2Anki - Linux/macOS launcher

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

for cmd in python3 python; do
    if command -v "$cmd" &>/dev/null; then
        "$cmd" "$SCRIPT_DIR/plantnet2anki_gui.py"
        exit $?
    fi
done

echo "[ERROR] Python not found. Run install.sh first."
exit 1
