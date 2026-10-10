#!/usr/bin/env python3
"""Bench checks for the GSM module through a USB-to-serial adapter, before the ESP32
is involved (build phase 2).

    pip install pyserial
    python tools/modem_bench.py --port /dev/ttyUSB0 check      # alive, SIM, network, signal
    python tools/modem_bench.py --port /dev/ttyUSB0 watch      # wait for calls, show caller ID
    python tools/modem_bench.py --port /dev/ttyUSB0 watch --answer --hangup-after 10
    python tools/modem_bench.py --port /dev/ttyUSB0 sms --to +9198... --text "hello"
    python tools/modem_bench.py --port /dev/ttyUSB0 console    # type AT commands yourself

Phase 2 is done when `watch --answer --hangup-after 10` answers a call from a test
phone and hangs up after 10 seconds, showing the right caller number.
"""
from __future__ import annotations

import argparse
import re
import sys
import threading
import time


def open_port(port: str, baud: int):
    import serial

    return serial.Serial(port, baud, timeout=0.2)


def cmd(ser, command: str, timeout: float = 3.0) -> tuple[bool, list[str]]:
    ser.reset_input_buffer()
    ser.write((command + "\r").encode())
    lines, end = [], time.time() + timeout
    while time.time() < end:
        raw = ser.readline().decode(errors="replace").strip()
        if not raw or raw == command:
            continue
        if raw == "OK":
            return True, lines
        if "ERROR" in raw:
            lines.append(raw)
            return False, lines
        lines.append(raw)
    return False, lines + ["(timeout)"]


def check(ser) -> int:
    problems = 0
    steps = [
        ("AT", "module answers"),
        ("ATE0", "echo off"),
        ("ATI", "module model"),
        ("AT+CPIN?", "SIM ready"),
        ("AT+CREG?", "registered (2G/3G)"),
        ("AT+CEREG?", "registered (4G)"),
        ("AT+COPS?", "operator"),
        ("AT+CSQ", "signal"),
        ("AT+CLIP=1", "caller ID on"),
        ("AT+CCLK?", "network time"),
    ]
    for c, what in steps:
        ok, lines = cmd(ser, c)
        text = " | ".join(lines)
        note = ""
        if c == "AT+CPIN?" and "READY" not in text:
            ok, note = False, "insert or unlock the SIM"
        if c in ("AT+CREG?", "AT+CEREG?"):
            m = re.search(r",(\d)", text)
            stat = int(m.group(1)) if m else -1
            note = {1: "home", 5: "roaming", 2: "searching", 3: "denied", 0: "not registered"}.get(stat, "?")
        if c == "AT+CSQ":
            m = re.search(r"\+CSQ: (\d+)", text)
            rssi = int(m.group(1)) if m else 99
            note = "unknown" if rssi == 99 else ("good" if rssi >= 15 else "fair" if rssi >= 10 else "weak: move the antenna")
        print(f"[{'PASS' if ok else 'FAIL'}] {what:22} {c:11} {text} {('— ' + note) if note else ''}")
        problems += 0 if ok else 1
    print("\nVoice calls need registration on CREG (2G fallback) or VoLTE on CEREG. "
          "If neither shows home/roaming, check the SIM and antenna.")
    return 1 if problems else 0


def watch(ser, answer: bool, rings: int, hangup_after: float) -> int:
    print("Waiting for calls. Ctrl-C to stop.")
    cmd(ser, "AT+CLIP=1")
    count, number = 0, None
    try:
        while True:
            line = ser.readline().decode(errors="replace").strip()
            if not line:
                continue
            print(f"  {time.strftime('%H:%M:%S')} {line}")
            if line.startswith("+CLIP:"):
                m = re.search(r'"([^"]*)"', line)
                number = m.group(1) if m else ""
                print(f"  -> caller ID: {number or 'withheld'}")
            if line == "RING":
                count += 1
                if answer and count >= rings:
                    ok, _ = cmd(ser, "ATA", 5)
                    print(f"  -> ATA {'OK' if ok else 'FAILED'}")
                    if ok and hangup_after > 0:
                        time.sleep(hangup_after)
                        ok, _ = cmd(ser, "AT+CHUP")
                        print(f"  -> AT+CHUP {'OK' if ok else 'FAILED'} after {hangup_after:.0f} s")
                    count = 0
            if line in ("NO CARRIER", "MISSED_CALL") or line.startswith("VOICE CALL: END"):
                print("  -> call ended")
                count = 0
    except KeyboardInterrupt:
        return 0


def sms(ser, to: str, text: str) -> int:
    cmd(ser, "AT+CMGF=1")
    ser.write(f'AT+CMGS="{to}"\r'.encode())
    time.sleep(0.5)
    if b">" not in ser.read(64):
        print("no SMS prompt from the module")
        return 1
    ser.write(text.encode() + b"\x1a")
    end = time.time() + 60
    while time.time() < end:
        line = ser.readline().decode(errors="replace").strip()
        if line == "OK":
            print("SMS sent")
            return 0
        if "ERROR" in line:
            print(f"SMS failed: {line}")
            return 1
    print("SMS timed out")
    return 1


def console(ser) -> int:
    def reader():
        while True:
            line = ser.readline().decode(errors="replace").strip()
            if line:
                print(f"< {line}")

    threading.Thread(target=reader, daemon=True).start()
    print("Type AT commands; Ctrl-D to quit.")
    try:
        for line in sys.stdin:
            ser.write((line.strip() + "\r").encode())
    except KeyboardInterrupt:
        pass
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--port", required=True)
    p.add_argument("--baud", type=int, default=115200)
    sub = p.add_subparsers(dest="action", required=True)
    sub.add_parser("check")
    w = sub.add_parser("watch")
    w.add_argument("--answer", action="store_true")
    w.add_argument("--rings", type=int, default=1)
    w.add_argument("--hangup-after", type=float, default=10)
    s = sub.add_parser("sms")
    s.add_argument("--to", required=True)
    s.add_argument("--text", default="Test from the call assistant module")
    sub.add_parser("console")
    args = p.parse_args()
    ser = open_port(args.port, args.baud)
    if args.action == "check":
        return check(ser)
    if args.action == "watch":
        return watch(ser, args.answer, args.rings, args.hangup_after)
    if args.action == "sms":
        return sms(ser, args.to, args.text)
    return console(ser)


if __name__ == "__main__":
    sys.exit(main())
