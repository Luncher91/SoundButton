#!/usr/bin/env python3
"""Serial simulator for SoundButton.

This helper creates a pseudo-terminal pair and writes simulated serial
messages to the slave end. Use it together with `serial_btn_sound.py` by
pointing the app at the printed slave path.

Example:
  python3 serial_simulator.py
  python3 serial_btn_sound.py --port /dev/pts/5

Controls:
  1 - send BTN1
  2 - send BTN2
  3 - send BTN3
  h - toggle automatic HEART_BEAT messages
  t - send a custom command
  q - quit
"""

from __future__ import annotations

import argparse
import os
import pty
import threading
import time
from pathlib import Path

DEFAULT_HEARTBEAT_INTERVAL = 1.0
DEFAULT_BUTTONS = ["BTN1", "BTN2", "BTN3"]


def write_line(master_fd: int, text: str) -> None:
    data = (text.strip() + "\n").encode("utf-8")
    os.write(master_fd, data)


def heartbeat_loop(master_fd: int, interval: float, stop_event: threading.Event) -> None:
    while not stop_event.is_set():
        write_line(master_fd, "HEART_BEAT")
        for _ in range(int(interval * 10)):
            if stop_event.is_set():
                return
            time.sleep(0.1)


def print_instructions(slave_path: Path, interval: float) -> None:
    print("Serial simulator started.")
    print(f"Slave device path: {slave_path}")
    print("Open that path from your serial app.")
    print("")
    print("Controls:")
    print("  1 - send BTN1")
    print("  2 - send BTN2")
    print("  3 - send BTN3")
    print("  h - toggle HEART_BEAT on/off")
    print("  t - send a custom command")
    print("  q - quit")
    print("")
    print(f"Automatic HEART_BEAT every {interval:.1f}s is active.")
    print("")


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate a serial device for SoundButton.")
    parser.add_argument("--heartbeat-interval", type=float, default=DEFAULT_HEARTBEAT_INTERVAL,
                        help="Seconds between automatic HEART_BEAT messages.")
    parser.add_argument("--no-heartbeat", action="store_true", help="Disable automatic HEART_BEAT messages.")
    args = parser.parse_args()

    master_fd, slave_fd = pty.openpty()
    slave_name = Path(os.ttyname(slave_fd))

    stop_event = threading.Event()
    heartbeat_active = threading.Event()
    heartbeat_active.set()

    heartbeat_thread = threading.Thread(
        target=lambda: heartbeat_loop(master_fd, args.heartbeat_interval, stop_event),
        daemon=True,
    )

    if not args.no_heartbeat:
        heartbeat_thread.start()

    print_instructions(slave_name, args.heartbeat_interval)

    try:
        while True:
            user_input = input("Enter action [1/2/3/h/t/q]: ").strip().lower()
            if user_input == "q":
                print("Exiting simulator.")
                break
            if user_input == "h":
                if heartbeat_active.is_set():
                    heartbeat_active.clear()
                    stop_event.set()
                    print("Automatic HEART_BEAT disabled.")
                else:
                    stop_event.clear()
                    heartbeat_thread = threading.Thread(
                        target=lambda: heartbeat_loop(master_fd, args.heartbeat_interval, stop_event),
                        daemon=True,
                    )
                    heartbeat_thread.start()
                    heartbeat_active.set()
                    print("Automatic HEART_BEAT enabled.")
                continue
            if user_input == "1":
                write_line(master_fd, "BTN1")
                print("Sent BTN1")
                continue
            if user_input == "2":
                write_line(master_fd, "BTN2")
                print("Sent BTN2")
                continue
            if user_input == "3":
                write_line(master_fd, "BTN3")
                print("Sent BTN3")
                continue
            if user_input == "t":
                custom = input("Custom command: ").strip()
                if custom:
                    write_line(master_fd, custom)
                    print(f"Sent {custom}")
                continue
            print("Unknown input. Use 1/2/3/h/t/q.")
    except (EOFError, KeyboardInterrupt):
        print("\nStopping simulator.")
    finally:
        stop_event.set()
        try:
            os.close(master_fd)
            os.close(slave_fd)
        except OSError:
            pass


if __name__ == "__main__":
    main()
