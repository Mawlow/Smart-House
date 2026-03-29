"""
UART fingerprint test — matches esp32/main.py (UART2: TX=GPIO17, RX=GPIO16).

**JM-101B** (and most “AS608 optical” boards) use the **same protocol** as this script:
header `EF 01`, default **57600** baud, command **VfyPwd (0x13)**. If you only see **0x00** on RX,
the sensor is not replying — fix **3.3 V**, **GND**, and **crossed UART** (module TX→GPIO16,
module RX←GPIO17). Do not trust wire colors; swap the two data wires once if unsure.

Run in Thonny / REPL:
  import test_fingerprint
  test_fingerprint.run()
  # If RX is always 0x00, also try (rare): test_fingerprint.run(also_try_swapped_uart_pins=True)
  # Verify ESP32 UART (sensor unplugged; jumper GPIO17--GPIO16):
  #   test_fingerprint.uart_loopback()
  # If RX is all 0x00: unplug module TX from GPIO16, then:
  #   test_fingerprint.sniff_only(2000)

What it does:
  - For each baud rate: drain RX, optional listen for stray bytes, send VfyPwd (0x13), dump TX/RX hex.
  - Helps debug wrong TX/RX, baud, 5V vs 3.3V, dead module, or wrong UART pins.

Wiring reminder:
  ESP32 GPIO17 (TX2) -> sensor RX (often labelled Rxd / URXD)
  ESP32 GPIO16 (RX2) <- sensor TX (Txd / UTXD)
  GND <-> GND
  Sensor VCC per module (many are 3.3V tolerant; avoid 5V TTL into ESP32 RX without a divider).
"""

from machine import UART
import time

UART_NR = 2
PIN_TX = 17
PIN_RX = 16
BAUDS = (57600, 115200, 9600, 19200, 38400)

FP_HEADER = b"\xEF\x01"
FP_ADDR = b"\xFF\xFF\xFF\xFF"


def _hx(data):
    if not data:
        return "(empty)"
    return " ".join("{:02x}".format(b) for b in data)


def _checksum(packet_type, payload):
    total = packet_type + len(payload) + 2
    for x in payload:
        total += x
    return total & 0xFFFF


def build_vfy_pwd_packet(password_4bytes=b"\x00\x00\x00\x00"):
    cmd = 0x13
    payload = bytes([cmd]) + password_4bytes
    plen = len(payload) + 2
    ptype = 0x01
    chk = _checksum(ptype, payload)
    return (
        FP_HEADER
        + FP_ADDR
        + bytes([ptype])
        + plen.to_bytes(2, "big")
        + payload
        + chk.to_bytes(2, "big")
    )


def read_all(uart, total_ms, chunk_gap_ms=5):
    """Collect every byte available within total_ms."""
    buf = b""
    t0 = time.ticks_ms()
    while time.ticks_diff(time.ticks_ms(), t0) < total_ms:
        if uart.any():
            buf += uart.read()
        time.sleep_ms(chunk_gap_ms)
    return buf


def parse_response(buf):
    """If buffer contains a full AS608 reply starting with EF 01, return (ptype, payload) or None."""
    i = buf.find(FP_HEADER)
    if i < 0:
        return None
    b = buf[i:]
    if len(b) < 9:
        return None
    packet_len = int.from_bytes(b[7:9], "big")
    need = 9 + packet_len
    if len(b) < need:
        return None
    return b[6], b[9 : 9 + packet_len - 2]


def _all_zero(buf):
    return len(buf) > 0 and all(b == 0 for b in buf)


def print_all_zero_help():
    print(
        "\n  *** RX is only 0x00 — the module did NOT send a real AS608 reply (no EF 01 header). ***\n"
        "  Those bytes are usually **not** a valid packet: floating RX, wrong wiring, or bad baud/framing.\n"
        "  Checklist:\n"
        "    1) Sensor **power** (3.3V or 5V per board) and **GND** same as ESP32.\n"
        "    2) **Cross UART**: ESP **GPIO17 (TX2) -> sensor RX** / **GPIO16 (RX2) <- sensor TX**.\n"
        "       (If both lines go to the same label on a bad diagram, **swap the two data wires** once.)\n"
        "    3) Some boards need **3.3V logic** only on RX into the ESP32.\n"
        "    4) Confirm you are on **UART2** pins for your ESP32 devkit (some modules use different silkscreen).\n"
        "  **Split test:** unplug only the wire from **GPIO16** (module TX → ESP). Run:\n"
        "    >>> test_fingerprint.sniff_only(2000)\n"
        "    If RX goes **quiet** (no bytes), the zeros were coming **through that wire** (recheck TX/RX labels).\n"
        "    If you still get **0x00** with GPIO16 open, suspect **floating RX** or UART noise — fix GND/short wires.\n"
        "  Verify the ESP32 UART itself (sensor **disconnected** from 16/17):\n"
        "    >>> test_fingerprint.uart_loopback()\n"
        "    (Jumper wire **GPIO17 to GPIO16** only for that test, then remove.)\n"
    )


def sniff_only(total_ms=2000, baud=57600, tx_pin=PIN_TX, rx_pin=PIN_RX):
    """
    Listen on UART2 only (no TX to sensor). Use with **module TX unplugged from GPIO16** to see
    whether all-zero RX was coming from the module path or from a floating RX line.
    """
    print("\n=== UART2 RX sniff only ({} ms, {} baud) ===\n".format(total_ms, baud))
    u = UART(UART_NR, baudrate=baud, tx=tx_pin, rx=rx_pin)
    time.sleep_ms(150)
    u.read()
    buf = read_all(uart=u, total_ms=total_ms)
    if not buf:
        print("  RX: (nothing) — line quiet or not connected.\n")
    else:
        print("  RX ({} bytes):".format(len(buf)), _hx(buf[:64]) + (" ..." if len(buf) > 64 else ""))
        if _all_zero(buf):
            print("  All 0x00 — often floating RX or noise; check GND, wire length, pull-ups.\n")
        else:
            print("  Non-zero data present (unexpected if sensor TX is unplugged).\n")


def uart_loopback(tx_pin=PIN_TX, rx_pin=PIN_RX, baud=57600):
    """
    Proves UART2 TX/RX on the ESP32 work. **Unplug the fingerprint from GPIO16 and GPIO17 first.**
    Put one jumper: GPIO17 <-> GPIO16. Then run this in REPL.
    """
    print("\n=== UART loopback (sensor DISCONNECTED; GPIO{} jumpered to GPIO{}) ===\n".format(tx_pin, rx_pin))
    u = UART(UART_NR, baudrate=baud, tx=tx_pin, rx=rx_pin)
    time.sleep_ms(100)
    u.read()
    msg = b"LOOP"
    u.write(msg)
    time.sleep_ms(80)
    got = u.read()
    print("  Sent:", _hx(msg))
    print("  Got: ", _hx(got) if got else "(empty)")
    if got == msg:
        print("  >>> OK: UART echo works — problem is wiring/power/baud to the fingerprint module. <<<\n")
    elif got:
        print("  Partial/garbled — check single jumper only, same baud, short wires.\n")
    else:
        print("  No echo — no jumper, wrong pins, or UART conflict on this board.\n")


def run(listen_ms=400, answer_ms=1800, verbose=True, also_try_swapped_uart_pins=False):
    print("\n=== Fingerprint UART test (AS608 / JM-101B class) ===\n")
    pkt = build_vfy_pwd_packet(b"\x00\x00\x00\x00")
    print("VfyPwd packet (hex):", _hx(pkt))
    print("")

    uart = UART(UART_NR, baudrate=BAUDS[0], tx=PIN_TX, rx=PIN_RX)

    any_ok = False
    had_rx_bytes = False
    rx_only_zeros = True

    pin_sets = ((PIN_TX, PIN_RX, "normal TX=17 RX=16"),)
    if also_try_swapped_uart_pins:
        pin_sets = (
            (PIN_TX, PIN_RX, "normal TX=17 RX=16"),
            (PIN_RX, PIN_TX, "SWAPPED software TX=16 RX=17 (if your harness is crossed)"),
        )

    for txp, rxp, label in pin_sets:
        print("[ Pin mode:", label, "]\n")
        for baud in BAUDS:
            print("--- Baud", baud, "---")
            try:
                uart.init(baudrate=baud, tx=txp, rx=rxp)
            except Exception as exc:
                print("  UART init failed:", exc)
                continue

            time.sleep_ms(200)
            junk = uart.read()
            if junk:
                had_rx_bytes = True
                print("  RX junk after init:", _hx(junk))
                if not _all_zero(junk):
                    rx_only_zeros = False

            if listen_ms > 0:
                sniff = read_all(uart, listen_ms)
                if sniff:
                    had_rx_bytes = True
                    print("  RX sniff {} ms:".format(listen_ms), _hx(sniff))
                    if not _all_zero(sniff):
                        rx_only_zeros = False

            uart.read()
            uart.write(pkt)
            if verbose:
                print("  TX sent, waiting up to {} ms for reply...".format(answer_ms))

            raw = read_all(uart, answer_ms)
            if not raw:
                print("  RX: (nothing) — check TX/RX swap, GND, power, 3.3V logic, baud.")
            else:
                had_rx_bytes = True
                print("  RX raw ({} bytes):".format(len(raw)), _hx(raw))
                if not _all_zero(raw):
                    rx_only_zeros = False
                parsed = parse_response(raw)
                if parsed is None:
                    print("  Parse: no complete EF 01 ... packet (wrong baud, noise, or not AS608).")
                else:
                    ptype, payload = parsed
                    print("  Parse: packet_type=0x{:02x} payload_len={}".format(ptype, len(payload)))
                    if payload:
                        ack = payload[0]
                        print("  First payload byte (ACK): 0x{:02x}".format(ack))
                        if ack == 0x00:
                            print("  >>> SUCCESS: password verify OK (module alive at this baud). <<<")
                            any_ok = True
                        elif ack == 0x01:
                            print("  Error 0x01: packet receive error")
                        elif ack == 0x13:
                            print("  Error 0x13: password wrong (try your configured pwd bytes)")
                        elif ack == 0x18:
                            print("  Error 0x18: flash write error")
                        else:
                            print("  See AS608 datasheet for other ACK codes.")
            print("")
        print("")

    if not any_ok:
        if had_rx_bytes and rx_only_zeros:
            print_all_zero_help()
        elif not had_rx_bytes:
            print(
                "\n  HINT: UART never read any bytes — sensor **TX** may not reach **GPIO16**, "
                "module unpowered, or wrong UART pins for your board.\n"
                "  Run **test_fingerprint.uart_loopback()** with GPIO17 jumpered to GPIO16 (sensor unplugged) "
                "to verify the ESP32 UART.\n"
            )
    print("=== Done ===\n")


if __name__ == "__main__":
    run()
