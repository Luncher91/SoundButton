#!/usr/bin/env bash
set -euo pipefail

# Build a self-contained macOS .app bundle using py2app.
# The resulting app bundle will appear in dist/SerialButtonSound.app

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR"

# Create (or reuse) a venv so builds are reproducible.
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi

# shellcheck source=/dev/null
source .venv/bin/activate

pip install --upgrade pip setuptools wheel
pip install -r requirements.txt

# Build an ICNS from the canonical SVG (icon.svg).
# The SVG is the single source-of-truth; edit icon.svg directly to change the icon.
if [ ! -f "icon.icns" ]; then
  if [ ! -f "icon.svg" ]; then
    echo "error: icon.svg not found. Please create icon.svg in the project root before building."
    exit 1
  fi

  python - <<'PY'
import os
import shutil
import sys

try:
    import cairosvg
except ImportError:
    sys.exit("cairosvg is required to generate icon.icns. Please run: pip install cairosvg")

iconset_dir = "icon.iconset"
if os.path.exists(iconset_dir):
    shutil.rmtree(iconset_dir)
os.makedirs(iconset_dir)

for base in (16, 32, 128, 256, 512):
    png = os.path.join(iconset_dir, f"icon_{base}x{base}.png")
    cairosvg.svg2png(url="icon.svg", write_to=png, output_width=base, output_height=base)

    png2 = os.path.join(iconset_dir, f"icon_{base}x{base}@2x.png")
    cairosvg.svg2png(url="icon.svg", write_to=png2, output_width=base * 2, output_height=base * 2)

if os.system(f"iconutil -c icns {iconset_dir} -o icon.icns") != 0:
    raise SystemExit("Failed to generate icon.icns via iconutil")
PY
fi

pyinstaller --clean --noconfirm serial_btn_sound.spec

echo "\n✅ Built macOS app at: $PWD/dist/SerialButtonSound.app"
