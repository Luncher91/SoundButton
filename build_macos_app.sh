#!/usr/bin/env bash
set -euo pipefail

# Build a self-contained macOS .app bundle using py2app.
# The resulting app bundle will appear in dist/SerialButtonSound.app

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR"

# Create (or reuse) a venv so builds are reproducible.
if [ ! -d ".venv" ]; then
  python -m venv .venv
fi

# shellcheck source=/dev/null
source .venv/bin/activate

pip install --upgrade pip setuptools wheel
pip install -r requirements.txt

python setup.py py2app

echo "\n✅ Built macOS app at: $PWD/dist/SerialButtonSound.app"
