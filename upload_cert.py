#!/usr/bin/env python3
import sys, time, serial

PORT = "/dev/serial/by-id/usb-SIMCom_Wireless_Solution_A76XX_Series_LTE_Module_200806006809080000-if04-port0"
BAUD = 115200
CERT_NAME = "firebase_ca.pem"

def read_until(ser, tokens, timeout=5):
    if isinstance(tokens, str): tokens = [tokens]
    buf = ""; start = time.time()
    while (time.time() - start) < timeout:
        chunk = ser.read(256).decode("utf-8", errors="ignore")
        if chunk:
            buf += chunk; print(chunk, end="", flush=True)
            if any(t in buf for t in tokens): return buf
    return buf

def send(ser, cmd, wait=("OK","ERROR"), timeout=5):
    print(f"\n>> {cmd}")
    ser.write((cmd+"\r\n").encode()); ser.flush()
    return read_until(ser, list(wait), timeout)

def main():
    if len(sys.argv) < 2:
        print("Usage: sudo python3 upload_cert.py /path/to/cert.pem"); sys.exit(1)
    with open(sys.argv[1],"rb") as f: cert = f.read()
    size = len(cert); print(f"Cert: {sys.argv[1]} ({size} bytes)")
    ser = serial.Serial(PORT, BAUD, timeout=1); time.sleep(0.5)
    if "OK" not in send(ser,"AT"):
        print("\n[!] Modem not responding. Check port / nothing else holding it."); ser.close(); sys.exit(1)
    send(ser, f'AT+CCERTDELE="{CERT_NAME}"')
    print("\n>> AT+CCERTDOWN ...")
    ser.write(f'AT+CCERTDOWN="{CERT_NAME}",{size}\r\n'.encode()); ser.flush()
    read_until(ser, [">","DOWNLOAD"], timeout=5)
    ser.write(cert); ser.flush()
    read_until(ser, ["OK","ERROR"], timeout=10)
    send(ser, "AT+CCERTLIST", timeout=5)
    print("\n\nDone. If you saw OK and the cert in CCERTLIST, it worked.")
    ser.close()

main()
