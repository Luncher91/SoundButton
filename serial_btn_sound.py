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
SOUNDS_DIR = Path.home() / ".soundbutton" / "sounds"
SOUNDS_DIR.mkdir(parents=True, exist_ok=True)

# Map incoming serial messages to actions.
# The value can be:
# - A string (single action) -> play that action
# - A list of strings (multiple actions) -> play each in order
# Supported action types:
# - A string ending with an audio extension (e.g. ".wav", ".mp3") or absolute path -> play that audio file.
# - The special token "tput" -> run `tput bel` (terminal bell).
# - A callable (message) -> custom behaviour (advanced).
TRIGGER_MAP = {
    "BTN1": ["air_horn.wav"],
    # "BTN2": ["click.wav"],
    # "BTN3": ["alert.wav"],
}

BUILTIN_ACTION_CHOICES = [
    ("Air horn", "air_horn.wav"),
    ("Terminal bell", "tput"),
    ("Read text", "say"),
    ("Custom Audio", "custom"),
]

# Debounce configuration: ignore repeated triggers within this interval (seconds)
DEBOUNCE_SECONDS = 1.0
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
    """Play a WAV file using macOS afplay (blocking)."""
    try:
        subprocess.run(["afplay", path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        _safe_print(f"⚠️  Failed to play WAV '{path}': {exc}")


def play_tput_bell() -> None:
    """Play a bell sound via terminal (blocking)."""
    try:
        subprocess.run(["say", "-v", "Bells", "dong dong dong"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        try:
            print("\a", end="", flush=True)
        except Exception:
            pass
        _safe_print(f"⚠️  bell failed: {exc}")


def play_say_text(text: str) -> None:
    """Read text aloud using the macOS `say` command (blocking).
    
    Uses cached audio file if available, otherwise generates and caches it.
    """
    # Create cache directory in user's app data
    cache_dir = Path.home() / ".serial_btn_sound" / "say_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    
    # Create a safe filename from the text
    safe_name = "".join(c if c.isalnum() else "_" for c in text[:50])
    cache_file = cache_dir / f"{safe_name}.wav"
    
    # Generate cached audio if it doesn't exist
    if not cache_file.exists():
        try:
            # Generate AIFF first, then convert to WAV
            aiff_file = cache_file.with_suffix(".aiff")
            subprocess.run(
                ["say", "-o", str(aiff_file), text],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
            )
            # Convert AIFF to WAV
            subprocess.run(
                ["afconvert", "-f", "WAVE", "-d", "LEI16@44100", str(aiff_file), str(cache_file)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True
            )
            # Clean up AIFF
            aiff_file.unlink(missing_ok=True)
        except Exception as exc:
            _safe_print(f"⚠️  Failed to cache speech for '{text}': {exc}")
            # Fall back to direct say
            try:
                subprocess.run(["say", text], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except Exception as e:
                _safe_print(f"⚠️  Failed to speak text '{text}': {e}")
                return
    
    # Play the cached WAV file
    try:
        subprocess.run(["afplay", str(cache_file)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as exc:
        _safe_print(f"⚠️  Failed to play cached speech '{text}': {exc}")


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

    # Support both single action (string) and multiple actions (list)
    actions = [action] if isinstance(action, str) else action
    if not isinstance(actions, list):
        _safe_print(f"⚠️  Invalid action type for '{msg}': {type(action)}")
        return

    # Play each action in sequence (in a background thread so serial loop isn't blocked)
    def _play_actions_sequential(actions: list, msg: str):
        for single_action in actions:
            _handle_single_action_sync(single_action, msg)
    
    _run_async(_play_actions_sequential, actions, msg)


def _handle_single_action_sync(action: str, msg: str) -> None:
    """Handle a single action string synchronously (blocks until complete)."""
    # Check for special actions first (tput, say:)
    if action == "tput":
        play_tput_bell()
    elif action.startswith("say:"):
        play_say_text(action[len("say:"):])
    elif callable(action):
        try:
            action(msg)
        except Exception as exc:
            _safe_print(f"⚠️  Custom action error: {exc}")
    else:
        # Treat as audio file path
        action_path = Path(action)
        file_path = None
        if action_path.is_absolute() and action_path.exists():
            file_path = action
        elif not action_path.is_absolute():
            # Check current directory first
            if action_path.exists():
                file_path = str(action_path)
            # Then check sounds directory
            else:
                sounds_path = SOUNDS_DIR / action_path
                if sounds_path.exists():
                    file_path = str(sounds_path)
        
        if file_path:
            # Play synchronously and wait for it to finish
            try:
                subprocess.run(["afplay", file_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception as exc:
                _safe_print(f"⚠️  Failed to play WAV '{file_path}': {exc}")
        else:
            _safe_print(f"⚠️  Audio file not found: {action}")


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
        action = row["action_combo"].currentData()
        if action == "say":
            row["text_edit"].setPlaceholderText("Text to read aloud")
            row["text_edit"].setReadOnly(False)
            row["text_edit"].setEnabled(True)
        elif action == "custom":
            row["text_edit"].setPlaceholderText("Path to audio file")
            row["text_edit"].setReadOnly(True)
            row["text_edit"].setEnabled(False)
        else:
            row["text_edit"].setPlaceholderText("")
            row["text_edit"].setReadOnly(False)
            row["text_edit"].setEnabled(False)
        if "browse_btn" in row:
            row["browse_btn"].setVisible(action == "custom")

    def _browse_sound_file(self, row: dict[str, QtWidgets.QWidget]) -> None:
        file_dialog = QtWidgets.QFileDialog(self)
        file_dialog.setFileMode(QtWidgets.QFileDialog.FileMode.ExistingFile)
        file_dialog.setNameFilter("Audio files (*.wav *.mp3 *.aac *.m4a *.aiff *.caf *.aif)")
        if file_dialog.exec():
            selected_files = file_dialog.selectedFiles()
            if selected_files:
                source_path = Path(selected_files[0])
                # Copy to sounds dir with original name
                dest_path = SOUNDS_DIR / source_path.name
                try:
                    import shutil
                    shutil.copy2(source_path, dest_path)
                    row["text_edit"].setText(str(dest_path))
                    self._on_mapping_changed()
                except Exception as exc:
                    QtWidgets.QMessageBox.warning(self, "Error", f"Failed to copy file: {exc}")

    def _clear_mapping_rows(self) -> None:
        while self.mapping_rows:
            self._remove_mapping_row(self.mapping_rows[0], keep_one=False)

    def _remove_mapping_row(self, row: dict[str, QtWidgets.QWidget], keep_one: bool = True) -> None:
        if row not in self.mapping_rows:
            return
        self.mapping_rows.remove(row)
        for widget_key in ("trigger_edit", "action_combo", "text_edit", "browse_btn", "remove_btn"):
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
        trigger_edit.setText(trigger)

        action_combo = QtWidgets.QComboBox()
        for label, value in BUILTIN_ACTION_CHOICES:
            action_combo.addItem(label, value)
        index = next((i for i in range(action_combo.count()) if action_combo.itemData(i) == action), 0)
        action_combo.setCurrentIndex(index)

        text_edit = QtWidgets.QLineEdit(text)
        if action == "say":
            text_edit.setPlaceholderText("Text to read aloud")
        elif action == "custom":
            text_edit.setPlaceholderText("Path to audio file")
            text_edit.setReadOnly(True)
        else:
            text_edit.setPlaceholderText("")
        text_edit.setEnabled(action == "say")

        browse_btn = QtWidgets.QPushButton("Browse...")
        browse_btn.setFixedWidth(80)
        browse_btn.setVisible(action == "custom")
        browse_btn.clicked.connect(lambda: self._browse_sound_file(row))

        remove_btn = QtWidgets.QPushButton("Remove")
        remove_btn.setFixedWidth(90)

        row_layout.addWidget(trigger_edit, 1)
        row_layout.addWidget(action_combo)
        row_layout.addWidget(text_edit, 2)
        row_layout.addWidget(browse_btn)
        row_layout.addWidget(remove_btn)

        self.mapping_layout.addLayout(row_layout)

        row: dict[str, QtWidgets.QWidget] = {
            "layout": row_layout,
            "trigger_edit": trigger_edit,
            "action_combo": action_combo,
            "text_edit": text_edit,
            "browse_btn": browse_btn,
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
            # Handle both single action (string) and multiple actions (list)
            actions = [action] if isinstance(action, str) else action
            if not isinstance(actions, list):
                continue
            for single_action in actions:
                text = ""
                action_value = single_action
                if isinstance(single_action, str) and single_action.startswith("say:"):
                    text = single_action[len("say:"):]
                    action_value = "say"
                elif isinstance(single_action, str) and Path(single_action).is_absolute():
                    text = single_action
                    action_value = "custom"
                self.add_mapping_row(trigger=str(trigger), action=str(action_value), text=text)
        if not self.mapping_rows:
            self.add_mapping_row()

    def _build_trigger_map_from_rows(self) -> dict[str, list]:
        # Group rows by trigger, preserving order
        trigger_to_rows: dict[str, list] = {}
        for row in self.mapping_rows:
            trigger = row["trigger_edit"].text().strip()
            if not trigger:
                continue
            if trigger not in trigger_to_rows:
                trigger_to_rows[trigger] = []
            trigger_to_rows[trigger].append(row)

        # Build trigger map with lists of actions
        new_map: dict[str, list] = {}
        for trigger, rows in trigger_to_rows.items():
            actions = []
            for row in rows:
                action_value = row["action_combo"].currentData()
                if action_value == "say":
                    text = row["text_edit"].text().strip()
                    if text:
                        actions.append(f"say:{text}")
                elif action_value == "custom":
                    path = row["text_edit"].text().strip()
                    if path:
                        actions.append(path)
                else:
                    actions.append(action_value)
            if actions:
                new_map[trigger] = actions
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
