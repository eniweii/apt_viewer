#!/bin/bash
# Prepare a fresh Claude sandbox for the tools in this folder (about a minute). The sandbox is reset between conversations: run this first.
# Usage: bash setup_sandbox.sh [uploads folder = /mnt/user-data/uploads] [data folder = /home/claude/data]
# It installs the Python packages and the Tk/virtual display pieces, unpacks every .zip of the uploads, and prints the three variables the tools read.
UP=${1:-/mnt/user-data/uploads}; DATA=${2:-/home/claude/data}
pip install --break-system-packages -q pillow numpy skia-python glfw pefile capstone 2>&1 | tail -1
(apt-get update -q && apt-get install -y -q python3-tk xvfb) > /dev/null 2>&1 || echo "apt step failed: --window and gui_drive.py need python3-tk and xvfb"
mkdir -p "$DATA"
for z in "$UP"/*.zip; do [ -f "$z" ] && unzip -oq "$z" -d "$DATA"; done
ui=$(dirname "$(find "$DATA" -name root.big | head -1)" 2>/dev/null)
eng=$(dirname "$(find "$DATA" -name nfs.exe | head -1)" 2>/dev/null)
res=$(find "$DATA" \( -name '*.bun' -o -name '*.lzc' -o -name '*.bin' \) -not -path "$eng/*" -printf '%h\n' | sort -u | paste -sd:)
echo "export APT_UI=$ui"
echo "export APT_ENGINE_DATA=$eng"
echo "export APT_RES=$res"
python3 -c "import skia, PIL, numpy; print('skia', skia.__version__, '| Pillow', PIL.__version__)"
python3 -c "import tkinter; print('tkinter ok')" 2>&1 | tail -1
which xvfb-run > /dev/null && echo "xvfb-run ok"
