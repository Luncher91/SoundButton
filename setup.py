"""Build script for creating a macOS .app bundle using py2app.

Run:
  ./build_macos_app.sh

This will create a macOS application bundle at:
  dist/SerialButtonSound.app

If you need to include additional resources (e.g. WAV files), add them to the project
root and they will be bundled automatically.
"""

from __future__ import annotations

import glob

from setuptools import setup

APP = ["serial_btn_sound.py"]
DATA_FILES = glob.glob("*.wav")

OPTIONS = {
    "argv_emulation": True,
    # Use a custom icon (icon.icns) for the app bundle.
    "iconfile": "icon.icns",
    # Make sure pyserial + Qt are included; py2app can sometimes miss them without explicit packages.
    "packages": ["serial", "PySide6"],
    "resources": DATA_FILES,
}

setup(
    app=APP,
    name="SerialButtonSound",
    options={"py2app": OPTIONS},
)
