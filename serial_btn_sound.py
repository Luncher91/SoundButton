"""Serial listener that plays a sound when specific messages arrive.

This script supports:
- Mapping received messages (e.g. BTN1, BTN2) to either a wav sound file or a simple terminal bell via `tput bel`.
- A default mapping for BTN1 -> tput bell.

Usage:
  python serial_btn_sound.py --port /dev/ttyUSB0 --baud 9600

Requirements:
  pip install -r requirements.txt
  (includes pyserial, py2app, PySide6)

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
    from PySide6 import QtCore, QtGui, QtWidgets
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

BUILTIN_ACTION_CHOICES = [
    ("Air horn", "air_horn.wav"),
    ("Terminal bell", "tput"),
    ("Read text", "say"),
]

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
        subprocess.Popen(["say", "-v", "Bells", "dong dong dong"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        try:
            print("\a", end="", flush=True)
        except Exception:
            pass
        _safe_print(f"⚠️  bell failed: {exc}")


def play_say_text(text: str) -> None:
    """Read text aloud using the macOS `say` command."""
    try:
        subprocess.Popen(["say", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        _safe_print(f"⚠️  Failed to speak text '{text}': {exc}")


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
    elif isinstance(action, str) and action.startswith("say:"):
        _run_async(play_say_text, action[len("say:"):])
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

        trigger_label = QtWidgets.QLabel("Trigger mappings:")
        self.mapping_rows: list[dict[str, QtWidgets.QWidget]] = []
        self.mapping_layout = QtWidgets.QVBoxLayout()
        self.mapping_layout.setSpacing(8)
        self.mapping_layout.setContentsMargins(0, 0, 0, 0)

        mapping_frame = QtWidgets.QFrame()
        mapping_frame.setLayout(self.mapping_layout)
        mapping_frame.setFrameShape(QtWidgets.QFrame.Shape.StyledPanel)

        self.add_mapping_btn = QtWidgets.QPushButton("Add mapping")
        self.add_mapping_btn.clicked.connect(lambda: self.add_mapping_row())

        self.start_btn = QtWidgets.QPushButton("Start")
        self.stop_btn = QtWidgets.QPushButton("Stop")
        self.stop_btn.setEnabled(False)

        self.status_label = QtWidgets.QLabel("Stopped")

        self.port_edit.textChanged.connect(self._on_serial_settings_changed)
        self.baud_edit.textChanged.connect(self._on_serial_settings_changed)
        self.start_btn.clicked.connect(self.start_serial)
        self.stop_btn.clicked.connect(self.stop_serial)

        top_layout = QtWidgets.QHBoxLayout()
        top_layout.addWidget(port_label)
        top_layout.addWidget(self.port_edit, 1)
        top_layout.addSpacing(10)
        top_layout.addWidget(baud_label)
        top_layout.addWidget(self.baud_edit, 0)

        mapping_header_layout = QtWidgets.QHBoxLayout()
        mapping_header_layout.addWidget(trigger_label)
        mapping_header_layout.addStretch()
        mapping_header_layout.addWidget(self.add_mapping_btn)

        button_layout = QtWidgets.QHBoxLayout()
        button_layout.addWidget(self.start_btn)
        button_layout.addWidget(self.stop_btn)
        button_layout.addStretch()

        main_layout = QtWidgets.QVBoxLayout(self)
        main_layout.addLayout(top_layout)
        main_layout.addWidget(self.heartbeat_label)
        main_layout.addLayout(mapping_header_layout)
        main_layout.addWidget(mapping_frame, 1)
        main_layout.addLayout(button_layout)
        main_layout.addWidget(self.status_label)

        self.add_mapping_row()

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

    def _on_action_changed(self, row: dict[str, QtWidgets.QWidget]) -> None:
        row["text_edit"].setEnabled(row["action_combo"].currentData() == "say")

    def _clear_mapping_rows(self) -> None:
        while self.mapping_rows:
            self._remove_mapping_row(self.mapping_rows[0], keep_one=False)

    def _remove_mapping_row(self, row: dict[str, QtWidgets.QWidget], keep_one: bool = True) -> None:
        if row not in self.mapping_rows:
            return
        self.mapping_rows.remove(row)
        for widget_key in ("trigger_edit", "action_combo", "text_edit", "remove_btn"):
            widget = row.get(widget_key)
            if widget is not None:
                widget.hide()
                widget.deleteLater()
        while row["layout"].count():
            item = row["layout"].takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.mapping_layout.removeItem(row["layout"])
        if keep_one and not self.mapping_rows:
            self.add_mapping_row()

    def add_mapping_row(self, trigger: str = "", action: str = "tput", text: str = "") -> None:
        row_layout = QtWidgets.QHBoxLayout()
        row_layout.setSpacing(8)

        trigger_edit = QtWidgets.QLineEdit(trigger)
        trigger_edit.setPlaceholderText("Message (e.g. BTN1)")
        trigger_edit.setMinimumWidth(100)

        action_combo = QtWidgets.QComboBox()
        for label, value in BUILTIN_ACTION_CHOICES:
            action_combo.addItem(label, value)
        index = next((i for i in range(action_combo.count()) if action_combo.itemData(i) == action), 0)
        action_combo.setCurrentIndex(index)

        text_edit = QtWidgets.QLineEdit(text)
        text_edit.setPlaceholderText("Text to read aloud")
        text_edit.setEnabled(action == "say")

        remove_btn = QtWidgets.QPushButton("Remove")
        remove_btn.setFixedWidth(90)

        row_layout.addWidget(trigger_edit, 1)
        row_layout.addWidget(action_combo)
        row_layout.addWidget(text_edit, 2)
        row_layout.addWidget(remove_btn)

        self.mapping_layout.addLayout(row_layout)

        row: dict[str, QtWidgets.QWidget] = {
            "layout": row_layout,
            "trigger_edit": trigger_edit,
            "action_combo": action_combo,
            "text_edit": text_edit,
            "remove_btn": remove_btn,
        }
        self.mapping_rows.append(row)

        trigger_edit.textChanged.connect(lambda _: self._on_mapping_changed())
        action_combo.currentIndexChanged.connect(lambda _: (self._on_action_changed(row), self._on_mapping_changed()))
        text_edit.textChanged.connect(lambda _: self._on_mapping_changed())
        remove_btn.clicked.connect(lambda _: (self._remove_mapping_row(row), self._on_mapping_changed()))

    def load_trigger_map(self, trigger_map: dict | None = None) -> None:
        if trigger_map is None:
            trigger_map = TRIGGER_MAP
        self._clear_mapping_rows()
        for trigger, action in trigger_map.items():
            text = ""
            action_value = action
            if isinstance(action, str) and action.startswith("say:"):
                text = action[len("say:"):]
                action_value = "say"
            self.add_mapping_row(trigger=str(trigger), action=str(action_value), text=text)
        if not self.mapping_rows:
            self.add_mapping_row()

    def _build_trigger_map_from_rows(self) -> dict[str, str]:
        new_map: dict[str, str] = {}
        for row in self.mapping_rows:
            trigger = row["trigger_edit"].text().strip()
            if not trigger:
                continue
            action_value = row["action_combo"].currentData()
            if action_value == "say":
                text = row["text_edit"].text().strip()
                if not text:
                    continue
                new_map[trigger] = f"say:{text}"
            else:
                new_map[trigger] = action_value
        return new_map

    def _save_current_config(self) -> None:
        try:
            port = self.port_edit.text().strip()
            baud = int(self.baud_edit.text().strip())
            save_config(port, baud, TRIGGER_MAP)
        except Exception:
            pass

    def _on_mapping_changed(self) -> None:
        new_map = self._build_trigger_map_from_rows()
        TRIGGER_MAP.clear()
        TRIGGER_MAP.update(new_map)
        self.status_label.setText("Mappings updated.")
        self._save_current_config()

    def _on_serial_settings_changed(self) -> None:
        try:
            int(self.baud_edit.text().strip())
            self.status_label.setText("Serial settings updated.")
            self._save_current_config()
        except ValueError:
            self.status_label.setText("Enter a valid baud rate.")

    def start_serial(self) -> None:
        if self.serial_thread and self.serial_thread.is_alive():
            return

        try:
            port = self.port_edit.text().strip()
            baud = int(self.baud_edit.text().strip())
        except Exception as exc:
            QtWidgets.QMessageBox.critical(self, "Invalid serial settings", str(exc))
            return

        self._on_mapping_changed()

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
        _safe_print("\n⚠️  PySide6 is not available in this Python interpreter.")
        _safe_print("Please install it via 'pip install PySide6' and rerun this script.")
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

    trigger_map = cfg.get("trigger_map")
    if isinstance(trigger_map, dict):
        TRIGGER_MAP.clear()
        TRIGGER_MAP.update({str(k): v for k, v in trigger_map.items()})
    window.load_trigger_map(trigger_map if isinstance(trigger_map, dict) else TRIGGER_MAP)

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
