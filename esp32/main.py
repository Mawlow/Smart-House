import network
import socket
import time

try:
    import ujson as json
except ImportError:
    import json

from machine import ADC, PWM, Pin, UART

# -----------------------------
# WiFi + API config
# -----------------------------
WIFI_SSID = "ZTE_2.4G_2hDpYx"
WIFI_PASSWORD = "dniZYDVK"
API_KEY = "smarthouse-key"
HTTP_PORT = 80

# -----------------------------
# Pin mapping (edit if needed)
# -----------------------------
RELAY_ENTRANCE = Pin(23, Pin.OUT, value=1)  # active-low relay
RELAY_ROOM = Pin(22, Pin.OUT, value=1)      # active-low relay
RELAY_EXIT = Pin(21, Pin.OUT, value=1)      # active-low relay
LED_LIGHT = Pin(2, Pin.OUT, value=0)
LED_LIGHT_2 = Pin(4, Pin.OUT, value=0)

# Servos: entrance + exit (room is relay-only)
SERVO_ENTRANCE_PWM = PWM(Pin(18), freq=50)
SERVO_EXIT_PWM = PWM(Pin(19), freq=50)
SERVO_UNLOCK_ANGLE = 95
SERVO_LOCK_ANGLE = 0
GAS_ADC = ADC(Pin(34))   # gas sensor analog (e.g. MQ-135 AO)
SMOKE_ADC = ADC(Pin(35))  # smoke sensor analog
GAS_ADC.atten(ADC.ATTN_11DB)
SMOKE_ADC.atten(ADC.ATTN_11DB)

# Buzzer alarm (gas/smoke emergency). Two common hardware types:
#   - *Passive* piezo: needs a tone — use BUZZER_MODE = "pwm" (default below).
#   - *Active* module (built-in oscillator): steady DC — use BUZZER_MODE = "active".
# If wired to transistor that pulls LOW to sound, set BUZZER_ACTIVE_HIGH = False (active mode only).
# Change BUZZER_GPIO if you use another pin (avoid pins already used above).
USE_BUZZER_ALARM = True
BUZZER_GPIO = 25
BUZZER_MODE = "pwm"  # "pwm" | "active"
BUZZER_ACTIVE_HIGH = True
# Passive piezos: loudest with ~50% duty (≈512). Higher duty → more DC → often quieter.
# Tune BUZZER_PWM_FREQ (try 2500–4500 Hz) to match your part — wrong freq sounds weak.
BUZZER_PWM_FREQ = 4000
BUZZER_PWM_DUTY = 512

_buzzer_pwm = None
_buzzer_out = None


def _buzzer_init():
    global _buzzer_pwm, _buzzer_out
    _buzzer_pwm = None
    _buzzer_out = None
    if not USE_BUZZER_ALARM:
        return
    mode = (BUZZER_MODE or "pwm").lower()
    if mode == "pwm":
        _buzzer_pwm = PWM(Pin(BUZZER_GPIO), freq=int(BUZZER_PWM_FREQ), duty=0)
    else:
        idle = 0 if BUZZER_ACTIVE_HIGH else 1
        _buzzer_out = Pin(BUZZER_GPIO, Pin.OUT, value=idle)


_buzzer_init()

# HC-SR501 front-of-house PIR: HIGH = motion (use 3.3V-safe OUT or a divider if module outputs 5V).
# If detection feels weak: increase SENS pot, shorten time-delay pot, use H (retrigger) jumper — see ESP32_WIRING.md.
MOTION_FRONT = Pin(32, Pin.IN, Pin.PULL_DOWN)

# Optional fingerprint sensor over UART (AS608 / common clones).
# Set False if no module is wired — skips long boot probes (no "AS608 no valid reply" spam).
FP_USE_SENSOR = True
FP_UART_NR = 2
FP_UART_TX = 17
FP_UART_RX = 16
# Try in order; many modules use 57600, some 115200 or 9600.
FP_UART_BAUDS = (57600, 115200, 9600)
fp_uart = UART(FP_UART_NR, baudrate=FP_UART_BAUDS[0], tx=FP_UART_TX, rx=FP_UART_RX)

# When gas OR smoke ADC is at or above threshold, firmware calls open_all_doors() (all relays + servos).
# Checked every main-loop iteration, on each HTTP request, and when serving GET /api/ping.
GAS_THRESHOLD = 2600
SMOKE_THRESHOLD = 2600
ACCESS_OPEN_SECONDS = 4
FP_ADDRESS = b"\xFF\xFF\xFF\xFF"
FP_HEADER = b"\xEF\x01"
fp_ready = False
last_fp_match_ms = 0
FP_COOLDOWN_MS = 6000
_last_motion_pin_high = False
_last_motion_log_ms = 0
MOTION_SERIAL_COOLDOWN_MS = 4000

# ESP32 STA status() values (common); helps when IP stays "not connected"
_WIFI_STAT_TEXT = {
    1000: "IDLE",
    1001: "CONNECTING",
    1010: "GOT_IP",
    201: "NO_AP_FOUND",
    202: "WRONG_PASSWORD",
    203: "ASSOC_LEAVE",
    204: "ASSOC_FAIL",
    205: "CONNECTION_FAIL",
    -1: "DISCONNECTED",
}


def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if wlan.isconnected():
        print("WiFi already connected:", wlan.ifconfig())
        return wlan

    if WIFI_SSID in ("", "YOUR_WIFI_NAME") or WIFI_PASSWORD in ("", "YOUR_WIFI_PASSWORD"):
        print("WiFi: set WIFI_SSID and WIFI_PASSWORD in main.py (not the placeholder).")
        return wlan

    wlan.connect(WIFI_SSID, WIFI_PASSWORD)
    print("Connecting WiFi to", WIFI_SSID, end="")
    for _ in range(40):
        if wlan.isconnected():
            break
        print(".", end="")
        time.sleep(0.4)
    print("")
    if wlan.isconnected():
        print("IP:", wlan.ifconfig()[0], "netmask:", wlan.ifconfig()[1], "gw:", wlan.ifconfig()[2])
    else:
        st = wlan.status()
        hint = _WIFI_STAT_TEXT.get(st, "?")
        print("WiFi FAILED — status()=", st, "(" + hint + ")")
        print("  Check: 2.4GHz SSID/password, router in range, hidden-SSID allowed, MAC filter off.")
    return wlan


def _relay_open(pin):
    pin.value(0)  # active-low ON


def _relay_lock(pin):
    pin.value(1)  # active-low OFF


def open_all_doors():
    _relay_open(RELAY_ENTRANCE)
    _relay_open(RELAY_ROOM)
    _relay_open(RELAY_EXIT)
    set_servo_angle(SERVO_ENTRANCE_PWM, SERVO_UNLOCK_ANGLE)
    set_servo_angle(SERVO_EXIT_PWM, SERVO_UNLOCK_ANGLE)


def lock_all_doors():
    set_servo_angle(SERVO_ENTRANCE_PWM, SERVO_LOCK_ANGLE)
    _relay_lock(RELAY_ENTRANCE)
    _relay_lock(RELAY_ROOM)
    _relay_lock(RELAY_EXIT)
    time.sleep_ms(120)
    set_servo_angle(SERVO_EXIT_PWM, SERVO_LOCK_ANGLE)


def set_servo_angle(pwm, angle):
    angle = max(0, min(180, int(angle)))
    duty_us = int(500 + (angle / 180.0) * 2000)  # 500us..2500us
    duty = int((duty_us / 20000) * 1023)
    pwm.duty(duty)


def grant_access():
    _relay_open(RELAY_ENTRANCE)
    set_servo_angle(SERVO_ENTRANCE_PWM, SERVO_UNLOCK_ANGLE)
    time.sleep(ACCESS_OPEN_SECONDS)
    set_servo_angle(SERVO_ENTRANCE_PWM, SERVO_LOCK_ANGLE)
    _relay_lock(RELAY_ENTRANCE)


def apply_command(command):
    cmd = str(command or "").strip().lower()
    cmd = " ".join(cmd.split())
    _underscore = {
        "close_exit_door": "close exit door",
        "open_exit_door": "open exit door",
        "close_room_door": "close room door",
        "open_room_door": "open room door",
        "close_entrance_door": "close entrance door",
        "open_entrance_door": "open entrance door",
        "turn_on_light_2": "turn on light 2",
        "turn_off_light_2": "turn off light 2",
    }
    cmd = _underscore.get(cmd, cmd)
    _aliases = {
        "close all doors": "close_all_doors",
        "close all the doors": "close_all_doors",
        "lock all doors": "lock_all_doors",
        "open all doors": "open_all_doors",
        "open all the doors": "open_all_doors",
    }
    cmd = _aliases.get(cmd, cmd)
    if cmd == "turn on light two":
        cmd = "turn on light 2"
    elif cmd == "turn off light two":
        cmd = "turn off light 2"
    if cmd == "turn on light":
        LED_LIGHT.value(1)
        return True, "Light turned ON"
    if cmd == "turn off light":
        LED_LIGHT.value(0)
        return True, "Light turned OFF"
    if cmd == "turn on light 2":
        LED_LIGHT_2.value(1)
        return True, "Light 2 turned ON"
    if cmd == "turn off light 2":
        LED_LIGHT_2.value(0)
        return True, "Light 2 turned OFF"
    if cmd in ("open door", "open entrance door"):
        _relay_open(RELAY_ENTRANCE)
        set_servo_angle(SERVO_ENTRANCE_PWM, SERVO_UNLOCK_ANGLE)
        return True, "Entrance door opened"
    if cmd == "open room door":
        _relay_open(RELAY_ROOM)
        return True, "Room relay opened"
    if cmd == "open exit door":
        _relay_open(RELAY_EXIT)
        set_servo_angle(SERVO_EXIT_PWM, SERVO_UNLOCK_ANGLE)
        return True, "Exit door opened"
    if cmd == "close room door":
        _relay_lock(RELAY_ROOM)
        return True, "Room relay locked"
    if cmd == "close exit door":
        # De-energize exit solenoid first so the latch can move; then drive servo to locked.
        _relay_lock(RELAY_EXIT)
        time.sleep_ms(120)
        set_servo_angle(SERVO_EXIT_PWM, SERVO_LOCK_ANGLE)
        return True, "Exit door locked"
    if cmd in ("close door", "close entrance door"):
        set_servo_angle(SERVO_ENTRANCE_PWM, SERVO_LOCK_ANGLE)
        _relay_lock(RELAY_ENTRANCE)
        return True, "Entrance door locked"
    if cmd == "grant_access":
        grant_access()
        return True, "Grant access executed"
    if cmd == "open_all_doors":
        open_all_doors()
        return True, "All doors opened"
    if cmd in ("lock_all_doors", "close_all_doors"):
        lock_all_doors()
        return True, "All doors locked"
    return False, "Invalid command"


def buzzer_alarm_on():
    if not USE_BUZZER_ALARM:
        return
    if _buzzer_pwm is not None:
        _buzzer_pwm.duty(max(0, min(1023, int(BUZZER_PWM_DUTY))))
    elif _buzzer_out is not None:
        _buzzer_out.value(1 if BUZZER_ACTIVE_HIGH else 0)


def buzzer_alarm_off():
    if not USE_BUZZER_ALARM:
        return
    if _buzzer_pwm is not None:
        _buzzer_pwm.duty(0)
    elif _buzzer_out is not None:
        _buzzer_out.value(0 if BUZZER_ACTIVE_HIGH else 1)


def check_emergency():
    """If gas or smoke exceeds threshold, unlock everything and sound the buzzer."""
    gas_value = GAS_ADC.read()
    smoke_value = SMOKE_ADC.read()
    if gas_value >= GAS_THRESHOLD or smoke_value >= SMOKE_THRESHOLD:
        open_all_doors()
        buzzer_alarm_on()
        return True, gas_value, smoke_value
    buzzer_alarm_off()
    return False, gas_value, smoke_value


def motion_front_active():
    return MOTION_FRONT.value() == 1


def _fp_checksum(packet_type, payload):
    total = packet_type + len(payload) + 2
    for value in payload:
        total += value
    return total & 0xFFFF


def fp_send_command(command, params=b"", timeout_ms=900):
    payload = bytes([command]) + params
    length = len(payload) + 2
    packet_type = 0x01  # command packet
    checksum = _fp_checksum(packet_type, payload)
    packet = (
        FP_HEADER
        + FP_ADDRESS
        + bytes([packet_type])
        + length.to_bytes(2, "big")
        + payload
        + checksum.to_bytes(2, "big")
    )
    fp_uart.read()  # clear stale bytes
    fp_uart.write(packet)
    return fp_read_packet(timeout_ms)


def fp_read_packet(timeout_ms):
    started = time.ticks_ms()
    buf = b""
    while time.ticks_diff(time.ticks_ms(), started) < timeout_ms:
        if fp_uart.any():
            buf += fp_uart.read()
            if len(buf) >= 9:
                packet_len = int.from_bytes(buf[7:9], "big")
                total_len = 9 + packet_len
                if len(buf) >= total_len:
                    packet_type = buf[6]
                    payload = buf[9:9 + packet_len - 2]
                    return packet_type, payload
        time.sleep_ms(5)
    return None, b""


def fp_verify_password():
    # VfyPwd (0x13); default password is usually 0x00000000 (change if you set one in software).
    overall_last_pt = None
    for attempt in range(2):
        if attempt:
            print("AS608: retry handshake after 2s (sensor may need warm-up)...")
            time.sleep_ms(2000)
        last_pt = None
        for baud in FP_UART_BAUDS:
            try:
                fp_uart.init(baudrate=baud, tx=FP_UART_TX, rx=FP_UART_RX)
            except Exception as exc:
                print("AS608 UART init failed at baud", baud, ":", exc)
                continue
            time.sleep_ms(250)
            fp_uart.read()
            packet_type = None
            payload = b""
            for _ in range(4):
                packet_type, payload = fp_send_command(0x13, b"\x00\x00\x00\x00", timeout_ms=1600)
                last_pt = packet_type
                overall_last_pt = packet_type
                if packet_type == 0x07 and len(payload) > 0 and payload[0] == 0x00:
                    print("AS608 OK at baud", baud)
                    return True
                time.sleep_ms(120)
            print(
                "AS608 no valid reply at baud",
                baud,
                "last_type=",
                packet_type,
                "ack=",
                payload[:1] if payload else b"",
            )
        print(
            "AS608 check: wiring TX/RX (ESP TX->sensor RX, ESP RX<-sensor TX), 3.3V logic, power, baud. Last pkt type=",
            last_pt,
        )
    print("AS608: failed after 2 attempts; last pkt type=", overall_last_pt)
    return False


def fp_search_once():
    # 1) Capture image
    packet_type, payload = fp_send_command(0x01)
    if not (packet_type == 0x07 and payload and payload[0] == 0x00):
        return False

    # 2) Convert image to char buffer 1
    packet_type, payload = fp_send_command(0x02, b"\x01")
    if not (packet_type == 0x07 and payload and payload[0] == 0x00):
        return False

    # 3) Search in full template library from page 0 count 162
    packet_type, payload = fp_send_command(0x04, b"\x01\x00\x00\x00\xA2")
    if not (packet_type == 0x07 and payload):
        return False

    # ACK code 0x00 = match found, then 2 bytes page id + 2 bytes score
    return payload[0] == 0x00


def fingerprint_access_detected():
    global fp_ready, last_fp_match_ms
    if not fp_ready:
        return False

    now = time.ticks_ms()
    if time.ticks_diff(now, last_fp_match_ms) < FP_COOLDOWN_MS:
        return False

    if fp_search_once():
        last_fp_match_ms = now
        return True
    return False


# AS608 library slots: page IDs 0 .. FP_MAX_PAGE_ID (162 templates).
FP_MAX_PAGE_ID = 161


def fp_wait_image_ok(timeout_ms):
    """GenImg (0x01) until finger present (ACK 0x00) or timeout. ACK 0x02 = no finger."""
    deadline = time.ticks_add(time.ticks_ms(), timeout_ms)
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        packet_type, payload = fp_send_command(0x01, b"", timeout_ms=1200)
        if packet_type == 0x07 and payload:
            code = payload[0]
            if code == 0x00:
                return True, None
            if code == 0x02:
                time.sleep_ms(100)
                continue
            return False, "get_image_ack_0x{:02x}".format(code)
        time.sleep_ms(40)
    return False, "timeout_waiting_finger"


def fp_enroll_at_page(page_id):
    """
    Two captures -> RegModel -> Store in flash at page_id.
    Caller should allow ~2 minutes for user to press finger twice.
    """
    if page_id < 0 or page_id > FP_MAX_PAGE_ID:
        return False, "page_id out of range 0-161"
    if not fp_ready:
        return False, "fingerprint sensor not ready"

    ok, err = fp_wait_image_ok(55000)
    if not ok:
        return False, err or "first_scan_failed"

    packet_type, payload = fp_send_command(0x02, b"\x01", timeout_ms=1500)
    if not (packet_type == 0x07 and payload and payload[0] == 0x00):
        return False, "image2tz_buffer1_failed"

    time.sleep_ms(2200)

    ok, err = fp_wait_image_ok(55000)
    if not ok:
        return False, err or "second_scan_failed"

    packet_type, payload = fp_send_command(0x02, b"\x02", timeout_ms=1500)
    if not (packet_type == 0x07 and payload and payload[0] == 0x00):
        return False, "image2tz_buffer2_failed"

    packet_type, payload = fp_send_command(0x05, b"", timeout_ms=1500)
    if not (packet_type == 0x07 and payload and payload[0] == 0x00):
        ack = payload[0] if payload else -1
        if ack == 0x0A:
            return False, "reg_model_merge_failed_lift_and_try_again"
        return False, "reg_model_failed_0x{:02x}".format(ack)

    packet_type, payload = fp_send_command(
        0x06,
        bytes([0x01, (page_id >> 8) & 0xFF, page_id & 0xFF]),
        timeout_ms=1500,
    )
    if not (packet_type == 0x07 and payload and payload[0] == 0x00):
        ack = payload[0] if payload else -1
        return False, "store_failed_0x{:02x}".format(ack)

    return True, "stored"


def fp_delete_template(page_id):
    """Delete template at page_id (command 0x0C, 2-byte page index)."""
    if page_id < 0 or page_id > FP_MAX_PAGE_ID:
        return False, "page_id out of range 0-161"
    if not fp_ready:
        return False, "fingerprint sensor not ready"
    packet_type, payload = fp_send_command(
        0x0C,
        bytes([(page_id >> 8) & 0xFF, page_id & 0xFF]),
        timeout_ms=1500,
    )
    if packet_type == 0x07 and payload and payload[0] == 0x00:
        return True, "deleted"
    ack = payload[0] if payload else -1
    return False, "delete_failed_0x{:02x}".format(ack)


def http_response(conn, status_code=200, payload=None):
    if payload is None:
        payload = {}
    body = json.dumps(payload)
    status_text = "OK" if status_code == 200 else "ERROR"
    response = (
        "HTTP/1.1 {} {}\r\n"
        "Content-Type: application/json\r\n"
        "Connection: close\r\n"
        "Content-Length: {}\r\n\r\n{}"
    ).format(status_code, status_text, len(body), body)
    conn.send(response.encode("utf-8"))


def parse_request(raw):
    text = raw.decode("utf-8", "ignore")
    headers, _, body = text.partition("\r\n\r\n")
    first_line = headers.split("\r\n")[0] if headers else ""
    parts = first_line.split(" ")
    method = parts[0] if len(parts) > 0 else ""
    path = parts[1] if len(parts) > 1 else "/"
    header_lines = headers.split("\r\n")[1:]
    header_map = {}
    for line in header_lines:
        if ":" in line:
            k, v = line.split(":", 1)
            header_map[k.strip().lower()] = v.strip()
    return method, path, header_map, body


def handle_request(conn, raw):
    global fp_ready
    method, path, headers, body = parse_request(raw)
    emergency, gas, smoke = check_emergency()
    api_key = headers.get("x-api-key", "")
    if path.startswith("/api/") and api_key != API_KEY:
        http_response(conn, 401, {"ok": False, "message": "Unauthorized API key"})
        return

    if method == "GET" and path == "/api/ping":
        motion = motion_front_active()
        http_response(
            conn,
            200,
            {
                "ok": True,
                "message": "ESP32 online",
                "gas": gas,
                "smoke": smoke,
                "emergency": emergency,
                "motion": motion,
                "fingerprint_ready": bool(fp_ready),
            },
        )
        return

    if method == "POST" and path == "/api/control":
        try:
            data = json.loads(body) if body else {}
        except Exception:
            http_response(conn, 400, {"ok": False, "message": "Invalid JSON"})
            return
        ok, message = apply_command(data.get("command", ""))
        check_emergency()
        code = 200 if ok else 400
        http_response(conn, code, {"ok": ok, "message": message})
        return

    if method == "POST" and path == "/api/fingerprint/enroll":
        try:
            data = json.loads(body) if body else {}
        except Exception:
            http_response(conn, 400, {"ok": False, "message": "Invalid JSON"})
            return
        try:
            page_id = int(data.get("page_id", -1))
        except (TypeError, ValueError):
            page_id = -1
        ok, msg = fp_enroll_at_page(page_id)
        if ok:
            http_response(
                conn,
                200,
                {"ok": True, "message": "Enrolled at page {}".format(page_id), "page_id": page_id},
            )
        else:
            http_response(conn, 400, {"ok": False, "message": msg, "page_id": page_id})
        return

    if method == "POST" and path == "/api/fingerprint/delete":
        try:
            data = json.loads(body) if body else {}
        except Exception:
            http_response(conn, 400, {"ok": False, "message": "Invalid JSON"})
            return
        try:
            page_id = int(data.get("page_id", -1))
        except (TypeError, ValueError):
            page_id = -1
        ok, msg = fp_delete_template(page_id)
        if ok:
            http_response(conn, 200, {"ok": True, "message": msg, "page_id": page_id})
        else:
            http_response(conn, 400, {"ok": False, "message": msg, "page_id": page_id})
        return

    if method == "POST" and path == "/api/fingerprint/reinit":
        if not FP_USE_SENSOR:
            http_response(
                conn,
                200,
                {
                    "ok": True,
                    "fingerprint_ready": False,
                    "message": "Fingerprint disabled in firmware (FP_USE_SENSOR=False)",
                },
            )
            return
        fp_uart.read()
        fp_ready = fp_verify_password()
        http_response(
            conn,
            200,
            {
                "ok": True,
                "fingerprint_ready": fp_ready,
                "message": "Sensor ready" if fp_ready else "Handshake failed — check UART, power, TX/RX; see ESP32 serial log",
            },
        )
        return

    http_response(conn, 404, {"ok": False, "message": "Not found"})


def start_server():
    global _last_motion_pin_high, _last_motion_log_ms
    addr = socket.getaddrinfo("0.0.0.0", HTTP_PORT)[0][-1]
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(addr)
    s.listen(3)
    s.settimeout(0.25)
    print("ESP32 HTTP server running on port", HTTP_PORT)

    while True:
        emergency, gas, smoke = check_emergency()
        if emergency:
            print("EMERGENCY! gas={}, smoke={} -> all doors OPEN".format(gas, smoke))

        m_now = motion_front_active()
        if m_now and not _last_motion_pin_high:
            now_ms = time.ticks_ms()
            if time.ticks_diff(now_ms, _last_motion_log_ms) >= MOTION_SERIAL_COOLDOWN_MS:
                print("Motion (front PIR) detected")
                _last_motion_log_ms = now_ms
        _last_motion_pin_high = m_now

        if fingerprint_access_detected():
            print("Fingerprint granted -> entrance open")
            grant_access()

        try:
            conn, _ = s.accept()
        except OSError:
            continue

        try:
            raw = conn.recv(2048)
            if raw:
                handle_request(conn, raw)
        except Exception as exc:
            http_response(conn, 500, {"ok": False, "message": "Server error: {}".format(exc)})
        finally:
            conn.close()


connect_wifi()
lock_all_doors()
if FP_USE_SENSOR:
    time.sleep_ms(1000)
    fp_ready = fp_verify_password()
else:
    fp_ready = False
    print("AS608 disabled (FP_USE_SENSOR=False)")
print("AS608 ready:", fp_ready)
start_server()
