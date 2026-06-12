#!/usr/bin/env python3
# lannes_system.py  -  UNIFIED Lannes System (single file, overwrite-and-run)
#
# Runs FOUR things at once, sharing ONE GPIO request and one config:
#   1. TELEMETRY heartbeat -> lanness-tower-01/telemetry  (Current Threat card)
#   2. RPi HEALTH metrics  -> expanded_metrics            (RPi Health panel)
#   3. REAL PIR breach      -> GPIO17 edge fires a breach (capture + history write)
#   4. MANUAL test breach   -> press ENTER to fire the same flow
#
# Requirements:
#   sudo apt install python3-opencv -y
#   pip3 install requests gpiod picamera2 --break-system-packages
#
# Run:  python3 lannes_system.py     (Ctrl-C to stop)

import os
import re
import time
import json
import base64
import datetime
import threading
import subprocess
import requests
import cv2
from picamera2 import Picamera2
import gpiod
from gpiod.line import Direction, Edge

# ===========================================================================
# CONFIG
# ===========================================================================
FIREBASE_HOST   = "https://lanness-sytem-default-rtdb.firebaseio.com"
HISTORY_PATH    = "/lanness-tower-01/history"
TELEMETRY_PATH  = "/lanness-tower-01/telemetry"
HEALTH_PATH     = "/expanded_metrics"          # <-- dashboard Stream C reads this

SENSOR_NAME = "PIR Trigger"
ZONE        = "ZONE_A"
STATUS_TEXT = "Perimeter Breach"

GPIO_PIN     = 17
CHIP_PATH    = "/dev/gpiochip0"
TRIGGER_EDGE = Edge.FALLING
PIR_ACTIVE_LOW = True
DEBOUNCE_SECONDS = 3.0

IMAGE_DIR     = os.path.expanduser("~/breach_images")
FULL_SIZE     = (640, 480)
THUMB_SIZE    = (320, 240)
THUMB_QUALITY = 50
MAX_EVENTS_IN_DB = 30

TELEMETRY_INTERVAL = 2.0
HEALTH_INTERVAL    = 5.0           # push health every 5s
MAX_HEALTH_IN_DB   = 40           # keep newest 40 (matches dashboard slice)

os.makedirs(IMAGE_DIR, exist_ok=True)

# ===========================================================================
# SHARED STATE
# ===========================================================================
cam = None
cam_lock = threading.Lock()
last_breach_time = 0.0
stop_flag = threading.Event()

# ===========================================================================
# CAMERA
# ===========================================================================
def init_camera():
    global cam
    print("Initializing camera...")
    cam = Picamera2()
    cam.configure(cam.create_video_configuration(main={"size": FULL_SIZE, "format": "RGB888"}))
    cam.start()
    time.sleep(1)

def capture_full_and_thumb(ts):
    with cam_lock:
        frame = cam.capture_array()
    fname = f"breach_{ts}.jpg"
    cv2.imwrite(os.path.join(IMAGE_DIR, fname), frame)
    thumb = cv2.resize(frame, THUMB_SIZE, interpolation=cv2.INTER_AREA)
    ok, enc = cv2.imencode(".jpg", thumb, [int(cv2.IMWRITE_JPEG_QUALITY), THUMB_QUALITY])
    thumb_b64 = ("data:image/jpeg;base64," + base64.b64encode(enc).decode("utf-8")) if ok else ""
    return fname, thumb_b64

# ===========================================================================
# FIREBASE
# ===========================================================================
def post_breach(record):
    r = requests.post(f"{FIREBASE_HOST}{HISTORY_PATH}.json", data=json.dumps(record), timeout=15)
    r.raise_for_status()
    return r.json().get("name")

def prune_path(path, max_keep, time_key):
    """Generic pruning: keep newest max_keep by a timestamp extractor."""
    try:
        data = requests.get(f"{FIREBASE_HOST}{path}.json", timeout=15).json() or {}
        items = sorted(data.items(), key=time_key)
        if len(items) > max_keep:
            for key, _ in items[:-max_keep]:
                requests.delete(f"{FIREBASE_HOST}{path}/{key}.json", timeout=15)
    except Exception as e:
        print(f"   prune skipped ({path}): {e}")

def push_telemetry(pir):
    now = int(time.time())
    payload = {
        "linkQuality": "WiFi_LAN",
        "metrics": {
            "acoustic_frequency_hz": 0,
            "acoustic_target": "None",
            "lidar_distance_m": 0.0,
            "pir_trigger": pir,
        },
        "status": "Active" if pir == 1 else "Idle",
        "systemTime": now,
    }
    requests.put(f"{FIREBASE_HOST}{TELEMETRY_PATH}.json", data=json.dumps(payload), timeout=10)

def post_health(record):
    requests.post(f"{FIREBASE_HOST}{HEALTH_PATH}.json", data=json.dumps(record), timeout=10)

# ===========================================================================
# RPi 5 HEALTH METRIC COLLECTION
# ===========================================================================
def _run(cmd):
    try:
        return subprocess.check_output(cmd, shell=True, text=True, timeout=5).strip()
    except Exception:
        return ""

def read_cpu_temp():
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            return round(int(f.read().strip()) / 1000.0, 1)
    except Exception:
        return 0.0

def read_clock_mhz():
    out = _run("vcgencmd measure_clock arm")        # arm=<hz>
    m = re.search(r"=(\d+)", out)
    return int(int(m.group(1)) / 1_000_000) if m else 0

def read_uptime_hours():
    try:
        with open("/proc/uptime") as f:
            return round(float(f.read().split()[0]) / 3600.0, 2)
    except Exception:
        return 0.0

def read_kernel():
    return _run("uname -r") or "unknown"

def read_ram():
    total = free = avail = 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):     total = int(line.split()[1]) * 1024
                elif line.startswith("MemFree:"):     free  = int(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"):avail = int(line.split()[1]) * 1024
    except Exception:
        pass
    used = total - avail if total else 0
    pct = round((used / total) * 100, 2) if total else 0.0
    return total, free, used, pct

def read_throttle_flags():
    """vcgencmd get_throttled -> 0x... bitmask."""
    out = _run("vcgencmd get_throttled")            # throttled=0x0
    m = re.search(r"0x([0-9a-fA-F]+)", out)
    bits = int(m.group(1), 16) if m else 0
    return {
        "under_voltage_now":               bool(bits & (1 << 0)),
        "frequency_capped_now":            bool(bits & (1 << 1)),
        "throttled_now":                   bool(bits & (1 << 2)),
        "temperature_limit_now":           bool(bits & (1 << 3)),
        "under_voltage_has_occurred":      bool(bits & (1 << 16)),
        "frequency_capped_has_occurred":   bool(bits & (1 << 17)),
        "throttled_has_occurred":          bool(bits & (1 << 18)),
        "temperature_limit_has_occurred":  bool(bits & (1 << 19)),
    }

def read_pmic_voltages():
    """Raw multiline string of PMIC rails (panel parses lines like NAME=val)."""
    return _run("vcgencmd pmic_read_adc") or ""

def read_fan_state():
    out = _run("cat /sys/class/thermal/cooling_device0/cur_state 2>/dev/null")
    try:
        return int(out)
    except Exception:
        return 0

def build_health_record(latency_ms):
    total, free, used, pct = read_ram()
    return {
        "network_performance": {"transmission_latency_ms": latency_ms},
        "processing_core": {
            "clock_speed_mhz":   read_clock_mhz(),
            "ram_free_bytes":    free,
            "ram_total_bytes":   total,
            "ram_usage_percent": pct,
            "ram_used_bytes":    used,
        },
        "system_identity": {
            "kernel_version": read_kernel(),
            "timestamp":      int(time.time()),
            "uptime_hours":   read_uptime_hours(),
        },
        "thermal_and_power": {
            "cpu_temperature_celsius": read_cpu_temp(),
            "fan_target_state":        read_fan_state(),
            "pmic_voltages":           read_pmic_voltages(),
        },
        "throttling_flags": read_throttle_flags(),
    }

# ===========================================================================
# SHARED BREACH HANDLER  (sensor + ENTER both call this)
# ===========================================================================
def handle_breach(source):
    global last_breach_time
    now = time.time()
    if now - last_breach_time < DEBOUNCE_SECONDS:
        return
    last_breach_time = now

    ts = int(now)
    dt = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[BREACH] ({source}) {dt}")

    fname, thumb = capture_full_and_thumb(ts)
    print(f"   image -> {fname} | thumb ~{len(thumb)//1024}KB")

    record = {
        "sensor": SENSOR_NAME, "status": f"{STATUS_TEXT} - {ZONE}", "zone": ZONE,
        "timestamp": ts, "datetime": dt, "thumbnail": thumb, "image_local": fname,
    }
    try:
        pid = post_breach(record)
        print(f"   Firebase OK -> {pid}")
        prune_path(HISTORY_PATH, MAX_EVENTS_IN_DB, lambda kv: kv[1].get("timestamp", 0))
    except Exception as e:
        print(f"   [!] history write FAILED: {e} (image still on SD)")

# ===========================================================================
# BACKGROUND THREADS
# ===========================================================================
def telemetry_loop(request):
    while not stop_flag.is_set():
        try:
            val = request.get_value(GPIO_PIN)
            ival = int(val.value) if hasattr(val, "value") else int(val)
            pir = 1 if ((ival == 0) if PIR_ACTIVE_LOW else (ival == 1)) else 0
            push_telemetry(pir)
        except Exception as e:
            print(f"   telemetry failed: {e}")
        stop_flag.wait(TELEMETRY_INTERVAL)

def health_loop():
    while not stop_flag.is_set():
        try:
            # crude latency probe: time a tiny Firebase GET
            t0 = time.time()
            requests.get(f"{FIREBASE_HOST}/.json?shallow=true", timeout=10)
            latency = int((time.time() - t0) * 1000)
            post_health(build_health_record(latency))
            prune_path(HEALTH_PATH, MAX_HEALTH_IN_DB,
                       lambda kv: kv[1].get("system_identity", {}).get("timestamp", 0))
        except Exception as e:
            print(f"   health push failed: {e}")
        stop_flag.wait(HEALTH_INTERVAL)

def manual_trigger_loop():
    while not stop_flag.is_set():
        try:
            input()
            handle_breach("MANUAL")
        except (EOFError, KeyboardInterrupt):
            break

# ===========================================================================
# MAIN
# ===========================================================================
def main():
    init_camera()
    line_cfg = {GPIO_PIN: gpiod.LineSettings(direction=Direction.INPUT, edge_detection=TRIGGER_EDGE)}

    with gpiod.request_lines(CHIP_PATH, consumer="Lannes_System", config=line_cfg) as request:
        threading.Thread(target=telemetry_loop, args=(request,), daemon=True).start()
        threading.Thread(target=health_loop, daemon=True).start()
        threading.Thread(target=manual_trigger_loop, daemon=True).start()

        print("=" * 55)
        print(" LANNES SYSTEM ONLINE")
        print(f"  - Sensor:    GPIO{GPIO_PIN} ({TRIGGER_EDGE.name})")
        print("  - Manual:    press ENTER to fire a breach")
        print(f"  - Telemetry: every {TELEMETRY_INTERVAL}s")
        print(f"  - Health:    every {HEALTH_INTERVAL}s")
        print("  - Ctrl-C to stop")
        print("=" * 55)

        try:
            while True:
                if request.wait_edge_events(timeout=1.0):
                    request.read_edge_events()
                    handle_breach("SENSOR")
        except KeyboardInterrupt:
            print("\nShutting down...")
        finally:
            stop_flag.set()
            if cam:
                cam.stop()

if __name__ == "__main__":
    main()
