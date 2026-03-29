"""
Hardware test for SmartHouse ESP32 — pin map must match main.py.

Usage (Thonny / REPL):
  import test_sensors
  test_sensors.run_all()

Optional: set TEST_FINGERPRINT = False below to skip AS608 (faster if not wired).
Relays click, servos move, LED blinks — stand clear of moving parts.
"""

from machine import ADC, PWM, Pin, UART
import time

# -----------------------------------------------------------------------------
# Pin map — keep aligned with esp32/main.py
# -----------------------------------------------------------------------------
RELAY_ENTRANCE = Pin(23, Pin.OUT, value=1)
RELAY_ROOM = Pin(22, Pin.OUT, value=1)
RELAY_EXIT = Pin(21, Pin.OUT, value=1)
LED_LIGHT = Pin(2, Pin.OUT, value=0)

SERVO_ENTRANCE_PWM = PWM(Pin(18), freq=50)
SERVO_EXIT_PWM = PWM(Pin(19), freq=50)
SERVO_UNLOCK = 95
SERVO_LOCK = 0

GAS_ADC = ADC(Pin(34))
SMOKE_ADC = ADC(Pin(35))
GAS_ADC.atten(ADC.ATTN_11DB)
SMOKE_ADC.atten(ADC.ATTN_11DB)

MOTION_FRONT = Pin(32, Pin.IN, Pin.PULL_DOWN)

BUZZER_ALARM_PIN = Pin(25, Pin.OUT, value=0)

FP_UART_NR = 2
FP_UART_TX = 17
FP_UART_RX = 16
FP_UART_BAUDS = (57600, 115200, 9600)
FP_HEADER = b"\xEF\x01"
FP_ADDRESS = b"\xFF\xFF\xFF\xFF"

GAS_THRESHOLD = 2600
SMOKE_THRESHOLD = 2600

# Set False to skip fingerprint UART test
TEST_FINGERPRINT = True

# Set True to try WiFi using same placeholders as main.py (optional)
TEST_WIFI = False
WIFI_SSID = "YOUR_WIFI_NAME"
WIFI_PASSWORD = "YOUR_WIFI_PASSWORD"


def _relay_open(pin):
    pin.value(0)


def _relay_lock(pin):
    pin.value(1)


def set_servo_angle(pwm, angle):
    angle = max(0, min(180, int(angle)))
    duty_us = int(500 + (angle / 180.0) * 2000)
    duty = int((duty_us / 20000) * 1023)
    pwm.duty(duty)


def safe_lock_all():
    """Relays locked (active-high idle), servos at lock angle, LED off."""
    set_servo_angle(SERVO_ENTRANCE_PWM, SERVO_LOCK)
    _relay_lock(RELAY_ENTRANCE)
    _relay_lock(RELAY_ROOM)
    _relay_lock(RELAY_EXIT)
    time.sleep_ms(120)
    set_servo_angle(SERVO_EXIT_PWM, SERVO_LOCK)
    LED_LIGHT.value(0)
    BUZZER_ALARM_PIN.value(0)


def test_led():
    print("[LED GPIO2] Blink 6x — watch the indicator / lamp driver.")
    for i in range(6):
        LED_LIGHT.value(1)
        time.sleep_ms(200)
        LED_LIGHT.value(0)
        time.sleep_ms(200)
    print("  OK (visual check)")


def test_buzzer():
    print("[Buzzer GPIO25] Alarm pin HIGH ~0.4s (gas/smoke emergency uses same pin in main.py).")
    BUZZER_ALARM_PIN.value(1)
    time.sleep_ms(400)
    BUZZER_ALARM_PIN.value(0)
    print("  OK (listen / meter)")


def test_relays():
    print("[Relays] Active-low: click each channel ~0.35s ON then OFF.")
    order = (
        ("Entrance GPIO23", RELAY_ENTRANCE),
        ("Room GPIO22", RELAY_ROOM),
        ("Exit GPIO21", RELAY_EXIT),
    )
    for name, pin in order:
        print("  OPEN", name)
        _relay_open(pin)
        time.sleep_ms(350)
        print("  LOCK", name)
        _relay_lock(pin)
        time.sleep_ms(200)
    print("  OK (listen/feel each relay)")


def test_servos():
    print("[Servos] Entrance GPIO18 then Exit GPIO19 — lock -> unlock -> lock.")
    for label, pwm in (("Entrance", SERVO_ENTRANCE_PWM), ("Exit", SERVO_EXIT_PWM)):
        print(" ", label, "lock")
        set_servo_angle(pwm, SERVO_LOCK)
        time.sleep_ms(400)
        print(" ", label, "unlock (~95 deg)")
        set_servo_angle(pwm, SERVO_UNLOCK)
        time.sleep_ms(800)
        print(" ", label, "lock")
        set_servo_angle(pwm, SERVO_LOCK)
        time.sleep_ms(400)
    print("  OK (watch both arms)")


def test_adc():
    print("[ADC] Gas GPIO34 / Smoke GPIO35 — 8 samples (warm up MQ ~1–2 min for stable).")
    smoke_vals = []
    gas_vals = []
    for i in range(8):
        g = GAS_ADC.read()
        s = SMOKE_ADC.read()
        gas_vals.append(g)
        smoke_vals.append(s)
        gas_alm = g >= GAS_THRESHOLD
        smk_alm = s >= SMOKE_THRESHOLD
        print(
            "  sample {:d}: gas={} smoke={} | emergency if gas>={} or smoke>={}: gas_alm={} smk_alm={}".format(
                i + 1, g, s, GAS_THRESHOLD, SMOKE_THRESHOLD, gas_alm, smk_alm
            )
        )
        time.sleep_ms(400)
    print("  OK (values should change if sensors are powered and analog wired)")

    ADC_SAT = 4000
    sat_gas = sum(1 for v in gas_vals if v >= ADC_SAT)
    sat_smoke = sum(1 for v in smoke_vals if v >= ADC_SAT)
    if sat_gas >= 5 or sat_smoke >= 5:
        print(
            "  ! CRITICAL: ADC pinned near max (~4095). On ESP32 this usually means AO is FLOATING (not connected), "
            "wired to the wrong pad (e.g. **DO** or **VCC** instead of **AO**), or a broken joint. "
            "It is NOT clean air — **main.py will see emergency and keep opening all doors** until fixed. "
            "Use each sensor’s **AO** -> GPIO34 (gas) / GPIO35 (smoke), **GND** common, correct **VCC**."
        )
    elif all(v == 0 for v in smoke_vals):
        print(
            "  ! HINT: smoke stuck at 0 — AO probably not on GPIO35, sensor unpowered, or AO shorted to GND. "
            "Check smoke module VCC/GND/AO -> GPIO35 (only pins 32–39 are ADC1 inputs on many ESP32 boards)."
        )
    elif max(smoke_vals) - min(smoke_vals) < 15:
        print("  HINT: smoke barely changes — verify second MQ (or smoke) board AO on GPIO35 after warm-up.")

    if sat_gas < 5 and max(gas_vals) - min(gas_vals) < 30:
        print("  HINT: gas is flat — normal if air is clean; blow gently near sensor (not the heater) to see a bump.")


def test_pir():
    print("[PIR GPIO32] Move in front of HC-SR501 for ~4s (poll 20x).")
    highs = 0
    for i in range(20):
        v = MOTION_FRONT.value()
        if v:
            highs += 1
        print("  {:2d}: motion_pin={} (1=motion)".format(i + 1, v))
        time.sleep_ms(200)
    print("  HIGH count:", highs, "/", 20)
    if highs == 0:
        print(
            "  HINT: never HIGH — check HC-SR501 OUT -> GPIO32, GND common, 3.3V-safe OUT level, "
            "and try moving during the test."
        )
    elif highs == 20:
        print(
            "  ! HINT: pin always HIGH — often wrong wiring (e.g. 5V into GPIO), OUT shorted to VCC, "
            "or jumper on sensor. Idle should be LOW; only HIGH when motion (until PIR timeout). "
            "Try disconnecting OUT from GPIO32: if REPL/read still HIGH, board/pin issue; if LOW, sensor/wiring."
        )
    else:
        print("  Looks OK (pin toggled during the window).")


def _fp_checksum(packet_type, payload):
    total = packet_type + len(payload) + 2
    for b in payload:
        total += b
    return total & 0xFFFF


def _fp_read(uart, timeout_ms):
    started = time.ticks_ms()
    buf = b""
    while time.ticks_diff(time.ticks_ms(), started) < timeout_ms:
        if uart.any():
            buf += uart.read()
            if len(buf) >= 9:
                plen = int.from_bytes(buf[7:9], "big")
                need = 9 + plen
                if len(buf) >= need:
                    return buf[6], buf[9 : 9 + plen - 2]
        time.sleep_ms(5)
    return None, b""


def _fp_send(uart, cmd, params=b"", timeout_ms=900):
    payload = bytes([cmd]) + params
    length = len(payload) + 2
    pt = 0x01
    chk = _fp_checksum(pt, payload)
    pkt = (
        FP_HEADER
        + FP_ADDRESS
        + bytes([pt])
        + length.to_bytes(2, "big")
        + payload
        + chk.to_bytes(2, "big")
    )
    uart.read()
    uart.write(pkt)
    return _fp_read(uart, timeout_ms)


def test_fingerprint():
    if not TEST_FINGERPRINT:
        print("[Fingerprint] Skipped (TEST_FINGERPRINT=False)")
        return
    print("[AS608 UART2] TX17 / RX16 — VfyPwd default password.")
    uart = UART(FP_UART_NR, baudrate=FP_UART_BAUDS[0], tx=FP_UART_TX, rx=FP_UART_RX)
    ok = False
    for baud in FP_UART_BAUDS:
        try:
            uart.init(baudrate=baud, tx=FP_UART_TX, rx=FP_UART_RX)
        except Exception as exc:
            print("  baud", baud, "init err:", exc)
            continue
        time.sleep_ms(250)
        uart.read()
        for _ in range(3):
            pt, payload = _fp_send(uart, 0x13, b"\x00\x00\x00\x00", timeout_ms=1600)
            if pt == 0x07 and len(payload) > 0 and payload[0] == 0x00:
                print("  OK at baud", baud)
                ok = True
                break
            time.sleep_ms(100)
        if ok:
            break
        print("  No ACK at baud", baud, "last_type=", pt)
    if not ok:
        print(
            "  FAIL — no UART reply (last_type=0 usually means timeout / no bytes). "
            "Swap ESP TX<->sensor RX and ESP RX<->sensor TX, common GND, 3.3V logic, power, "
            "or set TEST_FINGERPRINT=False if module not installed."
        )


def test_wifi():
    if not TEST_WIFI:
        print("[WiFi] Skipped (set TEST_WIFI=True and credentials)")
        return
    try:
        import network
    except ImportError:
        print("[WiFi] network module missing")
        return
    if WIFI_SSID in ("", "YOUR_WIFI_NAME"):
        print("[WiFi] Set WIFI_SSID / WIFI_PASSWORD in test_sensors.py")
        return
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    wlan.connect(WIFI_SSID, WIFI_PASSWORD)
    for _ in range(25):
        if wlan.isconnected():
            print("  OK", wlan.ifconfig())
            return
        time.sleep(0.4)
    print("  FAIL status=", wlan.status())


def run_all():
    print("\n======== SmartHouse ESP32 hardware test ========\n")
    safe_lock_all()
    time.sleep_ms(300)

    test_led()
    safe_lock_all()

    test_buzzer()
    safe_lock_all()

    test_relays()
    safe_lock_all()

    test_servos()
    safe_lock_all()

    test_adc()

    test_pir()

    test_fingerprint()

    test_wifi()

    safe_lock_all()
    print("\n======== Done. All outputs returned to safe (locked) state. ========\n")


if __name__ == "__main__":
    run_all()
