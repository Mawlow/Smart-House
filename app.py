import os
import pickle
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
import json
import base64
from urllib import error as url_error
from urllib import request as url_request

import cv2
import numpy as np
from flask import Flask, Response, jsonify, redirect, render_template, request, session, url_for
try:
    import face_recognition
    FACE_LIB_AVAILABLE = True
    FACE_LIB_ERROR = ""
except Exception as exc:  # pragma: no cover
    face_recognition = None
    FACE_LIB_AVAILABLE = False
    FACE_LIB_ERROR = str(exc)

try:
    import speech_recognition as sr
    VOICE_LIB_AVAILABLE = True
    VOICE_LIB_ERROR = ""
except Exception as exc:  # pragma: no cover
    sr = None
    VOICE_LIB_AVAILABLE = False
    VOICE_LIB_ERROR = str(exc)


app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "smart-home-door-admin-secret")

BASE_DIR = Path(__file__).resolve().parent
DATASET_DIR = BASE_DIR / "dataset"
ALERTS_DIR = BASE_DIR / "static" / "alerts"
ENCODINGS_FILE = BASE_DIR / "encodings.pkl"
LBPH_MODEL_FILE = BASE_DIR / "lbph_model.yml"
LBPH_LABELS_FILE = BASE_DIR / "lbph_labels.pkl"
NOTIFICATIONS_FILE = BASE_DIR / "notifications.pkl"
ESP32_SETTINGS_FILE = BASE_DIR / "esp32_settings.json"
CAMERA_SETTINGS_FILE = BASE_DIR / "camera_settings.json"
FINGERPRINT_USERS_FILE = BASE_DIR / "fingerprint_users.json"
FP_ENROLL_ESP32_TIMEOUT_SEC = 120.0
FP_MAX_TEMPLATE_SLOTS = 162
DATASET_DIR.mkdir(exist_ok=True)
ALERTS_DIR.mkdir(parents=True, exist_ok=True)

# Shared camera and lock keep webcam access simple for this module.
camera_lock = threading.Lock()
camera = None

last_status = {
    "message": "System ready",
    "access": "IDLE",
    "command": "None",
}
notifications = []
control_state = {
    "light": "OFF",
    "light_2": "OFF",
    "door": "CLOSED",
    "room_door": "CLOSED",
    "exit_door": "CLOSED",
}

# Substring match on transcript (Google or browser). Sorted longest-first so e.g.
# "turn on light" does not win inside "turn on light two".
_VOICE_PHRASES_RAW = (
    ("close all the doors", "CLOSE ALL DOORS", "close_all_doors"),
    ("close all doors", "CLOSE ALL DOORS", "close_all_doors"),
    ("lock all the doors", "CLOSE ALL DOORS", "lock_all_doors"),
    ("lock all doors", "CLOSE ALL DOORS", "lock_all_doors"),
    ("open all the doors", "OPEN ALL DOORS", "open_all_doors"),
    ("open all doors", "OPEN ALL DOORS", "open_all_doors"),
    ("open the entrance door", "OPEN ENTRANCE DOOR", "open entrance door"),
    ("open entrance door", "OPEN ENTRANCE DOOR", "open entrance door"),
    ("unlock the room door", "OPEN ROOM DOOR", "open room door"),
    ("open the room door", "OPEN ROOM DOOR", "open room door"),
    ("open room door", "OPEN ROOM DOOR", "open room door"),
    ("unlock room door", "OPEN ROOM DOOR", "open room door"),
    ("open the room", "OPEN ROOM DOOR", "open room door"),
    ("open the exit door", "OPEN EXIT DOOR", "open exit door"),
    ("open exit door", "OPEN EXIT DOOR", "open exit door"),
    ("close entrance door", "CLOSE ENTRANCE DOOR", "close entrance door"),
    ("close the exit door", "CLOSE EXIT DOOR", "close exit door"),
    ("close exit door", "CLOSE EXIT DOOR", "close exit door"),
    ("close room door", "CLOSE ROOM DOOR", "close room door"),
    ("turn off the second light", "TURN OFF LIGHT 2", "turn off light 2"),
    ("turn on the second light", "TURN ON LIGHT 2", "turn on light 2"),
    ("turn off light two", "TURN OFF LIGHT 2", "turn off light 2"),
    ("turn on light two", "TURN ON LIGHT 2", "turn on light 2"),
    ("turn off light 2", "TURN OFF LIGHT 2", "turn off light 2"),
    ("turn on light 2", "TURN ON LIGHT 2", "turn on light 2"),
    ("second light off", "TURN OFF LIGHT 2", "turn off light 2"),
    ("second light on", "TURN ON LIGHT 2", "turn on light 2"),
    ("switch off the light", "TURN OFF LIGHT", "turn off light"),
    ("turn off the light", "TURN OFF LIGHT", "turn off light"),
    ("turn off light", "TURN OFF LIGHT", "turn off light"),
    ("open room", "OPEN ROOM DOOR", "open room door"),
    ("turn on light", "TURN ON LIGHT", "turn on light"),
    ("close door", "CLOSE DOOR", "close door"),
    ("lights off", "TURN OFF LIGHT", "turn off light"),
    ("lights on", "TURN ON LIGHT", "turn on light"),
    ("open light", "TURN ON LIGHT", "turn on light"),
    ("open door", "OPEN DOOR", "open door"),
)
VOICE_PHRASES = tuple(sorted(_VOICE_PHRASES_RAW, key=lambda t: len(t[0]), reverse=True))


def apply_voice_command_text(raw_text):
    """Match transcript and update state + ESP32. Returns (response_dict, http_status)."""
    text = (raw_text or "").lower().strip()
    if not text:
        last_status["message"] = "No speech text provided."
        return {"ok": False, "message": last_status["message"]}, 400

    detected = None
    esp32_command = None
    for phrase, label, esp_cmd in VOICE_PHRASES:
        if phrase in text:
            detected = label
            esp32_command = esp_cmd
            break

    if detected and esp32_command:
        last_status["command"] = detected
        last_status["message"] = f"Command Detected: {detected}"
        if esp32_command == "turn on light":
            control_state["light"] = "ON"
        elif esp32_command == "turn off light":
            control_state["light"] = "OFF"
        elif esp32_command == "turn on light 2":
            control_state["light_2"] = "ON"
        elif esp32_command == "turn off light 2":
            control_state["light_2"] = "OFF"
        elif esp32_command in ("open door", "open entrance door"):
            control_state["door"] = "OPEN"
        elif esp32_command == "open room door":
            control_state["room_door"] = "OPEN"
        elif esp32_command == "open exit door":
            control_state["exit_door"] = "OPEN"
        elif esp32_command == "open_all_doors":
            control_state["door"] = "OPEN"
            control_state["room_door"] = "OPEN"
            control_state["exit_door"] = "OPEN"
        elif esp32_command in ("close_all_doors", "lock_all_doors"):
            control_state["door"] = "CLOSED"
            control_state["room_door"] = "CLOSED"
            control_state["exit_door"] = "CLOSED"
        elif esp32_command == "close entrance door":
            control_state["door"] = "CLOSED"
        elif esp32_command == "close room door":
            control_state["room_door"] = "CLOSED"
        elif esp32_command == "close exit door":
            control_state["exit_door"] = "CLOSED"
        elif esp32_command == "close door":
            control_state["door"] = "CLOSED"
        call_esp32("/api/control", {"command": esp32_command})
        return {"ok": True, "message": last_status["message"], "command": detected}, 200

    last_status["command"] = "UNKNOWN"
    last_status["message"] = "No valid command detected."
    return (
        {
            "ok": False,
            "message": last_status["message"],
            "command": "UNKNOWN",
            "heard": text,
        },
        200,
    )


ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")


def load_known_faces():
    if ENCODINGS_FILE.exists():
        with open(ENCODINGS_FILE, "rb") as file:
            data = pickle.load(file)
            return data.get("encodings", []), data.get("names", [])
    return [], []


def save_known_faces(encodings, names):
    with open(ENCODINGS_FILE, "wb") as file:
        pickle.dump({"encodings": encodings, "names": names}, file)


known_face_encodings, known_face_names = load_known_faces()

face_detector = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
LBPH_AVAILABLE = hasattr(cv2, "face") and hasattr(cv2.face, "LBPHFaceRecognizer_create")
lbph_recognizer = cv2.face.LBPHFaceRecognizer_create() if LBPH_AVAILABLE else None
lbph_label_to_name = {}
LBPH_CONFIDENCE_STRICT = 38.0
LBPH_REQUIRED_HITS = 6
ACCESS_GRANTED_DISPLAY_SECONDS = 3.0
last_granted_at = 0.0


def load_camera_settings():
    idx = int(os.getenv("SMARTHOUSE_CAMERA_INDEX", "0"))
    if CAMERA_SETTINGS_FILE.exists():
        try:
            with open(CAMERA_SETTINGS_FILE, "r", encoding="utf-8") as file:
                data = json.load(file)
            idx = int(data.get("camera_index", idx))
        except (OSError, ValueError, TypeError, KeyError):
            pass
    return {"camera_index": max(0, min(idx, 10))}


def save_camera_settings():
    with open(CAMERA_SETTINGS_FILE, "w", encoding="utf-8") as file:
        json.dump(camera_settings, file, indent=2)


camera_settings = load_camera_settings()


def open_capture(index):
    idx = int(index)
    backend_order = [None]
    v4l2 = getattr(cv2, "CAP_V4L2", None)
    gstreamer = getattr(cv2, "CAP_GSTREAMER", None)
    dshow = getattr(cv2, "CAP_DSHOW", None)

    if os.name == "nt":
        if dshow is not None:
            backend_order.append(dshow)
    else:
        if v4l2 is not None:
            backend_order.append(v4l2)
        if gstreamer is not None:
            backend_order.append(gstreamer)

    last_error = "Could not open camera index {}.".format(idx)
    for backend in backend_order:
        cap = cv2.VideoCapture(idx) if backend is None else cv2.VideoCapture(idx, backend)
        if cap is not None and cap.isOpened():
            # Best-effort defaults for small USB cams.
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            return cap, None
        try:
            cap.release()
        except Exception:
            pass

    return None, last_error


def reinit_camera(new_index=None):
    global camera
    with camera_lock:
        if camera is not None:
            try:
                camera.release()
            except Exception:
                pass
            camera = None
        idx = int(new_index if new_index is not None else camera_settings["camera_index"])
        camera_settings["camera_index"] = max(0, min(idx, 10))
        camera, err = open_capture(camera_settings["camera_index"])
        opened = camera is not None and camera.isOpened()
        return opened, err


def _normalize_notification(note):
    """Ensure id, kind, acknowledged for UI + acknowledge API (backward compatible with old pickles)."""
    if not isinstance(note, dict):
        return {
            "id": str(uuid.uuid4()),
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "message": str(note),
            "image": "",
            "kind": "unauthorized",
            "acknowledged": False,
        }
    out = dict(note)
    if not out.get("id"):
        out["id"] = str(uuid.uuid4())
    if not out.get("kind"):
        msg = out.get("message", "")
        if "Gas/smoke emergency" in msg:
            out["kind"] = "emergency"
        elif "HC-SR501" in msg or "motion detected" in msg.lower():
            out["kind"] = "motion"
        else:
            out["kind"] = "unauthorized"
    if out.get("kind") == "motion":
        out["acknowledged"] = True
    else:
        out["acknowledged"] = bool(out.get("acknowledged"))
    return out


def load_notifications():
    if NOTIFICATIONS_FILE.exists():
        try:
            with open(NOTIFICATIONS_FILE, "rb") as file:
                raw = pickle.load(file)
        except (OSError, pickle.UnpicklingError, EOFError):
            return []
        if not isinstance(raw, list):
            return []
        return [_normalize_notification(n) for n in raw]
    return []


def save_notifications():
    with open(NOTIFICATIONS_FILE, "wb") as file:
        pickle.dump(notifications, file)


notifications = load_notifications()
try:
    save_notifications()
except OSError:
    pass
notifications_lock = threading.Lock()
last_esp32_motion_high = False
last_esp32_emergency_high = False


def load_esp32_settings():
    defaults = {
        "base_url": "",
        "api_key": "smarthouse-key",
        "timeout_sec": 2.0,
        "motion_poll_sec": 0.65,
    }
    if ESP32_SETTINGS_FILE.exists():
        try:
            with open(ESP32_SETTINGS_FILE, "r", encoding="utf-8") as file:
                data = json.load(file)
            if isinstance(data, dict):
                return {**defaults, **data}
        except (OSError, json.JSONDecodeError, TypeError):
            pass
    return defaults.copy()


def save_esp32_settings():
    with open(ESP32_SETTINGS_FILE, "w", encoding="utf-8") as file:
        json.dump(esp32_settings, file, indent=2)


esp32_settings = load_esp32_settings()


def normalize_esp32_base_url(url):
    url = str(url or "").strip().rstrip("/")
    if not url:
        return ""
    if not url.lower().startswith(("http://", "https://")):
        url = "http://" + url
    return url


# Map spoken / spaced text to the exact command string the ESP32 expects (underscore form).
ESP32_CONTROL_ALIASES = {
    "close all doors": "close_all_doors",
    "close all the doors": "close_all_doors",
    "lock all doors": "lock_all_doors",
    "open all doors": "open_all_doors",
    "open all the doors": "open_all_doors",
    "open the room door": "open room door",
    "open the exit door": "open exit door",
    "open the entrance door": "open entrance door",
}

# API / tools sometimes send snake_case; ESP32 expects space-separated phrases for these.
ESP32_UNDERSCORE_TO_COMMAND = {
    "close_exit_door": "close exit door",
    "open_exit_door": "open exit door",
    "close_room_door": "close room door",
    "open_room_door": "open room door",
    "close_entrance_door": "close entrance door",
    "open_entrance_door": "open entrance door",
    "turn_on_light_2": "turn on light 2",
    "turn_off_light_2": "turn off light 2",
}


def normalize_esp32_control_command(raw):
    s = str(raw or "").strip().lower()
    s = " ".join(s.split())
    s = ESP32_UNDERSCORE_TO_COMMAND.get(s, s)
    s = ESP32_CONTROL_ALIASES.get(s, s)
    # Speech / UI sometimes sends "two" instead of "2"; ESP32 expects digit form.
    if s == "turn on light two":
        s = "turn on light 2"
    elif s == "turn off light two":
        s = "turn off light 2"
    return s


def load_fingerprint_users():
    if not FINGERPRINT_USERS_FILE.exists():
        return {}
    try:
        with open(FINGERPRINT_USERS_FILE, "r", encoding="utf-8") as file:
            data = json.load(file)
        return {str(k): int(v) for k, v in data.items() if str(k).strip()}
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return {}


def save_fingerprint_users(mapping):
    with open(FINGERPRINT_USERS_FILE, "w", encoding="utf-8") as file:
        json.dump(mapping, file, indent=2)


def next_free_fingerprint_slot(mapping):
    used = {int(v) for v in mapping.values()}
    for i in range(FP_MAX_TEMPLATE_SLOTS):
        if i not in used:
            return i
    return None


def call_esp32(path, payload=None, timeout_sec=None):
    base_url = normalize_esp32_base_url(esp32_settings.get("base_url", ""))
    if not base_url:
        return False, "ESP32 base URL is not configured."

    if timeout_sec is not None:
        timeout = float(timeout_sec)
    else:
        timeout = float(esp32_settings.get("timeout_sec", 2.0))
    url = f"{base_url}{path}"
    headers = {
        "Content-Type": "application/json",
        "X-API-Key": esp32_settings.get("api_key", ""),
    }
    if payload is not None and path == "/api/control" and isinstance(payload, dict) and "command" in payload:
        payload = dict(payload)
        payload["command"] = normalize_esp32_control_command(payload["command"])
    body = None if payload is None else json.dumps(payload).encode("utf-8")

    req = url_request.Request(url, data=body, headers=headers, method="GET" if body is None else "POST")
    try:
        with url_request.urlopen(req, timeout=timeout) as response:
            response_data = response.read().decode("utf-8").strip()
            return True, response_data or "OK"
    except url_error.HTTPError as exc:
        try:
            err_body = exc.read().decode("utf-8", errors="ignore").strip()
        except Exception:
            err_body = ""
        return False, err_body or f"ESP32 HTTP {exc.code}: {exc.reason}"
    except url_error.URLError as exc:
        return False, f"ESP32 connection error: {exc}"
    except Exception as exc:
        return False, f"ESP32 request failed: {exc}"


def detect_first_face(frame):
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    faces = face_detector.detectMultiScale(gray, scaleFactor=1.2, minNeighbors=5, minSize=(70, 70))
    if len(faces) == 0:
        return None, None
    x, y, w, h = faces[0]
    crop_gray = gray[y:y + h, x:x + w]
    return (x, y, w, h), crop_gray


def load_lbph_state():
    global lbph_label_to_name
    if not LBPH_AVAILABLE:
        return
    if LBPH_MODEL_FILE.exists() and LBPH_LABELS_FILE.exists():
        lbph_recognizer.read(str(LBPH_MODEL_FILE))
        with open(LBPH_LABELS_FILE, "rb") as file:
            lbph_label_to_name = pickle.load(file)


def train_lbph_from_dataset():
    global lbph_label_to_name
    if not LBPH_AVAILABLE:
        return False

    images = []
    labels = []
    label_map = {}
    label_id = 0

    for user_dir in sorted(DATASET_DIR.iterdir()):
        if not user_dir.is_dir():
            continue
        username = user_dir.name
        if username not in label_map:
            label_map[username] = label_id
            label_id += 1

        for image_path in user_dir.glob("*.jpg"):
            image = cv2.imread(str(image_path), cv2.IMREAD_GRAYSCALE)
            if image is None:
                continue
            image = cv2.resize(image, (200, 200))
            images.append(image)
            labels.append(label_map[username])

    if not images:
        return False

    lbph_recognizer.train(images, np.array(labels))
    lbph_recognizer.save(str(LBPH_MODEL_FILE))
    lbph_label_to_name = {v: k for k, v in label_map.items()}
    with open(LBPH_LABELS_FILE, "wb") as file:
        pickle.dump(lbph_label_to_name, file)
    return True


load_lbph_state()

_cam_ok, _cam_err = reinit_camera()
if not _cam_ok:
    last_status["message"] = _cam_err or "Camera not available. Set camera index in Admin or SMARTHOUSE_CAMERA_INDEX."


def safe_read_frame():
    with camera_lock:
        if camera is None or not camera.isOpened():
            return None
        ok, frame = camera.read()
    if not ok:
        return None
    return frame


def reset_temporary_access_status():
    if (
        last_status["access"] == "ACCESS GRANTED"
        and (time.time() - last_granted_at) >= ACCESS_GRANTED_DISPLAY_SECONDS
    ):
        last_status["message"] = "System ready"
        last_status["access"] = "IDLE"


def annotate_frame(frame):
    reset_temporary_access_status()
    text_1 = f"Status: {last_status['access']}"
    text_2 = f"Message: {last_status['message']}"
    text_3 = f"Command: {last_status['command']}"

    cv2.putText(frame, text_1, (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 255), 2)
    cv2.putText(frame, text_2, (10, 55), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (0, 255, 0), 2)
    cv2.putText(frame, text_3, (10, 85), cv2.FONT_HERSHEY_SIMPLEX, 0.65, (255, 255, 0), 2)
    return frame


def add_unauthorized_notification(frame):
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    image_name = f"unauthorized_{timestamp}.jpg"
    image_path = ALERTS_DIR / image_name
    cv2.imwrite(str(image_path), frame)
    note = {
        "id": str(uuid.uuid4()),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "message": "Someone is entering your house.",
        "image": f"/static/alerts/{image_name}",
        "kind": "unauthorized",
        "acknowledged": False,
    }
    with notifications_lock:
        notifications.insert(0, note)
        if len(notifications) > 40:
            notifications.pop()
        save_notifications()


def add_security_motion_notification():
    note = {
        "id": str(uuid.uuid4()),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "message": "Front entrance motion detected (HC-SR501 security sensor).",
        "image": "",
        "kind": "motion",
        "acknowledged": True,
    }
    with notifications_lock:
        notifications.insert(0, note)
        if len(notifications) > 40:
            notifications.pop()
        save_notifications()


def add_gas_smoke_emergency_notification(gas, smoke):
    note = {
        "id": str(uuid.uuid4()),
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "message": (
            "Gas/smoke emergency: ESP32 opened all doors (gas ADC={}, smoke ADC={}). "
            "Check sensors and ventilation."
        ).format(gas, smoke),
        "image": "",
        "kind": "emergency",
        "acknowledged": False,
    }
    with notifications_lock:
        notifications.insert(0, note)
        if len(notifications) > 40:
            notifications.pop()
        save_notifications()


def decode_data_url_to_frame(data_url):
    if not data_url or "," not in data_url:
        return None
    try:
        encoded = data_url.split(",", 1)[1]
        raw = base64.b64decode(encoded)
        arr = np.frombuffer(raw, dtype=np.uint8)
        frame = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        return frame
    except Exception:
        return None


def generate_video_stream():
    while True:
        frame = safe_read_frame()
        if frame is None:
            continue

        frame = annotate_frame(frame)
        ok, buffer = cv2.imencode(".jpg", frame)
        if not ok:
            continue

        yield (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n\r\n" + buffer.tobytes() + b"\r\n"
        )


@app.route("/")
def index():
    return redirect(url_for("door_portal"))


@app.route("/door")
def door_portal():
    return render_template("door.html")


@app.route("/admin-login")
def admin_login_page():
    return render_template("admin_login.html")


@app.route("/admin")
def admin_portal():
    if not session.get("is_admin"):
        return redirect(url_for("admin_login_page"))
    return render_template("admin.html")


@app.route("/admin/login", methods=["POST"])
def admin_login():
    username = (request.json or {}).get("username", "").strip()
    password = (request.json or {}).get("password", "").strip()

    if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
        session["is_admin"] = True
        return jsonify({"ok": True, "message": "Login successful."})

    return jsonify({"ok": False, "message": "Invalid admin credentials."}), 401


@app.route("/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("is_admin", None)
    return jsonify({"ok": True, "message": "Logged out."})


@app.route("/video_feed")
def video_feed():
    return Response(
        generate_video_stream(),
        mimetype="multipart/x-mixed-replace; boundary=frame",
    )


@app.route("/register_face", methods=["POST"])
def register_face():
    global known_face_encodings, known_face_names

    username = (request.json or {}).get("username", "").strip()
    if not username:
        return jsonify({"ok": False, "message": "Please enter a username."}), 400

    user_dir = DATASET_DIR / username
    user_dir.mkdir(parents=True, exist_ok=True)

    collected = 0
    new_encodings = []
    max_samples = 15
    max_seconds = 30
    start = time.time()

    while collected < max_samples and time.time() - start < max_seconds:
        frame = safe_read_frame()
        if frame is None:
            continue

        if FACE_LIB_AVAILABLE:
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            locations = face_recognition.face_locations(rgb_frame)
            if not locations:
                continue

            encodings = face_recognition.face_encodings(rgb_frame, locations)
            if not encodings:
                continue

            top, right, bottom, left = locations[0]
            face_crop = frame[top:bottom, left:right]
            if face_crop.size == 0:
                continue

            file_name = user_dir / f"{username}_{collected + 1}.jpg"
            cv2.imwrite(str(file_name), face_crop)
            new_encodings.append(encodings[0])
        else:
            bbox, gray_face = detect_first_face(frame)
            if bbox is None or gray_face is None:
                continue
            resized = cv2.resize(gray_face, (200, 200))
            file_name = user_dir / f"{username}_{collected + 1}.jpg"
            cv2.imwrite(str(file_name), resized)

        collected += 1
        time.sleep(0.15)

    if FACE_LIB_AVAILABLE and not new_encodings:
        last_status["message"] = "No face captured. Try better lighting."
        last_status["access"] = "ACCESS DENIED"
        return jsonify({"ok": False, "message": last_status["message"]}), 400

    if FACE_LIB_AVAILABLE:
        known_face_encodings.extend(new_encodings)
        known_face_names.extend([username] * len(new_encodings))
        save_known_faces(known_face_encodings, known_face_names)
    else:
        if collected == 0:
            last_status["message"] = "No face captured. Try better lighting."
            last_status["access"] = "ACCESS DENIED"
            return jsonify({"ok": False, "message": last_status["message"]}), 400
        if not train_lbph_from_dataset():
            last_status["message"] = "OpenCV fallback unavailable. Install opencv-contrib-python."
            last_status["access"] = "ACCESS DENIED"
            return jsonify({"ok": False, "message": last_status["message"]}), 503

    last_status["message"] = f"Registered {username} with {collected} samples"
    last_status["access"] = "REGISTERED"
    return jsonify({"ok": True, "message": last_status["message"]})


@app.route("/register_face_frame", methods=["POST"])
def register_face_frame():
    global known_face_encodings, known_face_names
    payload = request.json or {}
    username = str(payload.get("username", "")).strip()
    data_url = payload.get("image", "")
    if not username:
        return jsonify({"ok": False, "message": "Username is required."}), 400

    frame = decode_data_url_to_frame(data_url)
    if frame is None:
        return jsonify({"ok": False, "message": "Invalid camera frame."}), 400

    user_dir = DATASET_DIR / username
    user_dir.mkdir(parents=True, exist_ok=True)
    sample_idx = len(list(user_dir.glob("*.jpg"))) + 1

    if FACE_LIB_AVAILABLE:
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        locations = face_recognition.face_locations(rgb_frame)
        if not locations:
            return jsonify({"ok": False, "message": "No face detected in frame."}), 400
        encodings = face_recognition.face_encodings(rgb_frame, locations)
        if not encodings:
            return jsonify({"ok": False, "message": "Could not encode face."}), 400
        top, right, bottom, left = locations[0]
        face_crop = frame[top:bottom, left:right]
        if face_crop.size == 0:
            return jsonify({"ok": False, "message": "Invalid face crop."}), 400
        cv2.imwrite(str(user_dir / f"{username}_{sample_idx}.jpg"), face_crop)
        known_face_encodings.append(encodings[0])
        known_face_names.append(username)
        save_known_faces(known_face_encodings, known_face_names)
    else:
        bbox, gray_face = detect_first_face(frame)
        if bbox is None or gray_face is None:
            return jsonify({"ok": False, "message": "No face detected in frame."}), 400
        resized = cv2.resize(gray_face, (200, 200))
        cv2.imwrite(str(user_dir / f"{username}_{sample_idx}.jpg"), resized)
        train_lbph_from_dataset()

    last_status["message"] = f"Captured sample {sample_idx} for {username}"
    last_status["access"] = "REGISTERED"
    return jsonify({"ok": True, "message": last_status["message"]})


@app.route("/start_recognition", methods=["POST"])
def start_recognition():
    global last_granted_at
    reset_temporary_access_status()
    last_status["message"] = "Scanning face..."
    last_status["access"] = "SCANNING"

    if FACE_LIB_AVAILABLE and not known_face_encodings:
        last_status["message"] = "No registered users. Register face first."
        last_status["access"] = "ACCESS DENIED"
        return jsonify({"ok": False, "message": last_status["message"]}), 400
    if not FACE_LIB_AVAILABLE and not lbph_label_to_name:
        if not train_lbph_from_dataset():
            last_status["message"] = "No registered users. Register face first."
            last_status["access"] = "ACCESS DENIED"
            return jsonify({"ok": False, "message": last_status["message"]}), 400

    timeout_seconds = 12
    start = time.time()
    lbph_hits = {}
    last_frame = None

    while time.time() - start < timeout_seconds:
        frame = safe_read_frame()
        if frame is None:
            continue
        last_frame = frame.copy()

        if FACE_LIB_AVAILABLE:
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            locations = face_recognition.face_locations(rgb_frame)
            encodings = face_recognition.face_encodings(rgb_frame, locations)

            for encoding in encodings:
                matches = face_recognition.compare_faces(known_face_encodings, encoding, tolerance=0.38)
                distances = face_recognition.face_distance(known_face_encodings, encoding)

                if len(distances) == 0:
                    continue

                best_idx = int(np.argmin(distances))
                if matches[best_idx]:
                    username = known_face_names[best_idx]
                    last_status["message"] = f"Welcome {username}"
                    last_status["access"] = "ACCESS GRANTED"
                    last_granted_at = time.time()
                    call_esp32("/api/control", {"command": "grant_access"})
                    return jsonify(
                        {
                            "ok": True,
                            "message": last_status["message"],
                            "status": last_status["access"],
                        }
                    )
        else:
            if not LBPH_AVAILABLE:
                last_status["message"] = "OpenCV fallback unavailable. Install opencv-contrib-python."
                last_status["access"] = "ACCESS DENIED"
                return jsonify({"ok": False, "message": last_status["message"]}), 503
            bbox, gray_face = detect_first_face(frame)
            if bbox is None or gray_face is None:
                continue
            face_200 = cv2.resize(gray_face, (200, 200))
            label, confidence = lbph_recognizer.predict(face_200)
            if label in lbph_label_to_name and confidence <= LBPH_CONFIDENCE_STRICT:
                username = lbph_label_to_name[label]
                lbph_hits[username] = lbph_hits.get(username, 0) + 1
                if lbph_hits[username] >= LBPH_REQUIRED_HITS:
                    last_status["message"] = f"Welcome {username}"
                    last_status["access"] = "ACCESS GRANTED"
                    last_granted_at = time.time()
                    call_esp32("/api/control", {"command": "grant_access"})
                    return jsonify({"ok": True, "message": last_status["message"], "status": last_status["access"]})

    last_status["message"] = "Unauthorized Person Detected"
    last_status["access"] = "ACCESS DENIED"
    if last_frame is not None:
        add_unauthorized_notification(last_frame)
    return jsonify(
        {
            "ok": False,
            "message": last_status["message"],
            "status": last_status["access"],
        }
    )


@app.route("/start_recognition_frame", methods=["POST"])
def start_recognition_frame():
    global last_granted_at
    payload = request.json or {}
    frame = decode_data_url_to_frame(payload.get("image", ""))
    if frame is None:
        return jsonify({"ok": False, "message": "Invalid camera frame."}), 400

    if FACE_LIB_AVAILABLE and not known_face_encodings:
        last_status["message"] = "No registered users. Register face first."
        last_status["access"] = "ACCESS DENIED"
        return jsonify({"ok": False, "message": last_status["message"]}), 400
    if not FACE_LIB_AVAILABLE and not lbph_label_to_name and not train_lbph_from_dataset():
        last_status["message"] = "No registered users. Register face first."
        last_status["access"] = "ACCESS DENIED"
        return jsonify({"ok": False, "message": last_status["message"]}), 400

    if FACE_LIB_AVAILABLE:
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        locations = face_recognition.face_locations(rgb_frame)
        encodings = face_recognition.face_encodings(rgb_frame, locations)
        for encoding in encodings:
            matches = face_recognition.compare_faces(known_face_encodings, encoding, tolerance=0.38)
            distances = face_recognition.face_distance(known_face_encodings, encoding)
            if len(distances) == 0:
                continue
            best_idx = int(np.argmin(distances))
            if matches[best_idx]:
                username = known_face_names[best_idx]
                last_status["message"] = f"Welcome {username}"
                last_status["access"] = "ACCESS GRANTED"
                last_granted_at = time.time()
                call_esp32("/api/control", {"command": "grant_access"})
                return jsonify({"ok": True, "message": last_status["message"], "status": last_status["access"]})
    else:
        bbox, gray_face = detect_first_face(frame)
        if bbox is not None and gray_face is not None and LBPH_AVAILABLE:
            face_200 = cv2.resize(gray_face, (200, 200))
            label, confidence = lbph_recognizer.predict(face_200)
            if label in lbph_label_to_name and confidence <= LBPH_CONFIDENCE_STRICT:
                username = lbph_label_to_name[label]
                last_status["message"] = f"Welcome {username}"
                last_status["access"] = "ACCESS GRANTED"
                last_granted_at = time.time()
                call_esp32("/api/control", {"command": "grant_access"})
                return jsonify({"ok": True, "message": last_status["message"], "status": last_status["access"]})

    last_status["message"] = "Unauthorized Person Detected"
    last_status["access"] = "ACCESS DENIED"
    add_unauthorized_notification(frame)
    return jsonify({"ok": False, "message": last_status["message"], "status": last_status["access"]})


@app.route("/voice_command_text", methods=["POST"])
def voice_command_text():
    """Apply a voice command from transcript text (e.g. browser Web Speech API on the client)."""
    payload = request.json or {}
    text = payload.get("text", "")
    if not isinstance(text, str):
        return jsonify({"ok": False, "message": "Invalid text payload."}), 400
    body, status = apply_voice_command_text(text)
    return jsonify(body), status


@app.route("/start_voice_command", methods=["POST"])
def start_voice_command():
    if not VOICE_LIB_AVAILABLE:
        msg = "SpeechRecognition is not installed. Reinstall requirements."
        last_status["message"] = msg
        return jsonify({"ok": False, "message": f"{msg} ({VOICE_LIB_ERROR})"}), 503

    recognizer = sr.Recognizer()
    try:
        with sr.Microphone() as source:
            recognizer.adjust_for_ambient_noise(source, duration=0.8)
            audio = recognizer.listen(source, timeout=5, phrase_time_limit=5)

        text = recognizer.recognize_google(audio).lower().strip()
        body, status = apply_voice_command_text(text)
        return jsonify(body), status

    except sr.WaitTimeoutError:
        last_status["message"] = "Listening timeout. Please try again."
        return jsonify({"ok": False, "message": last_status["message"]}), 408
    except sr.UnknownValueError:
        last_status["message"] = "Could not understand speech."
        return jsonify({"ok": False, "message": last_status["message"]}), 400
    except Exception as exc:
        last_status["message"] = f"Voice error: {exc}"
        return jsonify({"ok": False, "message": last_status["message"]}), 500


@app.route("/status", methods=["GET"])
def status():
    reset_temporary_access_status()
    payload = dict(last_status)
    payload["controls"] = control_state
    payload["camera_index"] = camera_settings["camera_index"]
    payload["camera_ok"] = camera is not None and camera.isOpened()
    return jsonify(payload)


@app.route("/admin/users", methods=["GET"])
def admin_users():
    if not session.get("is_admin"):
        return jsonify({"ok": False, "message": "Unauthorized"}), 401
    users = []
    for user_dir in sorted(DATASET_DIR.iterdir()):
        if user_dir.is_dir():
            users.append(user_dir.name)
    return jsonify({"users": users})


@app.route("/admin/delete_face", methods=["POST"])
def delete_face():
    if not session.get("is_admin"):
        return jsonify({"ok": False, "message": "Unauthorized"}), 401
    global known_face_encodings, known_face_names
    username = (request.json or {}).get("username", "").strip()
    if not username:
        return jsonify({"ok": False, "message": "Username is required."}), 400

    user_dir = DATASET_DIR / username
    if user_dir.exists() and user_dir.is_dir():
        for image_path in user_dir.glob("*.jpg"):
            try:
                image_path.unlink()
            except OSError:
                pass
        try:
            user_dir.rmdir()
        except OSError:
            pass

    if FACE_LIB_AVAILABLE:
        filtered = [
            (enc, name)
            for enc, name in zip(known_face_encodings, known_face_names)
            if name != username
        ]
        known_face_encodings = [item[0] for item in filtered]
        known_face_names = [item[1] for item in filtered]
        save_known_faces(known_face_encodings, known_face_names)

    train_lbph_from_dataset()
    last_status["message"] = f"Deleted face data for {username}"
    last_status["access"] = "UPDATED"
    return jsonify({"ok": True, "message": last_status["message"]})


@app.route("/admin/fingerprint-users", methods=["GET"])
def admin_fingerprint_users():
    if not session.get("is_admin"):
        return jsonify({"ok": False, "message": "Unauthorized"}), 401
    fp_map = load_fingerprint_users()
    users = [
        {"username": u, "page_id": p}
        for u, p in sorted(fp_map.items(), key=lambda item: item[0].lower())
    ]
    return jsonify({"users": users})


@app.route("/admin/register_fingerprint", methods=["POST"])
def admin_register_fingerprint():
    if not session.get("is_admin"):
        return jsonify({"ok": False, "message": "Unauthorized"}), 401
    username = (request.json or {}).get("username", "").strip()
    if not username:
        return jsonify({"ok": False, "message": "Username is required."}), 400

    fp_map = load_fingerprint_users()
    if username in fp_map:
        page_id = fp_map[username]
    else:
        page_id = next_free_fingerprint_slot(fp_map)
        if page_id is None:
            return jsonify({"ok": False, "message": "No free fingerprint slots (max 162)."}), 400

    ok, raw = call_esp32(
        "/api/fingerprint/enroll",
        {"page_id": page_id},
        timeout_sec=FP_ENROLL_ESP32_TIMEOUT_SEC,
    )
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        body = {}
    if ok and body.get("ok"):
        fp_map[username] = page_id
        save_fingerprint_users(fp_map)
        last_status["message"] = f"Fingerprint enrolled for {username} (module slot {page_id})"
        last_status["access"] = "REGISTERED"
        return jsonify({"ok": True, "message": last_status["message"], "page_id": page_id})
    if body.get("message"):
        return jsonify({"ok": False, "message": body["message"]}), 400
    return jsonify({"ok": False, "message": raw or "ESP32 enroll failed"}), 502


@app.route("/admin/delete_fingerprint", methods=["POST"])
def admin_delete_fingerprint():
    if not session.get("is_admin"):
        return jsonify({"ok": False, "message": "Unauthorized"}), 401
    username = (request.json or {}).get("username", "").strip()
    if not username:
        return jsonify({"ok": False, "message": "Username is required."}), 400

    fp_map = load_fingerprint_users()
    if username not in fp_map:
        return jsonify({"ok": False, "message": "No fingerprint registered for that user."}), 404

    page_id = fp_map[username]
    ok, raw = call_esp32(
        "/api/fingerprint/delete",
        {"page_id": page_id},
        timeout_sec=15.0,
    )
    try:
        body = json.loads(raw)
    except json.JSONDecodeError:
        body = {}
    if ok and body.get("ok"):
        del fp_map[username]
        save_fingerprint_users(fp_map)
        last_status["message"] = f"Deleted fingerprint for {username} (slot {page_id})"
        last_status["access"] = "UPDATED"
        return jsonify({"ok": True, "message": last_status["message"]})
    if body.get("message"):
        return jsonify({"ok": False, "message": body["message"]}), 400
    return jsonify({"ok": False, "message": raw or "ESP32 delete failed"}), 502


@app.route("/admin/notifications", methods=["GET"])
def admin_notifications():
    if not session.get("is_admin"):
        return jsonify({"ok": False, "message": "Unauthorized"}), 401
    with notifications_lock:
        snapshot = list(notifications)
        unack = sum(
            1 for n in snapshot if not n.get("acknowledged") and n.get("kind") == "emergency"
        )
    return jsonify({"notifications": snapshot, "unacknowledged_count": unack})


@app.route("/admin/acknowledge_emergency", methods=["POST"])
def admin_acknowledge_emergency():
    """Mark a gas/smoke alert acknowledged and tell ESP32 to lock all doors (buzzer stops when ADC clears)."""
    if not session.get("is_admin"):
        return jsonify({"ok": False, "message": "Unauthorized"}), 401
    nid = str((request.json or {}).get("id", "")).strip()
    if not nid:
        return jsonify({"ok": False, "message": "Notification id required."}), 400
    with notifications_lock:
        found = None
        for note in notifications:
            if note.get("id") == nid:
                found = note
                break
        if found is None:
            return jsonify({"ok": False, "message": "Notification not found."}), 404
        if found.get("kind") != "emergency":
            return jsonify({"ok": False, "message": "Only gas/smoke emergency alerts can use this action."}), 400
        if found.get("acknowledged"):
            return jsonify({"ok": True, "message": "Already acknowledged.", "already": True})
        found["acknowledged"] = True
        save_notifications()
    ok, esp_msg = call_esp32("/api/control", {"command": "close_all_doors"})
    control_state["door"] = "CLOSED"
    control_state["room_door"] = "CLOSED"
    control_state["exit_door"] = "CLOSED"
    last_status["command"] = "CLOSE ALL DOORS (emergency ack)"
    last_status["message"] = (
        "Emergency acknowledged; all doors close command sent to ESP32."
        if ok
        else f"Emergency marked acknowledged; ESP32 error: {esp_msg}"
    )
    return jsonify(
        {
            "ok": True,
            "message": last_status["message"],
            "esp32": {"ok": ok, "message": esp_msg},
        }
    )


@app.route("/admin/clear_notifications", methods=["POST"])
def clear_notifications():
    if not session.get("is_admin"):
        return jsonify({"ok": False, "message": "Unauthorized"}), 401
    with notifications_lock:
        notifications.clear()
        save_notifications()
    return jsonify({"ok": True, "message": "Notifications cleared"})


@app.route("/admin/control", methods=["POST"])
def admin_control():
    if not session.get("is_admin"):
        return jsonify({"ok": False, "message": "Unauthorized"}), 401
    command = normalize_esp32_control_command((request.json or {}).get("command", ""))
    if command == "turn on light":
        control_state["light"] = "ON"
        last_status["command"] = "TURN ON LIGHT"
    elif command == "turn off light":
        control_state["light"] = "OFF"
        last_status["command"] = "TURN OFF LIGHT"
    elif command == "turn on light 2":
        control_state["light_2"] = "ON"
        last_status["command"] = "TURN ON LIGHT 2"
    elif command == "turn off light 2":
        control_state["light_2"] = "OFF"
        last_status["command"] = "TURN OFF LIGHT 2"
    elif command == "open door":
        control_state["door"] = "OPEN"
        last_status["command"] = "OPEN DOOR"
    elif command == "open entrance door":
        control_state["door"] = "OPEN"
        last_status["command"] = "OPEN ENTRANCE DOOR"
    elif command == "open room door":
        control_state["room_door"] = "OPEN"
        last_status["command"] = "OPEN ROOM DOOR"
    elif command == "open exit door":
        control_state["exit_door"] = "OPEN"
        last_status["command"] = "OPEN EXIT DOOR"
    elif command == "open_all_doors":
        control_state["door"] = "OPEN"
        control_state["room_door"] = "OPEN"
        control_state["exit_door"] = "OPEN"
        last_status["command"] = "OPEN ALL DOORS"
    elif command == "close door":
        control_state["door"] = "CLOSED"
        last_status["command"] = "CLOSE DOOR"
    elif command == "close entrance door":
        control_state["door"] = "CLOSED"
        last_status["command"] = "CLOSE ENTRANCE DOOR"
    elif command == "close room door":
        control_state["room_door"] = "CLOSED"
        last_status["command"] = "CLOSE ROOM DOOR"
    elif command == "close exit door":
        control_state["exit_door"] = "CLOSED"
        last_status["command"] = "CLOSE EXIT DOOR"
    elif command in ("close_all_doors", "lock_all_doors"):
        control_state["door"] = "CLOSED"
        control_state["room_door"] = "CLOSED"
        control_state["exit_door"] = "CLOSED"
        last_status["command"] = "CLOSE ALL DOORS"
    else:
        return jsonify({"ok": False, "message": "Invalid command"}), 400

    last_status["message"] = f"Admin command: {last_status['command']}"
    success, esp_message = call_esp32("/api/control", {"command": command})
    return jsonify(
        {
            "ok": True,
            "message": last_status["message"],
            "controls": control_state,
            "esp32": {"ok": success, "message": esp_message},
        }
    )


@app.route("/admin/esp32-settings", methods=["GET", "POST"])
def admin_esp32_settings():
    if not session.get("is_admin"):
        return jsonify({"ok": False, "message": "Unauthorized"}), 401

    if request.method == "GET":
        return jsonify({"ok": True, "settings": esp32_settings})

    payload = request.json or {}
    base_url = normalize_esp32_base_url(payload.get("base_url", ""))
    api_key = str(payload.get("api_key", "")).strip()
    timeout_sec = float(payload.get("timeout_sec", 2.0))
    motion_poll = float(payload.get("motion_poll_sec", esp32_settings.get("motion_poll_sec", 0.65)))
    esp32_settings["base_url"] = base_url
    esp32_settings["api_key"] = api_key
    esp32_settings["timeout_sec"] = max(0.5, min(timeout_sec, 10.0))
    esp32_settings["motion_poll_sec"] = max(0.35, min(motion_poll, 3.0))
    save_esp32_settings()
    return jsonify({"ok": True, "message": "ESP32 settings saved.", "settings": esp32_settings})


@app.route("/admin/esp32-test", methods=["POST"])
def admin_esp32_test():
    if not session.get("is_admin"):
        return jsonify({"ok": False, "message": "Unauthorized"}), 401
    success, message = call_esp32("/api/ping")
    fingerprint_ready = None
    if success and message:
        try:
            data = json.loads(message)
            fingerprint_ready = data.get("fingerprint_ready")
            base = data.get("message", "ESP32 online")
            if fingerprint_ready is True:
                message = f"{base} — Fingerprint sensor: ready."
            elif fingerprint_ready is False:
                message = (
                    f"{base} — Fingerprint sensor: NOT ready (UART/power/baud or boot timing; "
                    "use Retry sensor handshake or check Thonny serial for AS608 lines)."
                )
            else:
                message = base
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    return jsonify(
        {"ok": success, "message": message, "fingerprint_ready": fingerprint_ready}
    )


@app.route("/admin/fingerprint-reinit", methods=["POST"])
def admin_fingerprint_reinit():
    if not session.get("is_admin"):
        return jsonify({"ok": False, "message": "Unauthorized"}), 401
    ok, raw = call_esp32("/api/fingerprint/reinit", payload={}, timeout_sec=25.0)
    if not ok:
        return jsonify({"ok": False, "message": raw}), 502
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, TypeError, ValueError):
        return jsonify({"ok": False, "message": raw or "Invalid JSON from ESP32"}), 502
    ready = bool(data.get("fingerprint_ready"))
    msg = data.get("message", raw)
    if ready:
        return jsonify({"ok": True, "message": msg, "fingerprint_ready": True})
    return jsonify({"ok": False, "message": msg, "fingerprint_ready": False}), 503


@app.route("/admin/camera-settings", methods=["GET", "POST"])
def admin_camera_settings():
    if not session.get("is_admin"):
        return jsonify({"ok": False, "message": "Unauthorized"}), 401

    if request.method == "GET":
        return jsonify({"ok": True, "settings": camera_settings})

    payload = request.json or {}
    idx = int(payload.get("camera_index", camera_settings["camera_index"]))
    camera_settings["camera_index"] = max(0, min(idx, 10))
    save_camera_settings()
    ok, err = reinit_camera()
    msg = (
        "Using camera index {}.".format(camera_settings["camera_index"])
        if ok
        else (err or "Could not open that camera.")
    )
    return jsonify({"ok": ok, "message": msg, "settings": camera_settings})


@app.route("/admin/camera-probe", methods=["GET"])
def admin_camera_probe():
    if not session.get("is_admin"):
        return jsonify({"ok": False, "message": "Unauthorized"}), 401

    results = []
    for idx in range(0, 11):
        cap, err = open_capture(idx)
        opened = cap is not None and cap.isOpened()
        if cap is not None:
            try:
                cap.release()
            except Exception:
                pass
        results.append(
            {
                "index": idx,
                "ok": opened,
                "message": "available" if opened else (err or "not available"),
            }
        )
    return jsonify({"ok": True, "results": results})


@app.teardown_appcontext
def cleanup_camera(_exception):
    # Intentionally keep camera open during app life. Release only on process stop.
    pass


def esp32_motion_poll_loop():
    global last_esp32_motion_high, last_esp32_emergency_high
    time.sleep(2.0)
    while True:
        poll_sec = float(esp32_settings.get("motion_poll_sec", 0.65))
        poll_sec = max(0.35, min(poll_sec, 3.0))
        time.sleep(poll_sec)
        if not str(esp32_settings.get("base_url", "")).strip():
            continue
        ok, raw = call_esp32("/api/ping")
        if not ok or not raw:
            continue
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, TypeError, ValueError):
            continue
        motion = bool(data.get("motion"))
        if motion and not last_esp32_motion_high:
            add_security_motion_notification()
        last_esp32_motion_high = motion

        emergency = bool(data.get("emergency"))
        if emergency and not last_esp32_emergency_high:
            add_gas_smoke_emergency_notification(data.get("gas", "?"), data.get("smoke", "?"))
        last_esp32_emergency_high = emergency


threading.Thread(target=esp32_motion_poll_loop, daemon=True).start()


if __name__ == "__main__":
    _port = int(os.getenv("SMARTHOUSE_PORT", "5000"))
    # Listen on all interfaces so phones/PCs on the same LAN can reach the Pi.
    print("SmartHouse: open in a browser on another device: http://<this-machine-ip>:{}/door".format(_port))
    app.run(host="0.0.0.0", port=_port, debug=True)
