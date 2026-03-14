# SoundButton

A small macOS utility that listens to a serial port and plays a sound (or triggers an action) when it receives specific messages (e.g., `BTN1`).

It includes a GUI for:
- selecting a serial port and baud rate
- viewing heartbeat status from the device
- editing the mapping from serial messages to actions (e.g., play a WAV file)

The macOS `.app` bundle is built using `py2app`.

---

## Features

- **Serial-to-sound mapping** using a configurable JSON trigger map
- **Debouncing** (prevents repeated triggers within 3 seconds)
- **Heartbeat indicator** (shows whether the device is still sending `HEART_BEAT`)
- **Clean shutdown** (handles SIGINT/SIGTERM and closes the serial port cleanly)
- **macOS `.app` bundle** produced via `py2app`

---

## Requirements

- macOS (Intel or Apple Silicon)
- Python 3.13 (or compatible)
- `pyserial`
- `py2app`
- `PyQt6`

---

## Setup (development)

```bash
cd /path/to/SoundButton
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
python -m pip install -r requirements.txt
```

---

## Running

### GUI mode

```bash
python serial_btn_sound.py
```

### CLI (no GUI)

```bash
python serial_btn_sound.py --nogui --port /dev/ttyXXX --baud 115200
```

---

## Building the macOS App

```bash
./build_macos_app.sh
```

This produces a bundle at: `dist/SerialButtonSound.app`

---

## Configuring triggers

The GUI includes an editable JSON trigger map.
By default it looks like:

```json
{
  "BTN1": "air_horn.wav"
}
```

Actions can be:
- A `.wav` file name (loaded from the app folder)
- The special token `tput` (plays a simple bell sound via `say`)

---

## Notes

- If the GUI fails to start because `PyQt6` is not installed, install it via:

```bash
python -m pip install PyQt6
```

- The app opens the serial port automatically on startup.

---

## License

MIT
