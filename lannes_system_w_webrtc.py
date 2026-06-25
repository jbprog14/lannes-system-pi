#!/usr/bin/env python3
# lannes_system.py  -  UNIFIED Lannes System (single file)
#
# ONE process owns the camera and runs everything:
#   1. TELEMETRY heartbeat -> lanness-tower-01/telemetry   (Current Threat card)
#   2. RPi HEALTH metrics  -> expanded_metrics             (RPi Health panel)
#   3. REAL PIR breach      -> GPIO17 edge -> capture + history write
#   4. MANUAL test breach   -> press ENTER -> same breach flow
#   5. WEBRTC live feed      -> aiortc sender via Firebase signaling
#                              (works on SAME WIFI now; add TURN later for internet)
#
# Architecture: everything runs inside ONE asyncio event loop.
# The camera is owned by this process only; both breach capture and the
# WebRTC video track pull frames from the same shared Picamera2 instance.
#
# Requirements (on the Pi):
#   sudo apt install python3-opencv -y
#   pip3 install requests gpiod picamera2 aiortc aiohttp av --break-system-packages
#
# Run:  python3 lannes_system.py     (Ctrl-C to stop)

import os
import re
import time
import json
import base64
import asyncio
import datetime
import subprocess
import functools

import requests
import cv2
import numpy as np

from picamera2 import Picamera2
from av import VideoFrame
from aiortc import (
    RTCPeerConnection, RTCSessionDescription, RTCConfiguration,
    RTCIceServer, VideoStreamTrack, RTCIceCandidate,
)

# ===========================================================================
# CONFIG
# ===========================================================================
FIREBASE_HOST   = "https://lanness-sytem-default-rtdb.firebaseio.com"
HISTORY_PATH    = "/lanness-tower-01/history"
TELEMETRY_PATH  = "/lanness-tower-01/telemetry"
HEALTH_PATH     = "/expanded_metrics"
SIGNALING_PATH  = "/webrtc_signaling"

SENSOR_NAME = "PIR Trigger"
ZONE        = "ZONE_A"
STATUS_TEXT = "Perimeter Breach"

GPIO_PIN     = 17
CHIP_PATH    = "/dev/gpiochip0"
PIR_ACTIVE_LOW = True              # True = sensor pulls LOW on detect
DEBOUNCE_SECONDS = 3.0

IMAGE_DIR     = os.path.expanduser("~/breach_images")
CAM_SIZE      = (640, 480)
THUMB_SIZE    = (320, 240)
THUMB_QUALITY = 50
MAX_EVENTS_IN_DB = 30

TELEMETRY_INTERVAL = 2.0
HEALTH_INTERVAL    = 5.0
MAX_HEALTH_IN_DB   = 40
PIR_POLL_INTERVAL  = 0.05          # 20 Hz edge polling for the sensor

# ICE servers - STUN only (works on same WiFi). Add TURN here for internet/field.
ICE_SERVERS = [RTCIceServer(urls="stun:stun.l.google.com:19302")]

os.makedirs(IMAGE_DIR, exist_ok=True)

# ===========================================================================
# SHARED CAMERA (owned by this process only)
# ===========================================================================
print("Initializing camera...")
picam = Picamera2()
picam.configure(picam.create_video_configuration(main={"size": CAM_SIZE, "format": "RGB888"}))
picam.start()
time.sleep(1)
print("Camera online.")

cam_lock = asyncio.Lock()

def grab_frame_bgr():
    """Picamera2 'RGB888' actually returns BGR-ordered bytes. Return as-is (BGR)."""
    return picam.capture_array()

# ===========================================================================
# ASYNC HTTP HELPERS (don't block the event loop)
# ===========================================================================
async def _to_thread(func, *args, **kwargs):
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))

async def fb_get(path, params=""):
    return await _to_thread(lambda: requests.get(f"{FIREBASE_HOST}{path}.json{params}", timeout=10))

async def fb_put(path, payload):
    return await _to_thread(lambda: requests.put(f"{FIREBASE_HOST}{path}.json", data=json.dumps(payload), timeout=10))

async def fb_post(path, payload):
    return await _to_thread(lambda: requests.post(f"{FIREBASE_HOST}{path}.json", data=json.dumps(payload), timeout=15))

async def fb_patch(path, payload):
    return await _to_thread(lambda: requests.patch(f"{FIREBASE_HOST}{path}.json", data=json.dumps(payload), timeout=10))

async def fb_delete(path):
    return await _to_thread(lambda: requests.delete(f"{FIREBASE_HOST}{path}.json", timeout=10))

# ===========================================================================
# BREACH CAPTURE
# ===========================================================================
async def capture_full_and_thumb(ts):
    async with cam_lock:
        frame = await _to_thread(grab_frame_bgr)
    fname = f"breach_{ts}.jpg"
    await _to_thread(cv2.imwrite, os.path.join(IMAGE_DIR, fname), frame)
    thumb = cv2.resize(frame, THUMB_SIZE, interpolation=cv2.INTER_AREA)
    ok, enc = cv2.imencode(".jpg", thumb, [int(cv2.IMWRITE_JPEG_QUALITY), THUMB_QUALITY])
    thumb_b64 = ("data:image/jpeg;base64," + base64.b64encode(enc).decode("utf-8")) if ok else ""
    return fname, thumb_b64

_last_breach = 0.0

async def handle_breach(source):
    global _last_breach
    now = time.time()
    if now - _last_breach < DEBOUNCE_SECONDS:
        return
    _last_breach = now

    ts = int(now)
    dt = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"\n[BREACH] ({source}) {dt}")

    fname, thumb = await capture_full_and_thumb(ts)
    print(f"   image -> {fname} | thumb ~{len(thumb)//1024}KB")

    record = {
        "sensor": SENSOR_NAME, "status": f"{STATUS_TEXT} - {ZONE}", "zone": ZONE,
        "timestamp": ts, "datetime": dt, "thumbnail": thumb, "image_local": fname,
    }
    try:
        r = await fb_post(HISTORY_PATH, record)
        print(f"   history OK -> {r.json().get('name')}")
        await prune_path(HISTORY_PATH, MAX_EVENTS_IN_DB, lambda kv: kv[1].get("timestamp", 0))
    except Exception as e:
        print(f"   [!] history write FAILED: {e}")

async def prune_path(path, max_keep, time_key):
    try:
        r = await fb_get(path)
        data = r.json() or {}
        items = sorted(data.items(), key=time_key)
        for key, _ in items[:-max_keep] if len(items) > max_keep else []:
            await fb_delete(f"{path}/{key}")
    except Exception as e:
        print(f"   prune skipped ({path}): {e}")

# ===========================================================================
# RPi HEALTH
# ===========================================================================
def _run(cmd):
    try:
        return subprocess.check_output(cmd, shell=True, text=True, timeout=5).strip()
    except Exception:
        return ""

def build_health(latency_ms):
    # temp
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            temp = round(int(f.read().strip()) / 1000.0, 1)
    except Exception:
        temp = 0.0
    # clock
    m = re.search(r"=(\d+)", _run("vcgencmd measure_clock arm"))
    clock = int(int(m.group(1)) / 1_000_000) if m else 0
    # uptime
    try:
        with open("/proc/uptime") as f:
            uptime = round(float(f.read().split()[0]) / 3600.0, 2)
    except Exception:
        uptime = 0.0
    # ram
    total = free = avail = 0
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):      total = int(line.split()[1]) * 1024
                elif line.startswith("MemFree:"):      free  = int(line.split()[1]) * 1024
                elif line.startswith("MemAvailable:"): avail = int(line.split()[1]) * 1024
    except Exception:
        pass
    used = total - avail if total else 0
    pct = round((used / total) * 100, 2) if total else 0.0
    # throttle bitmask
    mm = re.search(r"0x([0-9a-fA-F]+)", _run("vcgencmd get_throttled"))
    bits = int(mm.group(1), 16) if mm else 0
    # fan
    try:
        fan = int(_run("cat /sys/class/thermal/cooling_device0/cur_state 2>/dev/null"))
    except Exception:
        fan = 0
    return {
        "network_performance": {"transmission_latency_ms": latency_ms},
        "processing_core": {
            "clock_speed_mhz": clock, "ram_free_bytes": free, "ram_total_bytes": total,
            "ram_usage_percent": pct, "ram_used_bytes": used,
        },
        "system_identity": {
            "kernel_version": _run("uname -r") or "unknown",
            "timestamp": int(time.time()), "uptime_hours": uptime,
        },
        "thermal_and_power": {
            "cpu_temperature_celsius": temp, "fan_target_state": fan,
            "pmic_voltages": _run("vcgencmd pmic_read_adc") or "",
        },
        "throttling_flags": {
            "under_voltage_now":              bool(bits & (1 << 0)),
            "frequency_capped_now":           bool(bits & (1 << 1)),
            "throttled_now":                  bool(bits & (1 << 2)),
            "temperature_limit_now":          bool(bits & (1 << 3)),
            "under_voltage_has_occurred":     bool(bits & (1 << 16)),
            "frequency_capped_has_occurred":  bool(bits & (1 << 17)),
            "throttled_has_occurred":         bool(bits & (1 << 18)),
            "temperature_limit_has_occurred": bool(bits & (1 << 19)),
        },
    }

# ===========================================================================
# WEBRTC VIDEO TRACK (pulls from the shared camera)
# ===========================================================================
class SharedCamTrack(VideoStreamTrack):
    async def recv(self):
        pts, time_base = await self.next_timestamp()
        async with cam_lock:
            frame = await _to_thread(grab_frame_bgr)
        # Picamera2 RGB888 -> bytes are BGR-ordered; tell av it's bgr24 (correct colors)
        vf = VideoFrame.from_ndarray(frame, format="bgr24")
        vf.pts = pts
        vf.time_base = time_base
        return vf

# ===========================================================================
# BACKGROUND TASKS
# ===========================================================================
async def telemetry_task(read_pir):
    while True:
        try:
            pir = read_pir()
            await fb_put(TELEMETRY_PATH, {
                "linkQuality": "WiFi_LAN",
                "metrics": {
                    "acoustic_frequency_hz": 0, "acoustic_target": "None",
                    "lidar_distance_m": 0.0, "pir_trigger": pir,
                },
                "status": "Active" if pir == 1 else "Idle",
                "systemTime": int(time.time()),
            })
        except Exception as e:
            print(f"   telemetry failed: {e}")
        await asyncio.sleep(TELEMETRY_INTERVAL)

async def health_task():
    while True:
        try:
            t0 = time.time()
            await fb_get(TELEMETRY_PATH, "?shallow=true")
            latency = int((time.time() - t0) * 1000)
            await fb_post(HEALTH_PATH, build_health(latency))
            await prune_path(HEALTH_PATH, MAX_HEALTH_IN_DB,
                             lambda kv: kv[1].get("system_identity", {}).get("timestamp", 0))
        except Exception as e:
            print(f"   health failed: {e}")
        await asyncio.sleep(HEALTH_INTERVAL)

async def manual_trigger_task():
    import sys
    # If there's no real terminal (e.g. running as a systemd service),
    # disable the ENTER trigger instead of crashing on EOFError.
    if not sys.stdin or not sys.stdin.isatty():
        print("No terminal attached - manual ENTER trigger disabled (service mode).")
        return
    loop = asyncio.get_event_loop()
    while True:
        try:
            await loop.run_in_executor(None, input)
            await handle_breach("MANUAL")
        except (EOFError, KeyboardInterrupt):
            print("Manual trigger input closed - disabling.")
            return

async def manual_trigger_task():
    import sys
    if not sys.stdin or not sys.stdin.isatty():
        print("No terminal attached - manual ENTER trigger disabled (service mode).")
        return
    loop = asyncio.get_event_loop()
    while True:
        try:
            await loop.run_in_executor(None, input)
            await handle_breach("MANUAL")
        except (EOFError, KeyboardInterrupt):
            print("Manual trigger input closed - disabling.")
            return

async def pir_sensor_task(request, read_pir_raw):
    """Poll the sensor for a falling-edge style breach."""
    last = 1
    while True:
        cur = read_pir_raw()
        if cur == 0 and last == 1:        # detected (active low)
            await handle_breach("SENSOR")
        last = cur
        await asyncio.sleep(PIR_POLL_INTERVAL)

# ===========================================================================
# WEBRTC SIGNALING (with trickle ICE through Firebase)
# ===========================================================================
async def webrtc_task():
    pc = None
    handled_offer = None
    print("WebRTC daemon active - waiting for dashboard offer...")

    while True:
        try:
            r = await fb_get(SIGNALING_PATH)
            data = r.json() if r.status_code == 200 else None

            if data and "offer" in data:
                offer_sdp = data["offer"].get("sdp")
                # New offer we haven't answered yet?
                if offer_sdp and offer_sdp != handled_offer and not data.get("answer"):
                    print("\n[WebRTC] Offer received - building peer connection...")
                    handled_offer = offer_sdp

                    if pc:
                        try: await pc.close()
                        except Exception: pass

                    pc = RTCPeerConnection(RTCConfiguration(iceServers=ICE_SERVERS))
                    pc.addTrack(SharedCamTrack())

                    @pc.on("iceconnectionstatechange")
                    async def on_ice():
                        print(f"[WebRTC] ICE state: {pc.iceConnectionState}")

                    @pc.on("connectionstatechange")
                    async def on_conn():
                        print(f"[WebRTC] Connection state: {pc.connectionState}")

                    # Push our ICE candidates to Firebase as they're gathered (trickle)
                    @pc.on("icecandidate")
                    async def on_cand(candidate):
                        if candidate:
                            await fb_patch(f"{SIGNALING_PATH}/pi_candidates/{int(time.time()*1000)}",
                                           {"candidate": candidate.to_sdp(),
                                            "sdpMid": candidate.sdpMid,
                                            "sdpMLineIndex": candidate.sdpMLineIndex})

                    await pc.setRemoteDescription(RTCSessionDescription(sdp=offer_sdp, type="offer"))
                    answer = await pc.createAnswer()
                    await pc.setLocalDescription(answer)
                    await fb_patch(SIGNALING_PATH, {
                        "answer": {"type": pc.localDescription.type, "sdp": pc.localDescription.sdp}
                    })
                    print("[WebRTC] Answer posted. Negotiating media...")

                # Pull any browser ICE candidates and add them
                if pc and data.get("browser_candidates"):
                    for _, c in data["browser_candidates"].items():
                        try:
                            await pc.addIceCandidate(RTCIceCandidate(
                                sdpMid=c.get("sdpMid"),
                                sdpMLineIndex=c.get("sdpMLineIndex"),
                                candidate=c.get("candidate"),
                            ))
                        except Exception:
                            pass

                # Offer cleared (browser left) -> reset
                if not data.get("offer") and pc:
                    print("[WebRTC] Offer gone - closing peer.")
                    try: await pc.close()
                    except Exception: pass
                    pc = None
                    handled_offer = None

        except Exception as e:
            print(f"   webrtc loop warning: {e}")
        await asyncio.sleep(2.0)

# ===========================================================================
# MAIN
# ===========================================================================
async def main():
    import gpiod
    from gpiod.line import Direction

    line_cfg = {GPIO_PIN: gpiod.LineSettings(direction=Direction.INPUT)}
    request = gpiod.request_lines(CHIP_PATH, consumer="Lannes_System", config=line_cfg)

    def read_pir_raw():
        val = request.get_value(GPIO_PIN)
        try:
            return int(val.value) if hasattr(val, "value") else int(val)
        except Exception:
            return 1

    def read_pir():
        raw = read_pir_raw()
        detected = (raw == 0) if PIR_ACTIVE_LOW else (raw == 1)
        return 1 if detected else 0

    print("=" * 55)
    print(" LANNES SYSTEM ONLINE (unified + WebRTC)")
    print(f"  - Sensor:    GPIO{GPIO_PIN}")
    print("  - Manual:    press ENTER to fire a breach")
    print(f"  - Telemetry: every {TELEMETRY_INTERVAL}s")
    print(f"  - Health:    every {HEALTH_INTERVAL}s")
    print("  - WebRTC:    waiting for dashboard (same WiFi)")
    print("  - Ctrl-C to stop")
    print("=" * 55)

    try:
        await asyncio.gather(
            telemetry_task(read_pir),
            health_task(),
            manual_trigger_task(),
            pir_sensor_task(request, read_pir_raw),
            webrtc_task(),
        )
    finally:
        request.release()
        picam.stop()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nShutting down...")
