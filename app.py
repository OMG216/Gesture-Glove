"""
Sensor Panel - Flask backend
Reads touch + gyroscope data from an Arduino over USB serial and serves a
live dashboard on your local network so your phone can view it.

SETUP
  1. pip install -r requirements.txt
  2. Upload arduino_sketch/arduino_sketch.ino to your Arduino first
  3. Set SERIAL_PORT below to match your Arduino (see instructions below)
  4. Run: python app.py
  5. On your phone (same Wi-Fi as the laptop), open http://<laptop-ip>:5000

FINDING YOUR LAPTOP'S IP (to type into your phone's browser)
  Windows : Command Prompt -> ipconfig -> "IPv4 Address"
  Mac     : Terminal -> ipconfig getifaddr en0
  Linux   : Terminal -> hostname -I

FINDING YOUR SERIAL PORT (for SERIAL_PORT below)
  Windows : Device Manager -> Ports (COM & LPT) -> e.g. "COM3"
  Mac     : Terminal -> ls /dev/cu.*    -> e.g. "/dev/cu.usbmodem14101"
  Linux   : Terminal -> ls /dev/tty*    -> e.g. "/dev/ttyACM0" or "/dev/ttyUSB0"
  If SERIAL_PORT is wrong, this script prints the available ports for you
  when it can't connect.

TROUBLESHOOTING
  - Phone can't reach the page: make sure phone and laptop are on the same
    Wi-Fi, and check your laptop's firewall allows Python / port 5000.
  - "NO LINK" never goes away: double check SERIAL_PORT and that the
    Arduino sketch has been uploaded and is running.
"""

import json
import threading
import time

import serial
import serial.tools.list_ports
from flask import Flask, jsonify, render_template

# ---- CONFIG: change this for your setup ----
SERIAL_PORT = "COM3"  # <-- change this to your Arduino's port
BAUD_RATE = 115200
# ---------------------------------------------

app = Flask(__name__)

state = {
    "touch": [0, 0, 0, 0, 0],
    "gyroX": 0.0,
    "gyroY": 0.0,
    "gyroZ": 0.0,
    "connected": False,
    "last_update": None,
}
state_lock = threading.Lock()


def list_available_ports():
    ports = list(serial.tools.list_ports.comports())
    if not ports:
        print("  No serial ports found. Is the Arduino plugged in?")
    for p in ports:
        print(f"  {p.device}  ({p.description})")


def serial_worker():
    """Runs forever in a background thread: connects to the Arduino, reads
    JSON lines, and keeps `state` updated. Reconnects automatically if the
    cable is unplugged or the Arduino resets."""
    ser = None
    while True:
        if ser is None:
            try:
                ser = serial.Serial(SERIAL_PORT, BAUD_RATE, timeout=1)
                time.sleep(2)  # let the Arduino finish resetting
                print(f"Connected to {SERIAL_PORT}")
            except serial.SerialException:
                print(f"Could not open {SERIAL_PORT}. Available ports:")
                list_available_ports()
                with state_lock:
                    state["connected"] = False
                time.sleep(3)
                continue

        try:
            raw = ser.readline().decode("utf-8", errors="ignore").strip()
            if not raw:
                continue
            data = json.loads(raw)
            with state_lock:
                state["touch"] = data.get("touch", state["touch"])
                state["gyroX"] = data.get("gyroX", state["gyroX"])
                state["gyroY"] = data.get("gyroY", state["gyroY"])
                state["gyroZ"] = data.get("gyroZ", state["gyroZ"])
                state["connected"] = True
                state["last_update"] = time.time()
        except json.JSONDecodeError:
            continue  # caught a partial line mid-write, just skip it
        except (serial.SerialException, OSError):
            print("Lost connection to Arduino. Retrying...")
            ser = None
            with state_lock:
                state["connected"] = False
            time.sleep(2)


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/sensors")
def api_sensors():
    with state_lock:
        return jsonify(state)


if __name__ == "__main__":
    threading.Thread(target=serial_worker, daemon=True).start()
    print("Dashboard running. On your phone, open: http://<this-laptop-ip>:5000")
    app.run(host="0.0.0.0", port=5000, debug=False)
