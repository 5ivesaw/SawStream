#!/usr/bin/env bash
set -euo pipefail
VENV="$HOME/.local/share/sawstream/venv"
mkdir -p "$HOME/.local/share/sawstream" "$HOME/.local/bin"
python3 -m venv "$VENV"
"$VENV/bin/pip" install -q --upgrade pip
"$VENV/bin/pip" install -q -e "$(cd "$(dirname "$0")" && pwd)"
ln -sf "$VENV/bin/sawstream" "$HOME/.local/bin/sawstream"
echo "Installed SawStream! Run 'sawstream' to launch."
