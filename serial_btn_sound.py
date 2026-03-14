"""Serial listener that plays a sound when specific messages arrive.

This script supports:
- Mapping received messages (e.g. BTN1, BTN2) to either a wav sound file or a simple terminal bell via `tput bel`.
- A default mapping for BTN1 -> tput bell.

Usage:
  python serial_btn_sound.py --port /dev/ttyUSB0 --baud 9600

Requirements:
  pip install -r requirements.txt
  (includes pyserial, py2app, PyQt6)

If you want to play a WAV file, put it next to this script and update the mapping.
"""

from __future__ import annotations

import argparse
import json
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable, Optional

try:
    from PyQt6 import QtCore, QtGui, QtWidgets
except ImportError as exc:
    QtWidgets = None
    _pyqt_import_error = exc

import serial


DEFAULT_PORT = "/dev/cu.usbserial-0001"  # change for your system (e.g. COM3 on Windows)
DEFAULT_BAUD = 115200

# Config persistence
CONFIG_PATH = Path.home() / ".soundbutton_config.json"

# Map incoming serial messages to actions.
# The value can be:
# - A string ending with ".wav" -> play that WAV file.
# - The special token "tput" -> run `tput bel` (terminal bell).
# - A callable (message) -> custom behaviour (advanced).
TRIGGER_MAP = {
    "BTN1": "air_horn.wav",
    # "BTN2": "click.wav",
    # "BTN3": "alert.wav",
}

# Debounce configuration: ignore repeated triggers within this interval (seconds)
DEBOUNCE_SECONDS = 3.0
# Track the last time each message was handled.
_last_triggered_at: dict[str, float] = {}

# How long to keep the heartbeat indicator green after the last HEART_BEAT.
HEARTBEAT_TIMEOUT = 2.0
# Track last heartbeat time (for GUI indicator updates).
_last_heartbeat_at: float | None = None

# Used to request a clean shutdown when the OS sends a termination signal.
_stop_event = threading.Event()

# Track the active thread running `read_loop` (used by the web UI status endpoint).
_serial_thread: Optional[threading.Thread] = None

# Optional callback to notify a GUI about received heartbeats.
_heartbeat_callback: "Optional[Callable[[], None]]" = None

def _safe_print(*args, **kwargs):
    """Print safely even if stdout/stderr is closed (e.g., in a macOS .app exit path)."""
    try:
        print(*args, **kwargs)
    except Exception:
        pass


def load_config() -> dict:
    """Load stored settings from disk.

    Stored keys:
    - port: serial port
    - baud: baud rate
    - trigger_map: dict mapping messages to actions

    Returns an empty dict on failure.
    """
    try:
        if CONFIG_PATH.exists():
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def save_config(
    port: str,
    baud: int,
    trigger_map: dict | None = None,
    debounce_seconds: float | None = None,
    heartbeat_timeout: float | None = None,
) -> None:
    """Persist the current settings to disk."""
    try:
        cfg: dict = {"port": port, "baud": baud}
        if trigger_map is not None:
            cfg["trigger_map"] = trigger_map
        if debounce_seconds is not None:
            cfg["debounce_seconds"] = debounce_seconds
        if heartbeat_timeout is not None:
            cfg["heartbeat_timeout"] = heartbeat_timeout
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with CONFIG_PATH.open("w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception as exc:
        _safe_print(f"⚠️  Failed to save config: {exc}")


def play_wav(path: str) -> None:
    """Play a WAV file using macOS afplay."""
    try:
        # afplay is non-blocking when launched via Popen.
        subprocess.Popen(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        _safe_print(f"⚠️  Failed to play WAV '{path}': {exc}")


def play_tput_bell() -> None:
    """Play a bell sound via terminal (tput bel) or fallback to ASCII BEL."""
    try:
        # Use a non-blocking subprocess so the serial loop isn't stalled.
        subprocess.Popen(["say", "-v", "Bells", "dong dong dong"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        # Fall back to ASCII BEL if the platform does not support `say`.
        try:
            print("\a", end="", flush=True)
        except Exception:
            pass
        _safe_print(f"⚠️  bell failed: {exc}")


def _run_async(func, *args, **kwargs):
    """Run a function in a daemon thread so the serial loop never blocks."""

    def _wrapper():
        try:
            func(*args, **kwargs)
        except Exception as exc:
            _safe_print(f"⚠️  Async handler error: {exc}")

    thread = threading.Thread(target=_wrapper, daemon=True)
    thread.start()


def _request_shutdown(signum, frame):
    """Signal handler that requests a clean shutdown."""

    # Avoid printing on every signal if the app is already stopping.
    if not _stop_event.is_set():
        _safe_print(f"\nReceived signal {signum}, stopping...")
    _stop_event.set()


def setup_signal_handlers() -> None:
    """Install handlers so the app can exit cleanly on macOS (Cmd+Q / Force Quit)."""

    signal.signal(signal.SIGINT, _request_shutdown)
    signal.signal(signal.SIGTERM, _request_shutdown)


def handle_message(msg: str) -> None:
    """Handle an incoming serial message by triggering the configured action."""
    global _last_heartbeat_at

    # HEART_BEAT messages are used to show the connection is alive.
    if msg == "HEART_BEAT":
        _last_heartbeat_at = time.monotonic()
        if _heartbeat_callback:
            try:
                _heartbeat_callback()
            except Exception:
                pass
        return

    # Debounce repeated messages: ignore events within DEBOUNCE_SECONDS.
    now = time.monotonic()
    last = _last_triggered_at.get(msg)
    if last is not None and (now - last) < DEBOUNCE_SECONDS:
        return
    _last_triggered_at[msg] = now

    action = TRIGGER_MAP.get(msg)
    if action is None:
        return

    if isinstance(action, str) and action.lower().endswith(".wav"):
        _run_async(play_wav, action)
    elif action == "tput":
        _run_async(play_tput_bell)
    elif callable(action):
        _run_async(action, msg)
    else:
        _safe_print(f"⚠️  Unsupported action for '{msg}': {action}")


def read_loop(port: str, baud: int) -> None:
    global _serial_thread
    try:
        while not _stop_event.is_set():
            try:
                with serial.Serial(port, baud, timeout=1) as ser:
                    _safe_print(f"Listening on {port} at {baud} baud...")
                    while not _stop_event.is_set():
                        line = ser.readline().decode("utf-8", errors="ignore").strip()
                        if not line:
                            continue

                        _safe_print(f"RX: {line}")
                        handle_message(line)
            except Exception as exc:
                # If we're shutting down, ignore errors and exit cleanly.
                if _stop_event.is_set():
                    break
                # Keep listening even if a transient error occurs.
                _safe_print(f"Error reading serial: {exc}, retrying in 1s...")
                time.sleep(1)

    except KeyboardInterrupt:
        # Clean exit on Ctrl+C (macOS can be noisy if this bubbles up).
        _safe_print("\nStopped by user.")
    except serial.SerialException as exc:
        _safe_print(f"⚠️  Unable to open serial port {port}: {exc}")
        sys.exit(1)
    finally:
        _serial_thread = None


class SerialGuiApp(QtWidgets.QWidget):
    """A simple PyQt GUI for monitoring HEART_BEAT and editing the trigger map."""

    def __init__(self) -> None:
        if QtWidgets is None:
            raise RuntimeError("PyQt6 is not available; GUI cannot be started.")

        super().__init__()
        self.setWindowTitle("Serial Button Sound")
        self.resize(480, 480)

        self.serial_thread: threading.Thread | None = None
        self.last_heartbeat: float | None = None

        self._heartbeat_timer = QtCore.QTimer(self)
        self._heartbeat_timer.setInterval(300)
        self._heartbeat_timer.timeout.connect(self._update_heartbeat_indicator)
        self._heartbeat_timer.start()

        self._build_ui()

    def _build_ui(self) -> None:
        port_label = QtWidgets.QLabel("Serial port:")
        self.port_edit = QtWidgets.QLineEdit(DEFAULT_PORT)
        baud_label = QtWidgets.QLabel("Baud:")
        self.baud_edit = QtWidgets.QLineEdit(str(DEFAULT_BAUD))

        self.heartbeat_label = QtWidgets.QLabel("HEART_BEAT")
        self.heartbeat_label.setAutoFillBackground(True)
        self._set_heartbeat_color("red")
        self.heartbeat_label.setAlignment(QtCore.Qt.AlignmentFlag.AlignCenter)
        self.heartbeat_label.setMinimumHeight(26)

        trigger_label = QtWidgets.QLabel("Trigger map (JSON):")
        self.trigger_edit = QtWidgets.QTextEdit()
        self.trigger_edit.setPlainText(json.dumps(TRIGGER_MAP, indent=2))

        self.apply_btn = QtWidgets.QPushButton("Apply config")
        self.start_btn = QtWidgets.QPushButton("Start")
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.stop_btn.setEnabled(False)

        self.status_label = QtWidgets.QLabel("Stopped")

        self.apply_btn.clicked.connect(self.apply_config)
        self.start_btn.clicked.connect(self.start_serial)
        self.stop_btn.clicked.connect(self.stop_serial)

        top_layout = QtWidgets.QHBoxLayout()
        top_layout.addWidget(port_label)
        top_layout.addWidget(self.port_edit, 1)
        top_layout.addSpacing(10)
        top_layout.addWidget(baud_label)
        top_layout.addWidget(self.baud_edit, 0)

        button_layout = QtWidgets.QHBoxLayout()
        button_layout.addWidget(self.apply_btn)
        button_layout.addWidget(self.start_btn)
        button_layout.addWidget(self.stop_btn)
        button_layout.addStretch()

        main_layout = QtWidgets.QVBoxLayout(self)
        main_layout.addLayout(top_layout)
        main_layout.addWidget(self.heartbeat_label)
        main_layout.addWidget(trigger_label)
        main_layout.addWidget(self.trigger_edit, 1)
        main_layout.addLayout(button_layout)
        main_layout.addWidget(self.status_label)

    def _set_heartbeat_color(self, color: str) -> None:
        palette = self.heartbeat_label.palette()
        palette.setColor(QtGui.QPalette.ColorRole.Window, QtGui.QColor(color))
        self.heartbeat_label.setPalette(palette)

    def _update_heartbeat_indicator(self) -> None:
        now = time.monotonic()
        if self.last_heartbeat is not None and (now - self.last_heartbeat) < HEARTBEAT_TIMEOUT:
            self._set_heartbeat_color("green")
            self.heartbeat_label.setText("HEART_BEAT received")
        else:
            self._set_heartbeat_color("red")
            if self.last_heartbeat is None:
                self.heartbeat_label.setText("No HEART_BEAT received yet")
            else:
                self.heartbeat_label.setText("HEART_BEAT timed out")

    def on_heartbeat(self) -> None:
        self.last_heartbeat = time.monotonic()

    def apply_config(self) -> None:
        try:
            data = json.loads(self.trigger_edit.toPlainText())
            if not isinstance(data, dict):
                raise ValueError("Trigger map must be a JSON object")
            # Update global trigger map in-place for thread safety.
            TRIGGER_MAP.clear()
            TRIGGER_MAP.update({str(k): v for k, v in data.items()})
            self.status_label.setText("Config applied.")

            # Persist current settings including the trigger map.
            save_config(self.port_edit.text().strip(), int(self.baud_edit.text().strip()), TRIGGER_MAP)
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Invalid Trigger Map", f"Failed to parse trigger map: {exc}")

    def start_serial(self) -> None:
        if self.serial_thread and self.serial_thread.is_alive():
            return

        try:
            port = self.port_edit.text().strip()
            baud = int(self.baud_edit.text().strip())
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Invalid serial settings", str(exc))
            return

        self.apply_config()

        # Persist the chosen port/baud, trigger map, and timing settings.
        save_config(
            port,
            baud,
            TRIGGER_MAP,
            debounce_seconds=DEBOUNCE_SECONDS,
            heartbeat_timeout=HEARTBEAT_TIMEOUT,
        )

        _stop_event.clear()
        global _heartbeat_callback
        _heartbeat_callback = self.on_heartbeat

        self.serial_thread = threading.Thread(target=read_loop, args=(port, baud), daemon=True)
        self.serial_thread.start()

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.apply_btn.setEnabled(False)
        self.status_label.setText(f"Listening on {port} @ {baud}...")

        self.heartbeat_label.setText("Connecting...")

    def stop_serial(self) -> None:
        global _heartbeat_callback
        _stop_event.set()
        _heartbeat_callback = None
        self.last_heartbeat = None
        if self.serial_thread:
            self.serial_thread.join(timeout=1)
        self.status_label.setText("Stopped")
        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.apply_btn.setEnabled(True)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:
        self.stop_serial()
        event.accept()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serial -> sound trigger")
    # Use None defaults so we can detect whether the user explicitly provided a value.
    parser.add_argument("--port", default=None, help="Serial port (e.g. /dev/ttyUSB0 or COM3)")
    parser.add_argument("--baud", type=int, default=None, help="Baud rate")
    parser.add_argument("--nogui", action="store_true", help="Run in CLI mode instead of GUI")
    return parser.parse_args()


def main() -> None:
    global DEBOUNCE_SECONDS, HEARTBEAT_TIMEOUT

    args = parse_args()
    setup_signal_handlers()

    if args.nogui:
        # In CLI mode we still want config to be applied (port/baud + trigger map + timings).
        cfg = load_config()
        trigger_map = cfg.get("trigger_map")
        if isinstance(trigger_map, dict):
            TRIGGER_MAP.clear()
            TRIGGER_MAP.update({str(k): v for k, v in trigger_map.items()})

        # Determine port/baud (cli > config > default).
        if args.port is not None:
            used_port = args.port
            port_source = "cli"
        elif "port" in cfg and cfg.get("port"):
            used_port = str(cfg["port"])
            port_source = "config"
        else:
            used_port = DEFAULT_PORT
            port_source = "default"

        if args.baud is not None:
            used_baud = args.baud
            baud_source = "cli"
        elif "baud" in cfg and cfg.get("baud") is not None:
            used_baud = int(cfg["baud"])
            baud_source = "config"
        else:
            used_baud = DEFAULT_BAUD
            baud_source = "default"

        # Load optional debounce/heartbeat settings from config.
        if "debounce_seconds" in cfg:
            try:
                DEBOUNCE_SECONDS = float(cfg["debounce_seconds"])
            except Exception:
                pass
        if "heartbeat_timeout" in cfg:
            try:
                HEARTBEAT_TIMEOUT = float(cfg["heartbeat_timeout"])
            except Exception:
                pass

        _safe_print(
            f"Using port={used_port} ({port_source}), "
            f"baud={used_baud} ({baud_source})"
        )

        try:
            read_loop(used_port, used_baud)
        except Exception as exc:
            _safe_print(f"⚠️  Unhandled exception: {exc}")
            sys.exit(1)
        return

    if QtWidgets is None:
        _safe_print("\n⚠️  PyQt6 is not available in this Python interpreter.")
        _safe_print("Please install it via 'pip install PyQt6' and rerun this script.")
        sys.exit(1)

    qt_app = QtWidgets.QApplication(sys.argv)
    window = SerialGuiApp()

    # Load persisted port/baud, falling back to args if nothing saved.
    cfg = load_config()

    # Determine what values to use (cli > config > default).
    if args.port is not None:
        used_port = args.port
        port_source = "cli"
    elif "port" in cfg and cfg.get("port"):
        used_port = str(cfg["port"])
        port_source = "config"
    else:
        used_port = DEFAULT_PORT
        port_source = "default"
    window.port_edit.setText(used_port)

    if args.baud is not None:
        used_baud = args.baud
        baud_source = "cli"
    elif "baud" in cfg and cfg.get("baud") is not None:
        used_baud = int(cfg["baud"])
        baud_source = "config"
    else:
        used_baud = DEFAULT_BAUD
        baud_source = "default"
    window.baud_edit.setText(str(used_baud))

    # Load debounce and heartbeat settings from config if present.
    if "debounce_seconds" in cfg:
        try:
            DEBOUNCE_SECONDS = float(cfg["debounce_seconds"])
        except Exception:
            pass
    if "heartbeat_timeout" in cfg:
        try:
            HEARTBEAT_TIMEOUT = float(cfg["heartbeat_timeout"])
        except Exception:
            pass

    _safe_print(
        f"Using port={used_port} ({port_source}), "
        f"baud={used_baud} ({baud_source})"
    )

    # Open the serial port immediately on start.
    window.show()
    window.start_serial()

    qt_app.exec()


if __name__ == "__main__":
    main()
