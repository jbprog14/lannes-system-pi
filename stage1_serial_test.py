#!/usr/bin/env python3
# stage1_serial_test.py
# Bare-minimum test: NO camera, NO GPIO. Just prove the UART link and the
# full ESP32->modem->Firebase path with a single keypress-triggered metadata send.
# Run this BEFORE the full lannes_tier1_pi.py.

import time
import json
import serial

SERIAL_PORT = '/dev/serial0'   # verify with: ls -l /dev/serial*
BAUD = 115200

ser = serial.Serial(SERIAL_PORT, BAUD, timeout=1)

def pump(seconds):
    """Print everything the gateway says for `seconds`."""
    start = time.time()
    while (time.time() - start) < seconds:
        chunk = ser.read(256).decode('utf-8', errors='ignore')
        if chunk:
            print(chunk, end='', flush=True)

print("Listening to gateway boot for 10s...\n")
pump(10)

print("\n\nPress ENTER to fire a test metadata payload (Ctrl-C to quit).")
try:
    while True:
        input()
        payload = {
            "tower": "lanness-tower-01",
            "zone": "ZONE_A",
            "status": "TEST_PING",
            "event_id": f"test_{int(time.time())}",
            "timestamp": int(time.time())
        }
        line = json.dumps(payload, separators=(',', ':'))
        print(f"--> sending {len(line)} bytes: {line}")
        ser.reset_input_buffer()
        ser.write(line.encode('utf-8'))
        ser.write(b'\n')
        ser.flush()
        print("--- gateway response ---")
        pump(60)   # watch the AT dialogue + result
        print("\n--- end ---\nPress ENTER for another, Ctrl-C to quit.")
except KeyboardInterrupt:
    print("\nbye")
finally:
    ser.close()
