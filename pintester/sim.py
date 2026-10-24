"""An in-process fake badge that speaks the same remote protocol.

Lets you try the CLI and GUI (``--sim``) and run the tests without
hardware. It mirrors the replies of the real app's
``_handle_remote_command`` closely enough that the parsing code is the same.
"""

from __future__ import annotations

import math
import time
from typing import Dict, List, Optional

from .link import (ALL_PINS, HS_PINS, MODES, PWM_FREQS, Link, LinkError, PROTOCOL,
                   normalise_pin, pin_kind)


class SimBadge:
    def __init__(self):
        self.port = 1
        self.populated = {1, 2}          # ports with a "hexpansion" fitted
        self.i2c_addresses = {}          # port -> list of addresses; default [0x50]
        self.state: Dict[str, dict] = {}
        self.reset()

    # -- state ---------------------------------------------------------
    def _default_mode(self) -> str:
        return "INSPECT" if self.port in self.populated else "IN"

    def reset(self) -> None:
        self.state = {
            name: {"mode": self._default_mode(), "user": 0, "bright": 50, "period": 2000,
                   "freq": 1000, "duty": 50}
            for name in ALL_PINS
        }

    def _level(self, name: str, st: dict):
        t = time.monotonic()
        mode = st["mode"]
        if mode in ("INSPECT", "IN", "LATCH"):
            # a gently wobbling "wired up" input so the UI has something to show
            return 1 if math.sin(t * 0.8 + ALL_PINS.index(name)) > 0.2 else 0
        if mode == "OFF":
            return 0
        if mode == "ON":
            return 1
        if mode == "AUTO":
            return int(t * 4) % 2
        if mode == "LEDOFF":
            return 0
        if mode == "LEDON":
            return st["bright"] * 255 // 100
        if mode == "BREATHE":
            phase = (t * 1000 % st["period"]) / st["period"]
            tri = 1 - abs(2 * phase - 1)
            return int(tri * st["bright"] * 255 / 100)
        if mode == "PWM":
            return st["duty"]
        if mode == "ANALOG":
            return int(1650 + 1400 * math.sin(t * 0.7 + ALL_PINS.index(name)))
        return None

    # -- protocol ------------------------------------------------------
    def handle(self, line: str) -> List[str]:
        parts = line.split()
        if not parts:
            return []
        cmd = parts[0].upper()
        try:
            if cmd == "PING":
                return ["PONG proto={}".format(PROTOCOL)]
            if cmd == "STATUS":
                return ["STATUS " + " ".join(
                    "{}={}".format(n, self._level(n, self.state[n])) for n in ALL_PINS)]
            if cmd == "GET" and len(parts) >= 2:
                n = normalise_pin(parts[1])
                return ["GET {} {}".format(parts[1], self._level(n, self.state[n]))]
            if cmd == "DUMP":
                return self._dump()
            if cmd == "PORTINFO":
                return self._portinfo()
            if cmd == "PORT":
                if len(parts) >= 2:
                    n = int(parts[1])
                    if not 1 <= n <= 6:
                        return ["ERR port must be 1-6"]
                    self.port = n
                    self.reset()
                    return ["OK PORT {}".format(n)]
                return ["PORT {}".format(self.port)]
            if cmd == "RESET":
                self.reset()
                return ["OK RESET"]
            if cmd == "INSPECT":
                names = [normalise_pin(parts[1])] if len(parts) >= 2 else list(ALL_PINS)
                out = []
                for n in names:
                    info = self._inspect(n)
                    out.append("INSPECT {} {}".format(n, " ".join(
                        "{}={}".format(k, v) for k, v in info.items())))
                return out + ["END"]
            if cmd == "SET" and len(parts) >= 3:
                return self._set(parts)
            return ["ERR bad command: {}".format(line)]
        except (ValueError, KeyError) as exc:
            return ["ERR {!r}".format(exc)]

    def _inspect(self, name: str) -> dict:
        st = self.state[name]
        info = {"func": "gpio", "dir": "in", "level": self._level(name, st) if st["mode"] in ("INSPECT", "IN", "LATCH") else 0}
        if pin_kind(name) == "hs":
            info["pull"] = "none"
        return info

    def _adc(self, name: str):
        """ADC channel name for pins that have one (HS pins on ports 4-6)."""
        if pin_kind(name) != "hs" or self.port < 4:
            return None
        idx = HS_PINS.index(name)
        gpio = {4: [11, 14, 13, 12], 5: [18, 16, 15, 17], 6: [3, 4, 5, 6]}[self.port][idx]
        return "ADC1_CH{}".format(gpio - 1) if gpio <= 10 else "ADC2_CH{}".format(gpio - 11)

    def _set(self, parts: List[str]) -> List[str]:
        try:
            name = normalise_pin(parts[1])
        except ValueError:
            return ["ERR unknown pin {}".format(parts[1])]
        word = parts[2].upper()
        kind = pin_kind(name)
        if word not in MODES:
            return ["ERR unknown mode {}".format(word)]
        if word in ("LEDOFF", "LEDON", "BREATHE") and kind != "ls":
            return ["ERR LED mode is LS pins only"]
        if word == "PWM" and kind != "hs":
            return ["ERR PWM mode is HS pins only"]
        if word == "ANALOG" and self._adc(name) is None:
            return ["ERR ANALOG needs an HS pin on port 4, 5 or 6"]
        st = self.state[name]
        if len(parts) >= 4:
            n = int(parts[3])
            if word in ("LEDON", "LEDOFF"):
                st["bright"] = max(0, min(100, n))
            elif word == "PWM":
                st["freq"] = min(PWM_FREQS, key=lambda f: abs(f - n))
                if len(parts) >= 5:
                    st["duty"] = max(0, min(100, int(parts[4])))
            elif word == "BREATHE":
                st["period"] = max(200, min(10000, n * 100))
                if len(parts) >= 5:
                    st["bright"] = max(0, min(100, int(parts[4])))
        st["mode"] = word
        st["user"] = 1
        return ["OK {} {}".format(parts[1], word)]

    def _portinfo(self) -> List[str]:
        present = self.port in self.populated
        out = ["PORTINFO port={} present={} detect={} eeprom={} files={} app={} scan=ok{}".format(
            self.port, int(present), int(present), int(present), 3 if present else 0, int(present),
            " addr=80 uid=4242 size=8192 page=32 ver=2 vid=51758 pid=4660" if present else "")]
        if present:
            out += ["NAME Sim Hexpansion",
                    "I2C " + " ".join(str(a) for a in self.i2c_addresses.get(self.port, [80]))]
        return out + ["END"]

    def _dump(self) -> List[str]:
        present = self.port in self.populated
        out = ["DUMP port={} present={} detect={} proto={}".format(
            self.port, int(present), int(present), PROTOCOL)]
        for name in ALL_PINS:
            st = self.state[name]
            mode, level = st["mode"], self._level(name, st)
            f = ["mode=" + mode, "level=" + ("-" if level is None else str(level)),
                 "kind=" + pin_kind(name), "user={}".format(st["user"]), "auto=0"]
            if self._adc(name):
                f.append("adc=" + self._adc(name))
            if mode == "INSPECT":
                info = self._inspect(name)
                f += ["{}={}".format(k, info[k]) for k in ("func", "dir", "pull") if k in info]
            elif mode == "ANALOG":
                f += ["mv={}".format(level), "raw={}".format(level * 65535 // 3100)]
            elif mode == "LATCH":
                f += ["hi=1", "lo=1"]
            elif mode in ("LEDOFF", "LEDON", "BREATHE"):
                f += ["bright={}".format(st["bright"]), "period={}".format(st["period"]),
                      "out={}".format((level or 0) * 100 // 255)]
            elif mode == "PWM":
                f += ["freq={}".format(st["freq"]), "duty={}".format(st["duty"])]
            out.append("PIN {} {}".format(name, " ".join(f)))
        return out + ["END"]


class SimLink(Link):
    name = "simulator"

    def __init__(self, latency: float = 0.01):
        self.badge = SimBadge()
        self.latency = latency

    def request(self, cmd: str, timeout: float = 1.5) -> List[str]:
        time.sleep(self.latency)
        return self.badge.handle(cmd)
