import time
import base64
import serial
import gpiod
from gpiod.line import Direction, Edge
import cv2
from picamera2 import Picamera2

# 1. Initialize physical UART Serial connection on the Pi 5 GPIO header
ser = serial.Serial(
    port='/dev/ttyAMA0',
    baudrate=115200,
    parity=serial.PARITY_NONE,
    stopbits=serial.STOPBITS_ONE,
    bytesize=serial.EIGHTBITS,
    timeout=5
)

GPIO_PIN = 17
CHIP_PATH = '/dev/gpiochip0'

print("🚀 Mounting Hardware Control Lines via gpiod v2 API...")

# 2. Map line properties using explicit v2.x configuration structures
line_cfg = {
    GPIO_PIN: gpiod.LineSettings(
        direction=Direction.INPUT,
        edge_detection=Edge.FALLING
    )
}

print("📷 Initializing persistent hardware camera lines...")
cam = Picamera2()
cam.configure(cam.create_video_configuration(main={"size": (640, 480), "format": "RGB888"}))
cam.start()
print("🔒 Embedded safety loop active. Standing by for IR sensor breach...")

try:
    # Open line request context manager directly on the target chip path
    with gpiod.request_lines(CHIP_PATH, consumer="Hardwired_IR_Monitor", config=line_cfg) as request:
        while True:
            # wait_edge_events blocks until an event occurs (1-second timeout loop)
            if request.wait_edge_events(timeout=1.0):
                # Clear the event from the internal OS queue buffer
                events = request.read_edge_events()
                
                print("\n⚠️ PERIMETER BREACH! Processing immediate frame capture...")
                frame = cam.capture_array()
                
                # Compress array to JPEG buffer format
                success, encoded_image = cv2.imencode('.jpg', frame)
                if success:
                    # Transcode binary arrays into base64 text strings
                    base64_string = base64.b64encode(encoded_image).decode('utf-8')
                    data_url_payload = f"data:image/jpeg;base64,{base64_string}"
                    
                    print(f"🔌 Streaming {len(data_url_payload)} string bytes over physical copper lines...")
                    ser.write(data_url_payload.encode('utf-8'))
                    ser.write(b'\n')  # Send newline character to mark end-of-string
                    ser.flush()
                    print("✅ Serial matrix offloaded successfully.")
                
                # Cooldown debounce window to prevent repeated captures from a single breach
                time.sleep(3.0)

except KeyboardInterrupt:
    print("\nDeactivating tactical hardware nodes...")
finally:
    cam.stop()
    ser.close()
