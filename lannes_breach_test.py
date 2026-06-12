#!/usr/bin/env python3
# lannes_breach_test.py  (v2)
# Press ENTER to fire a test breach. No GPIO needed. Proves the
# camera -> save -> thumbnail -> Firebase -> website path end-to-end.

import os, time, json, base64, datetime, requests, cv2
from picamera2 import Picamera2

FIREBASE_HOST = "https://lanness-sytem-default-rtdb.firebaseio.com"
HISTORY_PATH  = "/lanness-tower-01/history"
SENSOR_NAME, ZONE, STATUS_TEXT = "HW-201 IR Sensor (TEST)", "ZONE_A", "Perimeter Breach"
IMAGE_DIR = os.path.expanduser("~/breach_images")
FULL_SIZE, THUMB_SIZE, THUMB_QUALITY = (640, 480), (320, 240), 50
os.makedirs(IMAGE_DIR, exist_ok=True)

print("Initializing camera...")
cam = Picamera2()
cam.configure(cam.create_video_configuration(main={"size": FULL_SIZE, "format": "RGB888"}))
cam.start(); time.sleep(1)

def fire():
    ts = int(time.time())
    dt = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    frame = cam.capture_array()
    fname = f"breach_{ts}.jpg"
    cv2.imwrite(os.path.join(IMAGE_DIR, fname), frame)
    thumb = cv2.resize(frame, THUMB_SIZE, interpolation=cv2.INTER_AREA)
    ok, enc = cv2.imencode(".jpg", thumb, [int(cv2.IMWRITE_JPEG_QUALITY), THUMB_QUALITY])
    b64 = "data:image/jpeg;base64," + base64.b64encode(enc).decode() if ok else ""
    record = {
        "sensor": SENSOR_NAME, "status": f"{STATUS_TEXT} - {ZONE}", "zone": ZONE,
        "timestamp": ts, "datetime": dt, "thumbnail": b64, "image_local": fname,
    }
    r = requests.post(f"{FIREBASE_HOST}{HISTORY_PATH}.json", data=json.dumps(record), timeout=15)
    print(f"[TEST] {dt} | thumb ~{len(b64)//1024}KB | Firebase HTTP {r.status_code} -> {r.json().get('name')}")

print("Press ENTER to fire a test breach (Ctrl-C to quit).")
try:
    while True:
        input(); fire()
except KeyboardInterrupt:
    print("\nbye")
finally:
    cam.stop()
