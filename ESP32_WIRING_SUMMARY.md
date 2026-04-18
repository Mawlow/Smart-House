# ESP32 wiring summary

Quick reference for `esp32/main.py`. Full notes: [ESP32_WIRING.md](ESP32_WIRING.md).

## Pin map

| GPIO | Function |
|------|-----------|
| 23 | Relay — entrance lock |
| 22 | Relay — room lock |
| 21 | Relay — exit lock |
| 2 | LED 1 (+ 220Ω to anode, cathode GND) |
| 4 | LED 2 (+ 220Ω to anode, cathode GND) |
| 18 | Servo — entrance (PWM) |
| 19 | Servo — exit (PWM) |
| 34 | Gas sensor analog (ADC) |
| 35 | Smoke sensor analog (ADC) |
| 25 | Buzzer (PWM or active) |
| 32 | PIR motion (HC-SR501 OUT) |
| 17 | UART2 TX → fingerprint RX |
| 16 | UART2 RX ← fingerprint TX |

## Power / safety

- Do **not** power solenoid coils from the ESP32 3.3 V pin; use an external supply and relay modules with common GND.
- PIR and fingerprint: ensure **3.3 V–safe** signals to GPIOs (divider or 3.3 V supply if the module outputs 5 V).

## API (lights)

- `turn on light` / `turn off light` → LED on **GPIO2**
- `turn on light 2` / `turn off light 2` → LED on **GPIO4**

Same commands work from the admin Home Controls panel when the ESP32 URL and API key are set.
