# Smart House — Flask web app

A local **Flask** application that runs the smart-home UI: **door portal** (face recognition), **admin portal** (face + fingerprint registration, ESP32 controls, security alerts, voice), and optional integration with an **ESP32** (doors, lights, gas/smoke, PIR, fingerprint sensor). See **`ESP32_WIRING.md`** for microcontroller pins and hardware.

---

## What you need

| Item | Purpose |
|------|--------|
| **Python 3.11** (recommended) | App runtime. On **3.13+**, `requirements.txt` may skip `face_recognition` and `PyAudio`; use **3.11 or 3.12** for those extras. |
| **Webcam** (USB or built-in) | Face capture runs **on the machine that executes `app.py`** (not in the remote browser). |
| **Microphone** (optional) | Voice commands in Admin. |
| **Raspberry Pi** (optional) | Typical deployment: run Flask on the Pi and plug the USB camera into the Pi. |
| **ESP32 with MicroPython** (optional) | Physical doors, light, MQ gas/smoke, PIR, buzzer, AS608 fingerprint — see `esp32/main.py` and `ESP32_WIRING.md`. |

No database is required; data is stored in files under the project folder (`dataset/`, `*.json`, `*.pkl`).

---

## Installs (Python packages)

Dependencies are listed in **`requirements.txt`**:

| Package | Role |
|---------|------|
| **Flask** | Web server, routes, sessions, JSON APIs |
| **opencv-contrib-python** | Camera, face detection, **LBPH** fallback recognizer |
| **numpy** | Used by OpenCV |
| **face_recognition** | Primary face pipeline (only if your Python version matches `requirements.txt`; often needs **dlib**) |
| **SpeechRecognition** | Voice → text in admin |
| **PyAudio** | Microphone input for voice (when installed for your Python version) |

Install everything into a **virtual environment** (recommended):

### Windows (PowerShell)

```powershell
cd "D:\PAID PROJECTS\smartHouse"
py -3.11 -m venv .venv
.\.venv\Scripts\activate
python -m pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
```

### Linux / Raspberry Pi OS

```bash
cd /path/to/smartHouse
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip setuptools wheel
pip install -r requirements.txt
```

On the Pi, if the camera or audio stack complains about missing system libraries, install the usual OpenCV/audio deps for your distro (e.g. `libcamera` / `v4l` for cameras, `portaudio19-dev` before `PyAudio` on Debian/Ubuntu).

---

## How to run the web app

1. **Activate the venv** (same shell you used for `pip install`).
2. **Start the server:**

   ```powershell
   python app.py
   ```

   On Linux/Pi:

   ```bash
   python app.py
   ```

3. **Open a browser** on the same machine or another device on the LAN:
   - **Door / kiosk UI:** `http://localhost:5000` → redirects to **`/door`**
   - **Admin login:** `http://localhost:5000/admin-login` → after login, **`/admin`**

Default **admin** credentials are set by environment variables (optional). If unset, they fall back to:

- Username: **`admin`**
- Password: **`admin123`**

Override for production:

```powershell
$env:ADMIN_USERNAME = "youruser"
$env:ADMIN_PASSWORD = "strong-password"
$env:FLASK_SECRET_KEY = "long-random-string"
python app.py
```

```bash
export ADMIN_USERNAME=youruser
export ADMIN_PASSWORD='strong-password'
export FLASK_SECRET_KEY='long-random-string'
python app.py
```

**Port:** default **5000**. Change with:

```powershell
$env:SMARTHOUSE_PORT = "8080"
python app.py
```

The console prints the URL to use from other devices (host `0.0.0.0`).

---

## First-time configuration (after install)

1. **Log in** at **`/admin-login`**.
2. **Camera:** If the wrong device opens (common on Pi when index `0` is not the USB webcam), use **Admin → Camera** to set index **1** or **2**, or set `SMARTHOUSE_CAMERA_INDEX` before starting the app. Settings are saved in **`camera_settings.json`**.
3. **ESP32 (optional):** In **Admin → ESP32 Settings**, set the ESP32’s URL (e.g. `192.168.1.50` — `http://` is added if missing), **API key** (must match `API_KEY` in `esp32/main.py`), and **Motion poll sec** for PIR notifications. Saved in **`esp32_settings.json`**.
4. **Register users:** Same admin page — **Register Face** (browser webcam), **Register Fingerprint** (enrollment runs on the ESP32 AS608; place finger twice).

---

## Project structure (overview)

```text
smartHouse/
  app.py                 # Flask application
  requirements.txt       # pip dependencies
  ESP32_WIRING.md        # Hardware pinout and sensors
  esp32/
    main.py              # MicroPython firmware (upload to ESP32)
    test_sensors.py
    test_fingerprint.py
  templates/             # HTML (door, admin, login, …)
  static/                # CSS, alert images
  dataset/               # Face samples per user (created at runtime)
  *.json / *.pkl         # Settings, encodings, notifications (runtime)
```

---

## Camera: laptop vs Raspberry Pi

Video is captured **only on the host running `python app.py`**.

- Run Flask **on the Pi** with the USB webcam on the Pi to use that camera.
- If you run Flask on a **laptop** but open the site on the Pi browser, you still see the **laptop** camera.

---

## Face recognition modes

1. **Primary:** `face_recognition` (if installed — requires **dlib** on Windows in many cases).
2. **Fallback:** OpenCV Haar cascade + **LBPH** (`opencv-contrib-python`) if `face_recognition` is missing.

---

## Troubleshooting

### `face_recognition` / `dlib` fails (Windows)

The app still runs in OpenCV fallback mode. For full `face_recognition`, install **CMake** and **Microsoft C++ Build Tools**, open a new terminal, confirm `cmake` and `cl` work, then in the venv:

```powershell
pip install dlib
pip install face_recognition
```

Example (winget):

```powershell
winget install -e --id Kitware.CMake --accept-source-agreements --accept-package-agreements
winget install -e --id Microsoft.VisualStudio.2022.BuildTools --override "--wait --quiet --norestart --nocache --installPath C:\BuildTools --add Microsoft.VisualStudio.Workload.VCTools --includeRecommended" --accept-source-agreements --accept-package-agreements
```

### NumPy / OpenCV mismatch

```powershell
python -m pip uninstall -y numpy
python -m pip install --no-cache-dir numpy==2.3.3
```

### Voice: PyAudio missing

```powershell
pip install PyAudio
```

### Reset registered faces

```powershell
Remove-Item -Recurse -Force "dataset\*"
Remove-Item -Force "lbph_model.yml","lbph_labels.pkl","encodings.pkl" -ErrorAction SilentlyContinue
```

### PIR “not sensitive” / few motion alerts

Admin notifies on **new** motion edges only, and the HC-SR501 **sensitivity** and **time-delay** pots matter. See **`ESP32_WIRING.md`** (HC-SR501 section).

### ESP32 gas/smoke emergency

If analog readings sit at max, wiring or warm-up may be wrong — see **`ESP32_WIRING.md`** before depending on thresholds.

---

## ESP32 summary

- Flash **`esp32/main.py`** with **MicroPython**, configure WiFi and **`API_KEY`** to match the admin panel.
- **REST:** `GET /api/ping` (status, motion, emergency), `POST /api/control` (door/light commands), fingerprint enroll/delete under **`/api/fingerprint/*`**.
- Full pin map: **`ESP32_WIRING.md`**.

https://drive.google.com/file/d/1aOMOt-hKQE58HvCIdc49YxgJFsKBrM1Q/view?usp=sharing
---

## Security notes

- Change **admin password** and **`FLASK_SECRET_KEY`** for any network-exposed deployment.
- This stack is intended for **trusted LAN / home lab** use; add HTTPS and hardening for internet-facing installs.
