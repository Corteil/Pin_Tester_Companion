"""Serial link to the Pin Tester badge app, and the protocol it speaks.

The badge app has a plain-text remote protocol on its USB serial port (one
command per line, see ``_handle_remote_command`` in the badge app's app.py).
It only listens while *Remote: ON* is showing on the app's STATUS tile.

This module has no GUI/CLI code: it opens the port, sends commands, and
parses replies into small objects. ``SerialLink`` talks to a real badge;
``pintester.sim.SimLink`` implements the same interface in-process.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover - reported when you try to connect
    serial = None
    list_ports = None

PROTOCOL = 3

LS_PINS = ("LS_A", "LS_B", "LS_C", "LS_D", "LS_E")
HS_PINS = ("HS_F", "HS_G", "HS_H", "HS_I")
ALL_PINS = LS_PINS + HS_PINS
HS_ALIASES = {"MOSI": "HS_F", "SCK": "HS_G", "CS": "HS_H", "DC": "HS_I"}

# PWM frequencies the badge app steps through (HS pins).
PWM_FREQS = (10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 20000, 50000, 100000)
BREATHE_MIN_S, BREATHE_MAX_S = 0.2, 10.0

# protocol word -> (human label, kinds it applies to)
MODES = {
    "INSPECT": ("Inspect only", ("ls", "hs")),
    "IN": ("Input", ("ls", "hs")),
    "LATCH": ("Latched input", ("ls", "hs")),
    "OFF": ("Output low", ("ls", "hs")),
    "ON": ("Output high", ("ls", "hs")),
    "AUTO": ("Auto", ("ls", "hs")),       # blinks; the badge calls this mode OUTPUT: AUTO
    "LEDOFF": ("LED off", ("ls",)),
    "LEDON": ("LED on", ("ls",)),
    "BREATHE": ("LED breathe", ("ls",)),
    "PWM": ("PWM", ("hs",)),
    "ANALOG": ("Analog in", ("hs",)),   # only HS pins on ports 4-6 (see Pin.adc)
}
ANALOG_FULL_MV = 3100   # the ADC's usable range on the badge (~0-3.1 V)
# Blink high/low range offered by the GUI. The badge keeps accurate time from about 250 ms per phase
# upwards; shorter is unreliable because its screen redraw blocks everything for most of each ~180 ms
# frame (measured: 250/250 ms gives 99-102% of the expected edges, 100/100 ms gives 0-21%, 20/20 ms
# 11-14%). The protocol itself accepts 10 ms - 60 s; for fast square waves on an HS pin use PWM.
BLINK_MIN_MS, BLINK_MAX_MS, BLINK_DEFAULT_MS = 250, 5000, 250


def fmt_ms(ms) -> str:
    return "{:g} s".format(ms / 1000) if ms >= 1000 else "{} ms".format(int(ms))


def dropdown_entries(kind: str, analog_ok: bool = False) -> List[tuple]:
    """(label, protocol word) for a pin's mode dropdown. Output high/low is a
    single "Output" entry and LED on/off a single "LED" entry (a toggle switch
    picks the state), so ON and LEDON are left out and OFF / LEDOFF stand for
    them (selecting one starts off/low, the safe state)."""
    entries = []
    for word in modes_for(kind):
        if word in ("ON", "LEDON") or (word == "ANALOG" and not analog_ok):
            continue
        label = {"OFF": "Output", "LEDOFF": "LED"}.get(word, MODES[word][0])
        entries.append((label, word))
    return entries


def modes_for(kind: str) -> List[str]:
    return [word for word, (_label, kinds) in MODES.items() if kind in kinds]


def pin_kind(name: str) -> str:
    return "ls" if name.upper().startswith("LS") else "hs"


def normalise_pin(name: str) -> str:
    """Accept LS_A / ls_a / ls-a / MOSI... and return the canonical name."""
    n = name.strip().upper().replace("-", "_")
    n = HS_ALIASES.get(n, n)
    if n not in ALL_PINS:
        raise ValueError("unknown pin {!r}, expected one of {}".format(name, ", ".join(ALL_PINS)))
    return n


class LinkError(Exception):
    """Could not talk to the badge (no port, no reply, or an ERR reply)."""


@dataclass
class Pin:
    name: str
    kind: str
    mode: str = "INSPECT"
    level: Optional[int] = None
    fields: Dict[str, object] = field(default_factory=dict)

    @property
    def adc(self) -> Optional[str]:
        """ADC channel name (e.g. "ADC1_CH2") if this pin can read analog."""
        value = self.fields.get("adc")
        return str(value) if value else None

    @property
    def volts(self) -> Optional[float]:
        mv = self.fields.get("mv")
        return None if mv is None else mv / 1000.0

    @property
    def mode_label(self) -> str:
        return MODES.get(self.mode, (self.mode,))[0]

    def describe(self) -> str:
        """One-line human description of the pin's current state."""
        f, lvl = self.fields, self.level
        hl = "-" if lvl is None else ("HIGH" if lvl else "LOW")
        if self.mode == "INSPECT":
            func = f.get("func")
            if func is None:
                return "reading..."
            if func == "led":
                return "LED mode (brightness can't be read)"
            if func == "pwm":
                return "driving a PWM output"
            if func == "periph":
                return "routed to peripheral #{}".format(f.get("sel", "?"))
            bits = ["{}".format("output" if f.get("dir") == "out" else "input"), hl]
            if f.get("pull") not in (None, "none"):
                bits.append("pull-" + str(f["pull"]))
            return ", ".join(bits)
        if self.mode == "ANALOG":
            if self.volts is None:
                return "no reading (ADC error?)"
            return "{:.3f} V  (raw {}, {})".format(self.volts, f.get("raw", "?"), self.adc or "ADC")
        if self.mode in ("IN",):
            return hl
        if self.mode == "LATCH":
            return "{}  (seen high: {}, low: {})".format(hl, bool(f.get("hi")), bool(f.get("lo")))
        if self.mode in ("OFF", "ON"):
            return "driving " + ("HIGH" if self.mode == "ON" else "LOW")
        if self.mode == "AUTO":
            if f.get("hi") is None:
                return "auto (blink), now " + hl
            return "auto: {} high / {} low  (now {})".format(fmt_ms(f["hi"]), fmt_ms(f["lo"]), hl)
        if self.mode in ("LEDOFF", "LEDON", "BREATHE"):
            out = f.get("out")
            txt = "set {}%".format(f.get("bright", "?"))
            if self.mode == "BREATHE":
                txt += ", cycle {:.1f}s".format(f.get("period", 0) / 1000.0)
            if out is not None:
                txt += ", output {}%".format(out)
            return txt
        if self.mode == "PWM":
            return "{} Hz, {}% duty".format(f.get("freq", "?"), f.get("duty", "?"))
        return hl


@dataclass
class Port:
    port: int = 1
    present: bool = False
    detect: Optional[bool] = None
    proto: int = 0
    frame_ms: Optional[int] = None     # badge's average time between app updates
    pins: List[Pin] = field(default_factory=list)

    def pin(self, name: str) -> Pin:
        name = normalise_pin(name)
        for p in self.pins:
            if p.name == name:
                return p
        raise KeyError(name)


@dataclass
class PortInfo:
    """Hexpansion EEPROM / I2C details for the current port (PORTINFO)."""
    port: int = 1
    present: bool = False
    detect: Optional[bool] = None
    eeprom_mounted: bool = False
    files: int = 0
    has_app: bool = False
    scan: str = "-"                    # "ok", "err" or "-" (never scanned)
    scan_error: Optional[str] = None
    name: Optional[str] = None
    vid: Optional[int] = None
    pid: Optional[int] = None
    uid: Optional[int] = None
    eeprom_addr: Optional[int] = None
    eeprom_size: Optional[int] = None
    page_size: Optional[int] = None
    header_version: Optional[str] = None
    i2c: List[int] = field(default_factory=list)

    @property
    def vid_pid(self) -> Optional[str]:
        if self.vid is None or self.pid is None:
            return None
        return "0x{:04X} / 0x{:04X}".format(self.vid, self.pid)

    @property
    def eeprom_desc(self) -> Optional[str]:
        if self.eeprom_addr is None:
            return None
        bits = ["0x{:02X}".format(self.eeprom_addr)]
        if self.eeprom_size:
            bits.append("{} KiB".format(self.eeprom_size // 1024) if self.eeprom_size >= 1024
                        else "{} B".format(self.eeprom_size))
        if self.page_size:
            bits.append("{}-byte pages".format(self.page_size))
        return ", ".join(bits)


def parse_portinfo(lines: List[str]) -> PortInfo:
    info = PortInfo()
    for line in lines:
        head, _, rest = line.partition(" ")
        if head == "PORTINFO":
            kv = parse_kv(rest.split())
            info.port = int(kv.get("port", 1))
            info.present = bool(kv.get("present", 0))
            det = kv.get("detect")
            info.detect = None if det is None else bool(det)
            info.eeprom_mounted = bool(kv.get("eeprom", 0))
            info.files = int(kv.get("files", 0))
            info.has_app = bool(kv.get("app", 0))
            info.scan = str(kv.get("scan", "-"))
            info.eeprom_addr = kv.get("addr")
            info.uid = kv.get("uid")
            info.eeprom_size = kv.get("size")
            info.page_size = kv.get("page")
            ver = kv.get("ver")
            info.header_version = None if ver is None else str(ver)
            info.vid, info.pid = kv.get("vid"), kv.get("pid")
        elif head == "NAME":
            info.name = rest.strip() or None
        elif head == "I2C":
            info.i2c = [int(x) for x in rest.split() if x.isdigit()]
        elif head == "SCANERR":
            info.scan_error = rest.strip()
    return info


def _value(text: str):
    if text == "-":
        return None
    try:
        return int(text)
    except ValueError:
        return text


def parse_kv(tokens) -> Dict[str, object]:
    out = {}
    for tok in tokens:
        if "=" in tok:
            key, _, val = tok.partition("=")
            out[key] = _value(val)
    return out


def parse_dump(lines: List[str]) -> Port:
    port = Port()
    for line in lines:
        parts = line.split()
        if not parts:
            continue
        if parts[0] == "DUMP":
            kv = parse_kv(parts[1:])
            port.port = int(kv.get("port", 1))
            port.present = bool(kv.get("present", 0))
            det = kv.get("detect")
            port.detect = None if det is None else bool(det)
            port.proto = int(kv.get("proto", 1))
            port.frame_ms = kv.get("frame")
        elif parts[0] == "PIN" and len(parts) >= 2:
            kv = parse_kv(parts[2:])
            mode = str(kv.pop("mode", "INSPECT"))
            level = kv.pop("level", None)
            kind = str(kv.pop("kind", pin_kind(parts[1])))
            port.pins.append(Pin(parts[1], kind, mode, level, kv))
    return port


# ---------------------------------------------------------------------------

_KNOWN_PREFIXES = ("PONG", "STATUS", "GET ", "DUMP", "PIN ", "INSPECT", "END", "OK", "ERR",
                   "PORT ", "PORTINFO", "NAME ", "I2C ", "SCANERR")


def terminator_for(cmd: str) -> Callable[[str], bool]:
    """Which reply line completes a request for ``cmd``."""
    parts = cmd.split()
    word = parts[0].upper() if parts else ""
    if word in ("DUMP", "INSPECT", "PORTINFO"):
        return lambda ln: ln == "END" or ln.startswith("ERR")
    if word == "PING":
        return lambda ln: ln.startswith("PONG") or ln.startswith("ERR")
    if word == "STATUS":
        return lambda ln: ln.startswith("STATUS") or ln.startswith("ERR")
    if word == "GET":
        return lambda ln: ln.startswith("GET ") or ln.startswith("ERR")
    if word == "PORT" and len(parts) == 1:
        return lambda ln: ln.startswith("PORT ") or ln.startswith("ERR")
    return lambda ln: ln.startswith("OK") or ln.startswith("ERR")


class Link:
    """Protocol helpers on top of ``request()``. Subclasses provide transport."""

    name = "link"

    # -- transport -----------------------------------------------------
    def request(self, cmd: str, timeout: float = 1.5) -> List[str]:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:
        pass

    # -- protocol ------------------------------------------------------
    def raw(self, cmd: str, timeout: float = 1.5) -> List[str]:
        """Send any protocol command; return its reply lines. ERR raises."""
        lines = self.request(cmd, timeout)
        for ln in lines:
            if ln.startswith("ERR"):
                raise LinkError(ln[3:].strip() or "badge returned an error")
        return lines

    def ping(self, timeout: float = 1.0) -> int:
        line = self.raw("PING", timeout)[-1]
        return int(parse_kv(line.split()[1:]).get("proto", 1))

    def status(self) -> Dict[str, Optional[int]]:
        line = self.raw("STATUS")[-1]
        return {k: (None if v == "None" else int(v)) for k, v in
                (tok.split("=", 1) for tok in line.split()[1:] if "=" in tok)}

    def dump(self) -> Port:
        return parse_dump(self.raw("DUMP"))

    def inspect(self, pin: Optional[str] = None) -> Dict[str, Dict[str, object]]:
        cmd = "INSPECT" if pin is None else "INSPECT " + normalise_pin(pin)
        result = {}
        for ln in self.raw(cmd):
            parts = ln.split()
            if parts and parts[0] == "INSPECT" and len(parts) >= 2:
                result[parts[1]] = parse_kv(parts[2:])
        return result

    def portinfo(self, scan: bool = False) -> PortInfo:
        """EEPROM / I2C details for the current port. scan=True first does a
        fresh I2C bus scan on the badge (probes every address on that bus)."""
        return parse_portinfo(self.raw("PORTINFO SCAN" if scan else "PORTINFO", 3.0 if scan else 1.5))

    def get(self, pin: str) -> Optional[int]:
        line = self.raw("GET " + normalise_pin(pin))[-1]
        val = line.split()[-1]
        return None if val == "None" else int(val)

    def set_mode(self, pin: str, mode: str, *args) -> None:
        mode = mode.upper()
        if mode not in MODES:
            raise ValueError("unknown mode {!r}, expected one of {}".format(mode, ", ".join(MODES)))
        self.raw(" ".join(["SET", normalise_pin(pin), mode] + [str(a) for a in args]))

    def port(self, n: Optional[int] = None) -> int:
        if n is None:
            return int(self.raw("PORT")[-1].split()[-1])
        if not 1 <= int(n) <= 6:
            raise ValueError("port must be 1-6")
        self.raw("PORT {}".format(int(n)))
        return int(n)

    def reset(self) -> None:
        self.raw("RESET")


# ---------------------------------------------------------------------------

def find_devices() -> List[tuple]:
    """Likely badge serial ports as (device, description). Espressif USB
    (VID 0x303A) first, then any other ttyACM/ttyUSB."""
    if list_ports is None:
        return []
    found = []
    for p in list_ports.comports():
        espressif = p.vid == 0x303A
        plausible = espressif or "ACM" in p.device or "USB" in p.device or "usbmodem" in p.device
        if plausible:
            desc = (p.description or p.device)
            found.append((0 if espressif else 1, p.device, desc))
    return [(dev, desc) for _rank, dev, desc in sorted(found)]


class SerialLink(Link):
    """Talks to a real badge over its USB serial port."""

    def __init__(self, device: Optional[str] = None, baud: int = 115200):
        if serial is None:
            raise LinkError("pyserial is not installed (pip install pyserial)")
        if device is None:
            devices = find_devices()
            if not devices:
                raise LinkError("no badge serial port found (is it plugged in?)")
            device = devices[0][0]
        self.name = device
        self._lock = threading.Lock()
        try:
            ser = serial.Serial()
            ser.port = device
            ser.baudrate = baud
            ser.timeout = 0.05
            ser.write_timeout = 1.0
            # Opening with DTR/RTS asserted can reset an ESP32 or drop it
            # into its bootloader, which would kill the running app.
            ser.dtr = False
            ser.rts = False
            ser.open()
        except (serial.SerialException, OSError) as exc:
            raise LinkError("cannot open {}: {}".format(device, exc)) from exc
        self._ser = ser
        # The badge reads commands line by line, so stray bytes already in
        # its input buffer would be glued onto our first command and
        # produce "ERR bad command". A bare newline ends (and discards) any
        # such partial line; its reply, if any, is thrown away here.
        try:
            ser.write(b"\n")
            ser.flush()
            time.sleep(0.6)
            ser.reset_input_buffer()
        except (serial.SerialException, OSError) as exc:
            raise LinkError("serial error: {}".format(exc)) from exc

    def close(self) -> None:
        with self._lock:
            try:
                self._ser.close()
            except Exception:
                pass

    def request(self, cmd: str, timeout: float = 1.5) -> List[str]:
        if "\n" in cmd or "\r" in cmd:
            raise ValueError("commands are a single line")
        done = terminator_for(cmd)
        text = cmd.strip()
        # Stray text can be queued in the badge's input (e.g. the badge's own
        # boot messages echoed back by something that opened the port, like
        # ModemManager). The badge answers each such line with "ERR bad
        # command: <that text>", and a fragment glued onto the front of our
        # command stops it running at all. So: ignore ERR replies that are
        # not about our own command, and if ours never ran, send it again.
        with self._lock:
            for _attempt in range(2):
                stale = False
                try:
                    self._ser.reset_input_buffer()
                    self._ser.write((text + "\n").encode())
                    self._ser.flush()
                    deadline = time.monotonic() + timeout
                    buf = b""
                    lines: List[str] = []
                    while time.monotonic() < deadline:
                        chunk = self._ser.read(self._ser.in_waiting or 1)
                        if not chunk:
                            continue
                        buf += chunk
                        while b"\n" in buf:
                            raw, _, buf = buf.partition(b"\n")
                            ln = raw.decode("utf-8", "replace").strip()
                            if not ln or not ln.startswith(_KNOWN_PREFIXES):
                                continue  # REPL noise / tracebacks / echoes
                            if ln.startswith("ERR bad command:"):
                                culprit = ln[len("ERR bad command:"):].strip()
                                if culprit != text:
                                    stale = True
                                    continue  # about stray text, not our command
                            lines.append(ln)
                            if done(ln):
                                return lines
                except (serial.SerialException, OSError) as exc:
                    raise LinkError("serial error: {}".format(exc)) from exc
                if not stale:
                    break
        raise LinkError("no reply from the badge -- is Pin Tester running with "
                        "Remote: ON (STATUS tile, press CONFIRM)?")
