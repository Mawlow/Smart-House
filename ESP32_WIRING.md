# ESP32 Wiring Guide (MicroPython)

This wiring plan matches `esp32/main.py`.

## Important Power Notes

- **Do NOT power solenoid locks from ESP32 3.3V/5V pin.**
- Use an external supply for lock coils (commonly 12V, based on your lock model).
- Use a **relay module with transistor + optocoupler**.
- Connect all grounds together:
  - ESP32 GND
  - Relay module GND
  - External power supply GND

## Pin Mapping Used

- `GPIO23` -> Relay IN1 (Entrance lock)
- `GPIO22` -> Relay IN2 (Room lock)
- `GPIO21` -> Relay IN3 (Exit lock)
- `GPIO2`  -> LED 1 (main light output)
- `GPIO4`  -> LED 2 (second light / indicator)
- `GPIO18` -> Servo 1 signal (PWM) — paired with **entrance** solenoid (`GPIO23` relay)
- `GPIO19` -> Servo 2 signal (PWM) — paired with **exit** solenoid (`GPIO21` relay)
- `GPIO34` -> Gas sensor analog output (ADC)
- `GPIO35` -> Smoke sensor analog output (ADC)
- `GPIO25` -> Gas/smoke **alarm buzzer** (passive piezo uses **PWM** in firmware; active buzzer uses steady HIGH — see buzzer notes below)
- `GPIO32` -> HC-SR501 digital OUT (front / entrance PIR security)
- `GPIO17` -> Fingerprint TX line (to module RX) via UART2
- `GPIO16` -> Fingerprint RX line (to module TX) via UART2

## Relay + Solenoid Wiring (for each door)

For each door lock:

1. External power `+` -> Solenoid `+`
2. Solenoid `-` -> Relay `COM`
3. Relay `NO` -> External power `-`

Using `NO` means lock is off by default and energizes on command.  
If your lock behavior is opposite, switch `NO`/`NC` and adjust logic in code.

## LED wiring (two outputs)

**LED 1 (main)**

- `GPIO2` -> 220Ω resistor -> LED anode (+)
- LED cathode (-) -> GND

**LED 2 (secondary)**

- `GPIO4` -> 220Ω resistor -> second LED anode (+)
- LED cathode (-) -> GND

If controlling a bigger lamp, use a relay or MOSFET driver instead of driving the load from GPIO.

## Servo wiring (two locks)

Each **solenoid lock** that uses a servo gets its own signal pin; both servos share the same **5V supply rail** and **GND** with the ESP32 (use an external 5V supply if you have two servos under load — do not rely on the ESP32 3.3V regulator for motor current).

| Lock        | Relay GPIO | Servo signal | Role in code        |
|------------|------------|--------------|---------------------|
| Entrance   | `GPIO23`   | `GPIO18`     | `grant_access`, `open door` / `close door`, emergency |
| Room       | `GPIO22`   | *(none)*     | Relay only (`open` / `close room door`) |
| Exit       | `GPIO21`   | `GPIO19`     | `open` / `close exit door`, `open_all_doors` / `lock_all_doors`, emergency |

Mechanical install: mount each servo so its arm/slider matches **locked** at `SERVO_LOCK_ANGLE` (0°) and **unlocked** at `SERVO_UNLOCK_ANGLE` (95° in `main.py`). If your hardware needs different travel, edit `SERVO_UNLOCK_ANGLE` / `SERVO_LOCK_ANGLE` in `esp32/main.py`.

## Gas / smoke sensor wiring

### MQ-135 (4-pin breakout: VCC, GND, AO, DO)

Typical labels on the PCB:

| Pin | Connect to | Notes |
|-----|------------|--------|
| **VCC** | **5V** on ESP32 (or the voltage your board specifies) | Heater needs 5V on most MQ breakout boards. |
| **GND** | **GND** (common with ESP32, relays, etc.) | |
| **AO** | ESP32 **`GPIO34`** (gas) or **`GPIO35`** (smoke) | Analog level 0…~3V — matches what `esp32/main.py` reads with `ADC`. This is what **`GAS_THRESHOLD` / `SMOKE_THRESHOLD`** apply to. If readings stay **~4095** (max), the pin is often **floating** or you wired **DO** / **VCC** by mistake — fix before deploy or the firmware will treat it as **emergency** and open all doors. |
| **DO** | *(leave unconnected for this project)* | Digital output from the on-board comparator (LM393). Goes HIGH/LOW when analog crosses the **small blue pot** threshold on the module. **`main.py` does not use DO** — it only uses **AO** + software thresholds. You can leave **DO** floating or tie it nowhere. If you ever wire **DO** to a GPIO, use **3.3V-safe** logic (many modules output 0/5V on DO — check with a meter; add a divider if needed). |

Warm-up: MQ sensors need **~1–2 minutes** after power-on before readings stabilize.

Adjust sensitivity in software in `esp32/main.py`:

- `GAS_THRESHOLD` (reading on `GPIO34`)
- `SMOKE_THRESHOLD` (reading on `GPIO35`)

**Emergency behavior:** If **either** reading is **≥** its threshold, the firmware calls **`open_all_doors()`** (all three relays plus entrance and exit servos) and turns **`GPIO25` buzzer ON**. When readings drop **below** both thresholds, the buzzer turns **OFF**. That runs on every main-loop pass, at the start of each HTTP request, again after each `POST /api/control` (so a remote “lock” cannot override an active alarm), and the same logic feeds `GET /api/ping` (`"emergency": true`). Tune thresholds so normal air does not trip; allow **~1–2 minutes** warm-up after power-on before trusting readings.

**Buzzer (`GPIO25`):** Firmware defaults to **`BUZZER_MODE = "pwm"`** (default **~4 kHz** in `main.py`) so a **passive** piezo buzzer makes sound; a steady GPIO HIGH does nothing on passive parts. If it still sounds weak, try **`BUZZER_PWM_FREQ`** between **~2500–4500 Hz** (resonance varies by part). **`BUZZER_PWM_DUTY`** should stay near **half of 1023** (~512) for a passive buzzer—nearly full duty is mostly DC and gets quieter. For **more volume** than 3.3 V GPIO can give, use **5 V** and an **NPN transistor** (GPIO → resistor → base, emitter GND, buzzer + to supply, buzzer − to collector). If your module is an **active** buzzer (built-in oscillator), set **`BUZZER_MODE = "active"`** in `esp32/main.py`. Wire **signal** → `GPIO25`, **GND** → GND. If sound is inverted (only works when GPIO is LOW), set **`BUZZER_ACTIVE_HIGH = False`** in **active** mode. Set **`USE_BUZZER_ALARM = False`** if no buzzer. Run **`test_sensors.test_buzzer()`** after changing mode to verify wiring.

You can also tweak the **AO vs DO** behavior on the module: the pot adjusts when **DO** flips; **AO** still reflects the raw analog level used by the ESP32.

### Other analog gas/smoke modules

Same idea: **analog out → `GPIO34` (gas) or `GPIO35` (smoke)`**, **VCC/GND** as required by the module.

## HC-SR501 PIR Motion Sensor (front security)

Mount the sensor facing the approach to the entrance. When it sees movement, the ESP32 reports `motion: true` on `GET /api/ping`; the Flask server turns each **new** motion event into an admin notification.

Wiring:

- HC-SR501 `VCC` -> **5V** (or **3.3V** if your module works at 3.3V; check the board)
- HC-SR501 `GND` -> common GND with ESP32
- HC-SR501 `OUT` -> ESP32 `GPIO32`

**Logic level:** Many HC-SR501 boards output ~3.3V HIGH when motion is detected even when powered from 5V, but some output ~5V. If `OUT` measures above 3.3V, use a **voltage divider** (e.g. 10k + 20k) or power the PIR at 3.3V so `GPIO32` never sees more than 3.3V.

### Why it can feel “not sensitive” (even when the sensor works)

1. **Two potentiometers on the board (not optional)**  
   - **Sensitivity (SENS):** Turn it toward **maximum sensitivity** for testing (direction varies by PCB; try both extremes and pick the side that triggers from farther / smaller movement). If this pot is low, you must be very close or move a lot.  
   - **Time delay:** This is **how long `OUT` stays HIGH** after one trigger. If it is **turned up**, the pin can stay HIGH for **tens of seconds**. During that whole time you will **not** get a second admin alert (see below). For testing, turn delay **down** (short pulse) so each wave of motion can produce a new LOW → HIGH cycle.

2. **H / L jumper (if your board has one)**  
   - **H (repeat / retrigger):** Output can stay active while motion continues — usually feels more responsive.  
   - **L (single trigger):** One pulse per event — can feel “dead” until the internal timer finishes.  
   Prefer **H** for security-style “always know when someone is moving.”

3. **Warm-up:** After power-on, wait **~30–60 seconds** before judging sensitivity; the PIR needs to settle.

4. **Lens and aim:** The Fresnel lens must face the area of interest; side-on or blocked plastic reduces range. Indoor drafts / heat sources near the sensor can also confuse it.

5. **How the admin portal uses motion (software, not more “gain”)**  
   The server polls the ESP32 on an interval (default **~0.65 s**, configurable as **Motion poll sec** in Admin → ESP32 Settings). It adds a **Security Alert only when `motion` goes from false → true** (one alert per **new** detection burst). While `OUT` stays HIGH, **no extra alerts** — that is intentional to avoid spam. If the **time delay** pot keeps `OUT` HIGH for a long time, you will see **few alerts** even though the PIR is “seeing” you; **shorten the delay pot** or walk out of range until the output drops LOW, then trigger again.

6. **Self-test:** Run `test_sensors.test_pir()` (or `run_all()`) and watch `motion_pin=1` while you move; if that only flickers briefly, fix **pots / jumper / aim** before blaming the Flask app.

## Fingerprint sensor wiring (UART — AS608 / **JM-101B** / ZFM-style)

**JM-101B** modules are **AS608-class**: same serial framing (`EF 01 …`), default baud **57600**, and the same command set our `main.py` uses. If `test_fingerprint.py` shows **only `00` bytes** on RX, the problem is almost always **power, GND, or TX/RX crossed wrong** — not “wrong brand.”

### Rule (any module label)

- **Module TX** (data *out* from the sensor) → ESP32 **`GPIO16`** (UART2 **RX**)
- **Module RX** (data *in* to the sensor) → ESP32 **`GPIO17`** (UART2 **TX**)
- **GND** common with ESP32
- **VCC** = **3.3 V** unless your board’s datasheet explicitly allows 5 V logic on **both** sides (ESP32 GPIO is **3.3 V**-tolerant only)

### JM-101B ribbon / 6-pin harness (colors **vary by supplier**)

Many bundles use something like:

| Wire / label (example) | Role | Connect to ESP32 |
|------------------------|------|------------------|
| **3.3V** / **VCC** / **+** | Power | 3.3 V |
| **GND** | Ground | GND |
| **UTX** / **TX** / **Dout** | Module transmits | **GPIO16** (RX2) |
| **URX** / **RX** / **Din** | Module receives | **GPIO17** (TX2) |

Do **not** match wire **colors** to ESP32 “TX/RX” labels — match **function**: **always cross** UART (ESP **TX** → module **RX**, ESP **RX** ← module **TX**). If in doubt, **swap the two data wires** and run `test_fingerprint.py` again.

Optional **WAKE** / **IRQ** pins on some boards can be left unconnected unless the datasheet says otherwise.

`esp32/main.py` uses AS608-style flow (`GenImg` → `Img2Tz` → `Search`) and opens the entrance when a stored template matches.

Notes:

- Enroll fingerprints into the module’s flash first (vendor software, Arduino **Adafruit Fingerprint** examples, etc.).
- Default module password in code is `0x00000000` (change in `main.py` / `test_fingerprint.py` if you set another).

## Network Integration

Admin portal stores ESP32 URL and API key.

ESP32 exposes:
- `GET /api/ping` (JSON includes `gas`, `smoke`, `emergency`, `motion`)
- `POST /api/control` with JSON body:
  - `{"command":"turn on light"}` / `{"command":"turn off light"}` (GPIO2)
  - `{"command":"turn on light 2"}` / `{"command":"turn off light 2"}` (GPIO4)
  - `{"command":"open door"}` or `{"command":"open entrance door"}`
  - `{"command":"open room door"}`
  - `{"command":"open exit door"}`
  - `{"command":"close door"}` or `{"command":"close entrance door"}`
  - `{"command":"close room door"}`
  - `{"command":"close exit door"}`
  - `{"command":"grant_access"}`
  - `{"command":"open_all_doors"}`
  - `{"command":"lock_all_doors"}` or `{"command":"close_all_doors"}`

Use same API key on both sides.

## Bench test (all wiring)

After uploading `esp32/test_sensors.py`, in the MicroPython REPL run:

```text
import test_sensors
test_sensors.run_all()
```

That script exercises **both LEDs (GPIO2 / GPIO4)**, **buzzer GPIO25**, **three relays**, **two servos**, **gas/smoke ADC**, **PIR GPIO32**, and optionally **AS608 UART** (`TEST_FINGERPRINT`) and **WiFi** (`TEST_WIFI`). It ends in a **safe locked** state. Keep hands clear of servos and solenoids while it runs.

Fingerprint only (verbose UART + hex dumps): upload `esp32/test_fingerprint.py` and run:

```text
import test_fingerprint
test_fingerprint.run()
```
