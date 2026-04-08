#!/bin/bash
# OpenClaw Trader — dependency installer
# Run from the project root: bash scripts/install.sh
# Installs all Python packages required by ridley scripts and the dashboard.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
REQUIREMENTS="$PROJECT_ROOT/requirements.txt"

if [[ ! -f "$REQUIREMENTS" ]]; then
    echo "ERROR: requirements.txt not found at $REQUIREMENTS"
    exit 1
fi

echo "Installing OpenClaw Trader dependencies from $REQUIREMENTS ..."
pip3 install -r "$REQUIREMENTS"
echo "Done."
