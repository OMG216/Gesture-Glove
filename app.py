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

GYRO-CURSOR MODE
  A special mode steers your laptop's mouse with the gyroscope:
    - Active only while CH1 is OFF and CH2 is ON.
    - While that mode is active AND CH4 is held, the gyroscope drives the
      cursor like a joystick (tilt = move, the reading is a speed, not a
      position).
    - While steering (CH4 held, inside the mode), tapping CH5 fires one
      left click per tap.
    - While the mode is active, holding CH3 by itself scrolls down, and
      holding CH3 + CH4 together scrolls up. This works independently of
      steering, so it doesn't require CH4 - except for the "scroll up"
      combination, which does.
  Press ESC at any time to quit the whole program immediately.

  OS PERMISSIONS THIS NEEDS (pynput moves your real mouse + reads keys):
    macOS   : System Settings -> Privacy & Security -> Accessibility, AND
              -> Input Monitoring. Add Terminal (or your Python app) to
              both, or the cursor won't move / ESC won't be seen.
    Windows : Works out of the box. Some antivirus tools flag global mouse
              control - allow it if prompted.
    Linux   : Only works on an X11 session. On Wayland (default on many
              modern distros), apps are not allowed to inject mouse/key
              events for security reasons, so this feature will silently
              do nothing - log out and pick an "Ubuntu on Xorg" / X11
              session at login if that's your situation.

  Gyro axis mapping is a guess (gyroX -> left/right, gyroY -> up/down)
  since it depends on how the sensor is physically mounted. If motion
  feels backwards or swapped, use the Invert X / Invert Y buttons on the
  dashboard (under "GYRO-CURSOR MODE") - no restart needed. Sensitivity
  is also live-adjustable there via a slider.
"""

import os
import json
import threading
import time

import serial
import serial.tools.list_ports
from flask import Flask, jsonify, render_template, request
from pynput.mouse import Controller as MouseController, Button
from pynput.keyboard import Listener as KeyboardListener, Key

# ---- CONFIG: change this for your setup ----
SERIAL_PORT = "COM3"  # <-- change this to your Arduino's port
BAUD_RATE = 115200
# ---------------------------------------------

# ---- Gyro-cursor mode config (see GYRO-CURSOR MODE above) ----
# These are just the STARTING values - while the program is running you
# can change all three live from the dashboard's "GYRO-CURSOR MODE" panel
# (sensitivity slider + Invert X / Invert Y buttons), no restart needed.
DEFAULT_SENSITIVITY = 4.0  # pixels per (deg/s) per second of tilt
SENSITIVITY_MIN = 0.5
SENSITIVITY_MAX = 20.0
DEFAULT_INVERT_X = False
DEFAULT_INVERT_Y = False
MAX_DT = 0.25  # clamp time jumps (e.g. after a stall) so the cursor can't leap
SCROLL_AMOUNT = 2  # scroll "clicks" sent per reading while CH3 is held - the
                    # Arduino sketch reports ~10x/sec by default, so this is
                    # roughly 20 clicks/sec; raise/lower to taste
# ----------------------------------------------------------------

app = Flask(__name__)

state = {
    "touch": [0, 0, 0, 0, 0],
    "gyroX": 0.0,
    "gyroY": 0.0,
    "gyroZ": 0.0,
    "connected": False,
    "last_update": None,
    "mode_active": False,    # CH1 off AND CH2 on
    "cursor_active": False,  # mode_active AND CH4 held (gyro is steering)
    "scroll_direction": "off",  # "off" | "down" (CH3) | "up" (CH3 + CH4)
}
state_lock = threading.Lock()

# Live-adjustable cursor settings - read by update_cursor_control() every
# tick, written by the /api/settings POST route when the dashboard's
# slider/buttons change. Cheap enough to lock on every read; this isn't
# a hot path (max ~10-20 calls/sec).
settings = {
    "sensitivity": DEFAULT_SENSITIVITY,
    "invert_x": DEFAULT_INVERT_X,
    "invert_y": DEFAULT_INVERT_Y,
}
settings_lock = threading.Lock()

mouse_controller = MouseController()
_cursor_state = {
    "last_time": None,  # None means "not currently steering"
    "frac_x": 0.0,       # leftover sub-pixel movement, carried between ticks
    "frac_y": 0.0,
    "prev_ch5": 0,        # previous CH5 reading, to detect a fresh tap
}


def update_cursor_control(touch, gyro_x, gyro_y):
    """Joystick-style cursor control, called once per sensor reading.
    Moves the real OS mouse cursor while the mode + CH4 conditions hold,
    fires one click per fresh CH5 tap while steering, and scrolls while
    CH3 is held. Returns (mode_active, cursor_active, scroll_direction)
    so the dashboard can show all three states."""
    with settings_lock:
        sensitivity = settings["sensitivity"]
        invert_x = settings["invert_x"]
        invert_y = settings["invert_y"]

    mode_active = (touch[0] == 0) and (touch[1] == 1)
    cursor_active = mode_active and (touch[3] == 1)

    # --- Scroll: CH3 alone scrolls down, CH3 + CH4 together scrolls up.
    # Gated only by the overall mode, not by cursor_active, so "scroll
    # down" works even when CH4 (steering) is off. pynput's scroll(dx, dy)
    # uses positive dy for up, negative dy for down. ---
    scroll_direction = "off"
    if mode_active and touch[2] == 1:
        scroll_direction = "up" if touch[3] == 1 else "down"
        try:
            mouse_controller.scroll(0, SCROLL_AMOUNT if scroll_direction == "up" else -SCROLL_AMOUNT)
        except Exception as e:
            print(f"Scroll failed (check OS permissions): {e}")

    if not cursor_active:
        # Not steering right now - reset the timer so motion doesn't jump
        # the instant steering resumes, but keep tracking CH5 so a tap
        # that happens to land exactly on the transition isn't missed.
        _cursor_state["last_time"] = None
        _cursor_state["prev_ch5"] = touch[4]
        return mode_active, cursor_active, scroll_direction

    now = time.time()
    last_time = _cursor_state["last_time"]
    dt = 0.0 if last_time is None else min(now - last_time, MAX_DT)
    _cursor_state["last_time"] = now

    if dt > 0:
        dx = gyro_x * sensitivity * dt * (-1 if invert_x else 1)
        dy = gyro_y * sensitivity * dt * (-1 if invert_y else 1)

        # Accumulate fractional pixels so slow tilts still move the cursor
        # smoothly instead of being rounded away to zero every tick.
        _cursor_state["frac_x"] += dx
        _cursor_state["frac_y"] += dy
        move_x = int(_cursor_state["frac_x"])
        move_y = int(_cursor_state["frac_y"])
        _cursor_state["frac_x"] -= move_x
        _cursor_state["frac_y"] -= move_y

        if move_x or move_y:
            try:
                mouse_controller.move(move_x, move_y)
            except Exception as e:
                print(f"Cursor move failed (check OS permissions): {e}")

    # Click once per fresh CH5 tap (rising edge), not once per reading,
    # so holding CH5 down doesn't spam dozens of clicks per second.
    if touch[4] == 1 and _cursor_state["prev_ch5"] == 0:
        try:
            mouse_controller.click(Button.left, 1)
        except Exception as e:
            print(f"Cursor click failed (check OS permissions): {e}")
    _cursor_state["prev_ch5"] = touch[4]

    return mode_active, cursor_active, scroll_direction


def on_key_press(key):
    if key == Key.esc:
        print("\nESC pressed - shutting down Sensor Panel.")
        os._exit(0)


def start_escape_listener():
    listener = KeyboardListener(on_press=on_key_press)
    listener.daemon = True
    listener.start()


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
            touch = data.get("touch", state["touch"])
            gyro_x = data.get("gyroX", state["gyroX"])
            gyro_y = data.get("gyroY", state["gyroY"])
            gyro_z = data.get("gyroZ", state["gyroZ"])

            mode_active, cursor_active, scroll_direction = False, False, "off"
            if isinstance(touch, list) and len(touch) >= 5:
                # Done outside state_lock since it can call into the OS
                # mouse driver, which we don't want blocking the dashboard.
                mode_active, cursor_active, scroll_direction = update_cursor_control(touch, gyro_x, gyro_y)

            with state_lock:
                state["touch"] = touch
                state["gyroX"] = gyro_x
                state["gyroY"] = gyro_y
                state["gyroZ"] = gyro_z
                state["mode_active"] = mode_active
                state["cursor_active"] = cursor_active
                state["scroll_direction"] = scroll_direction
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


@app.route("/api/settings", methods=["GET"])
def get_settings():
    with settings_lock:
        return jsonify(dict(settings))


@app.route("/api/settings", methods=["POST"])
def update_settings():
    """Lets the dashboard change sensitivity / invert flags live, no
    restart needed. Unknown or malformed fields are ignored rather than
    erroring, so a bad request can't crash cursor control mid-session."""
    data = request.get_json(silent=True) or {}
    with settings_lock:
        if "sensitivity" in data:
            try:
                val = float(data["sensitivity"])
                settings["sensitivity"] = max(SENSITIVITY_MIN, min(SENSITIVITY_MAX, val))
            except (TypeError, ValueError):
                pass
        if "invert_x" in data:
            settings["invert_x"] = bool(data["invert_x"])
        if "invert_y" in data:
            settings["invert_y"] = bool(data["invert_y"])
        result = dict(settings)
    return jsonify(result)


if __name__ == "__main__":
    threading.Thread(target=serial_worker, daemon=True).start()
    start_escape_listener()
    print("Dashboard running. On your phone, open: http://<this-laptop-ip>:5000")
    print("Press ESC at any time (with this window focused, or globally on Windows/macOS with permissions) to quit.")
    app.run(host="0.0.0.0", port=5000, debug=False)