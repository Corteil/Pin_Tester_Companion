"""Command-line access to the Pin Tester badge app.

    pintester ports                      list serial ports
    pintester dump                       full state of the current port
    pintester pwm HS_F 1000 50           1 kHz, 50% on HS_F
    pintester led LS_A breathe 2.0       breathe LS_A over 2 s
    pintester shell                      interactive prompt
    pintester gui                        open the graphical app

Run ``pintester -h`` (or ``help`` inside the shell) for everything.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import shlex
import sys
import time
from typing import List, Optional

from . import __version__
from .link import (ALL_PINS, ANALOG_FULL_MV, MODES, PWM_FREQS, Link, LinkError, Pin, Port,
                   SerialLink, find_devices, normalise_pin)

RAW_WORDS = ("PING", "STATUS", "GET", "DUMP", "PORTINFO", "INSPECT", "PORT", "RESET", "SET")


# --- output helpers ----------------------------------------------------------

class Style:
    def __init__(self, enabled: bool):
        self.on = enabled

    def _c(self, code: str, text: str) -> str:
        return "\033[{}m{}\033[0m".format(code, text) if self.on else text

    def green(self, t): return self._c("32", t)
    def red(self, t): return self._c("31", t)
    def yellow(self, t): return self._c("33", t)
    def cyan(self, t): return self._c("36", t)
    def dim(self, t): return self._c("2", t)
    def bold(self, t): return self._c("1", t)


def _level_cell(pin: Pin, st: Style) -> str:
    if pin.level is None:
        return st.dim("-")
    if pin.mode in ("LEDOFF", "LEDON", "BREATHE"):
        return st.yellow("{}%".format(pin.fields.get("out", "?")))
    if pin.mode == "PWM":
        return st.yellow("{}%".format(pin.level))
    if pin.mode == "ANALOG":
        return st.cyan("{:.2f} V".format(pin.level / 1000))
    if pin.mode == "INSPECT" and pin.fields.get("func") in ("led", "pwm", "periph"):
        return st.dim("-")
    return st.green("HIGH") if pin.level else st.red("LOW")


def _plain_len(text: str) -> int:
    import re
    return len(re.sub(r"\033\[[0-9;]*m", "", text))


def format_port(port: Port, st: Style) -> str:
    if port.present:
        how = " (detect line)" if port.detect else ""
        head = "Port {}  {}{}".format(port.port, st.yellow("hexpansion present"), how)
    else:
        head = "Port {}  {}".format(port.port, st.dim("empty"))
    rows = [st.bold("{:<6} {:<15} {:<7} {}".format("PIN", "MODE", "LEVEL", "DETAILS"))]
    for p in port.pins:
        mode = p.mode_label
        if p.mode == "INSPECT":
            mode = st.cyan(mode)
        cell = _level_cell(p, st)
        pad = " " * max(0, 7 - _plain_len(cell))
        mpad = " " * max(0, 15 - _plain_len(mode))
        rows.append("{:<6} {}{} {}{} {}".format(p.name, mode, mpad, cell, pad, p.describe()))
    return head + "\n" + "\n".join(rows)


# --- commands ---------------------------------------------------------------

def cmd_ports(link, args, out):
    devices = find_devices()
    if not devices:
        print("no serial ports found", file=out)
    for dev, desc in devices:
        print("{}  {}".format(dev, desc), file=out)


def cmd_ping(link, args, out):
    print("badge responding, protocol {}".format(link.ping()), file=out)


def cmd_status(link, args, out):
    levels = link.status()
    if args.json:
        print(json.dumps(levels), file=out)
    else:
        for name, lvl in levels.items():
            print("{:<6} {}".format(name, "-" if lvl is None else lvl), file=out)


def cmd_dump(link, args, out):
    port = link.dump()
    if args.json:
        print(json.dumps(dataclasses.asdict(port), indent=2), file=out)
    else:
        print(format_port(port, Style(out.isatty())), file=out)


def format_info(info, st: Style) -> str:
    head = "Port {}  {}".format(info.port, st.yellow("hexpansion present") if info.present else st.dim("empty"))
    if info.detect is not None:
        head += st.dim("  (detect line {})".format("low" if info.detect else "high"))
    rows = []

    def row(label, value):
        rows.append("  {:<11} {}".format(label, value))

    row("Name", info.name or st.dim("-- no EEPROM header --"))
    if info.vid_pid:
        uid = "   unique id {}".format(info.uid) if info.uid is not None else ""
        row("VID / PID", info.vid_pid + uid)
    row("EEPROM", info.eeprom_desc or st.dim("not found"))
    if info.header_version:
        rows[-1] += "   header v{}".format(info.header_version)
    if info.eeprom_mounted:
        row("Filesystem", "mounted, {} file{}, app {}".format(
            info.files, "" if info.files == 1 else "s", "available" if info.has_app else "not present"))
    else:
        row("Filesystem", st.dim("not mounted"))
    if info.scan == "err":
        row("I2C scan", st.red(info.scan_error or "failed"))
    elif info.scan == "-":
        row("I2C scan", st.dim("not scanned (use --scan)"))
    else:
        row("I2C scan", " ".join("0x{:02X}".format(a) for a in info.i2c) if info.i2c else st.dim("no devices"))
    return head + "\n" + "\n".join(rows)


def cmd_info(link, args, out):
    info = link.portinfo(scan=args.scan)
    if args.json:
        print(json.dumps(dataclasses.asdict(info), indent=2), file=out)
    else:
        print(format_info(info, Style(out.isatty())), file=out)


def cmd_get(link, args, out):
    lvl = link.get(args.pin)
    print("-" if lvl is None else lvl, file=out)


def cmd_set(link, args, out):
    link.set_mode(args.pin, args.mode, *args.values)
    print("ok", file=out)


def cmd_in(link, args, out):
    link.set_mode(args.pin, "LATCH" if args.latch else "IN")
    print("ok", file=out)


def cmd_out(link, args, out):
    link.set_mode(args.pin, {"on": "ON", "off": "OFF", "blink": "AUTO"}[args.state])
    print("ok", file=out)


def cmd_pwm(link, args, out):
    link.set_mode(args.pin, "PWM", args.hz, args.duty)
    snapped = min(PWM_FREQS, key=lambda f: abs(f - args.hz))
    print("ok" if snapped == args.hz else "ok (snapped to {} Hz)".format(snapped), file=out)


def cmd_led(link, args, out):
    if args.state == "breathe":
        secs = args.value if args.value is not None else 2.0
        link.set_mode(args.pin, "BREATHE", max(2, min(100, round(secs * 10))))
    elif args.state == "on":
        link.set_mode(args.pin, "LEDON", *(() if args.value is None else (int(args.value),)))
    else:
        link.set_mode(args.pin, "LEDOFF")
    print("ok", file=out)


def cmd_analog(link, args, out):
    pin = link.dump().pin(args.pin)
    if not pin.adc:
        raise LinkError("{} has no ADC here: analog needs an HS pin on port 4, 5 or 6 "
                        "(see 'pintester dump')".format(pin.name))
    if pin.mode != "ANALOG":
        port = link.dump()
        if port.present:
            print("note: reading analog reconfigures {} -- don't do this on a pin the "
                  "hexpansion in port {} is using".format(pin.name, port.port), file=sys.stderr)
        link.set_mode(pin.name, "ANALOG")
        time.sleep(0.3)  # let the badge take its first averaged sample

    def read():
        mv = link.get(pin.name)
        if mv is None:
            raise LinkError("no reading from {} (ADC error?)".format(pin.name))
        return mv

    if not args.watch:
        mv = read()
        print(json.dumps({"pin": pin.name, "mv": mv, "volts": mv / 1000}) if args.json
              else "{:.3f} V  ({} mV, {})".format(mv / 1000, mv, pin.adc), file=out)
        return
    try:
        while True:
            mv = read()
            bar = "#" * round(30 * min(mv, ANALOG_FULL_MV) / ANALOG_FULL_MV)
            out.write("\r{} {:6.3f} V  |{:<30}|".format(pin.name, mv / 1000, bar))
            out.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(file=out)


def cmd_inspect_only(link, args, out):
    names = ALL_PINS if args.pin.lower() == "all" else (normalise_pin(args.pin),)
    for n in names:
        link.set_mode(n, "INSPECT")
    print("ok", file=out)


def cmd_port(link, args, out):
    if args.number is None:
        print(link.port(), file=out)
    else:
        link.port(args.number)
        print("ok (pins reset for the new port)", file=out)


def cmd_reset(link, args, out):
    link.reset()
    print("ok", file=out)


def cmd_inspect(link, args, out):
    info = link.inspect(args.pin)
    if args.json:
        print(json.dumps(info, indent=2), file=out)
        return
    for name, kv in info.items():
        print("{:<6} {}".format(name, " ".join("{}={}".format(k, v) for k, v in kv.items())), file=out)


def cmd_raw(link, args, out):
    for line in link.raw(" ".join(args.words)):
        print(line, file=out)


def cmd_monitor(link, args, out):
    st = Style(out.isatty())
    try:
        while True:
            text = format_port(link.dump(), st)
            out.write("\033[H\033[J" + text + "\n\n" + st.dim("Ctrl-C to stop") + "\n")
            out.flush()
            time.sleep(args.interval)
    except KeyboardInterrupt:
        print(file=out)


def add_commands(sp) -> None:
    """Register the friendly sub-commands (used by the CLI and the shell)."""
    def pin_arg(p):
        p.add_argument("pin", help="LS_A..LS_E, HS_F..HS_I (or MOSI/SCK/CS/DC)")

    sp.add_parser("ports", help="list serial ports").set_defaults(func=cmd_ports, no_link=True)
    sp.add_parser("ping", help="check the badge is answering").set_defaults(func=cmd_ping)

    p = sp.add_parser("status", help="live level of every pin")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)

    p = sp.add_parser("dump", help="full state of the current port")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_dump)

    p = sp.add_parser("info", help="hexpansion EEPROM and I2C details for the current port")
    p.add_argument("--scan", action="store_true", help="do a fresh I2C bus scan first")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_info)

    p = sp.add_parser("get", help="live level of one pin")
    pin_arg(p)
    p.set_defaults(func=cmd_get)

    p = sp.add_parser("set", help="raw SET: pin, mode word, optional values")
    pin_arg(p)
    p.add_argument("mode", help="one of: " + ", ".join(MODES))
    p.add_argument("values", nargs="*")
    p.set_defaults(func=cmd_set)

    p = sp.add_parser("in", help="make a pin an input")
    pin_arg(p)
    p.add_argument("--latch", action="store_true", help="remember ever-high / ever-low")
    p.set_defaults(func=cmd_in)

    p = sp.add_parser("out", help="drive a pin: on, off, or blink (2 Hz)")
    pin_arg(p)
    p.add_argument("state", choices=("on", "off", "blink"))
    p.set_defaults(func=cmd_out)

    p = sp.add_parser("pwm", help="hardware PWM on an HS pin")
    pin_arg(p)
    p.add_argument("hz", type=int, help="10 Hz - 100 kHz (snaps to 1-2-5 steps)")
    p.add_argument("duty", type=int, nargs="?", default=50, help="percent, default 50")
    p.set_defaults(func=cmd_pwm)

    p = sp.add_parser("led", help="LED mode on an LS pin")
    pin_arg(p)
    p.add_argument("state", choices=("off", "on", "breathe"))
    p.add_argument("value", type=float, nargs="?",
                   help="on: brightness %% (0-100); breathe: cycle time in seconds (0.2-10)")
    p.set_defaults(func=cmd_led)

    p = sp.add_parser("analog", help="read an HS pin's voltage (ports 4-6 only; reconfigures the pin)")
    pin_arg(p)
    p.add_argument("--watch", action="store_true", help="live readout until Ctrl-C")
    p.add_argument("-i", "--interval", type=float, default=0.2)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_analog)

    p = sp.add_parser("inspect-only", aliases=["hands-off"],
                      help="put a pin in inspect only (releases it, then read-only); 'all' for every pin")
    p.add_argument("pin", help="a pin name, or 'all'")
    p.set_defaults(func=cmd_inspect_only)

    p = sp.add_parser("port", help="show or change the hexpansion port (1-6)")
    p.add_argument("number", type=int, nargs="?")
    p.set_defaults(func=cmd_port)

    sp.add_parser("reset", help="hand every pin back").set_defaults(func=cmd_reset)

    p = sp.add_parser("inspect", help="fresh read-only snapshot of how pins are set")
    p.add_argument("pin", nargs="?")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_inspect)

    p = sp.add_parser("raw", help="send a protocol line as-is, e.g.  raw SET HS_F ON")
    p.add_argument("words", nargs="+")
    p.set_defaults(func=cmd_raw)

    p = sp.add_parser("monitor", help="live table, refreshed until Ctrl-C")
    p.add_argument("-i", "--interval", type=float, default=0.5)
    p.set_defaults(func=cmd_monitor)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="pintester",
        description="Control the Pin Tester badge app over USB. The badge must be running "
                    "Pin Tester with 'Remote: ON' (STATUS tile, press CONFIRM).",
        epilog="Run 'pintester shell' for an interactive prompt, 'pintester gui' for the GUI.")
    p.add_argument("-d", "--device", help="serial port (default: auto-detect)")
    p.add_argument("--sim", action="store_true", help="use a built-in simulated badge")
    p.add_argument("--version", action="version", version="pintester " + __version__)
    sp = p.add_subparsers(dest="command", metavar="COMMAND")
    add_commands(sp)
    sp.add_parser("shell", help="interactive prompt").set_defaults(func=None, shell=True)
    sp.add_parser("gui", help="open the graphical app").set_defaults(func=None, gui=True)
    return p


# --- shell ------------------------------------------------------------------

def _shell_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="", add_help=False)
    sp = p.add_subparsers(dest="command")
    add_commands(sp)
    return p


def run_line(link: Link, parser, line: str, out) -> None:
    """One shell line: UPPERCASE first word = raw protocol, else a sub-command."""
    try:
        tokens = shlex.split(line)
    except ValueError as exc:
        print("error:", exc, file=out)
        return
    if not tokens:
        return
    if tokens[0] in RAW_WORDS or tokens[0].isupper():
        for ln in link.raw(" ".join(tokens)):
            print(ln, file=out)
        return
    if tokens[0] != "help" and tokens[0] not in parser._subparsers._group_actions[0].choices:
        print("unknown command {!r} -- try 'help'".format(tokens[0]), file=out)
        return
    if tokens[0] == "help":
        print(HELP_TEXT, file=out)
        return
    try:
        args = parser.parse_args(tokens)
    except SystemExit:
        return  # argparse already printed its message
    if args.command is None:
        return
    args.func(link, args, out)


HELP_TEXT = """\
Commands (lowercase):  ports ping status dump info get set in out pwm led analog inspect-only
                       port reset inspect raw monitor        -- 'COMMAND -h' for details
Raw protocol (UPPERCASE, sent as-is):  PING  STATUS  DUMP  GET <pin>  PORT [n]
                       RESET  INSPECT [pin]  SET <pin> <mode> [values]
Other:  help   exit / quit / Ctrl-D"""


def shell(link: Link, out=sys.stdout) -> None:
    parser = _shell_parser()
    try:
        import readline
        words = sorted(parser._subparsers._group_actions[0].choices) + list(ALL_PINS) + list(MODES) + list(RAW_WORDS)

        def complete(text, state):
            matches = [w for w in words if w.lower().startswith(text.lower())]
            return matches[state] if state < len(matches) else None

        readline.set_completer(complete)
        readline.parse_and_bind("tab: complete")
        readline.set_completer_delims(" \t")
    except (ImportError, AttributeError):
        pass
    print("Pin Tester shell -- 'help' for commands, Ctrl-D to exit.", file=out)
    while True:
        try:
            try:
                line = input("pin[{}]> ".format(link.port()))
            except LinkError:
                line = input("pin[?]> ")
        except (EOFError, KeyboardInterrupt):
            print(file=out)
            return
        if line.strip() in ("exit", "quit"):
            return
        try:
            run_line(link, parser, line, out)
        except (LinkError, ValueError) as exc:
            print("error:", exc, file=out)


# --- entry point ------------------------------------------------------------

def open_link(args) -> Link:
    if args.sim:
        from .sim import SimLink
        return SimLink()
    return SerialLink(args.device)


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    if getattr(args, "gui", False):
        from .gui import run_gui
        return run_gui(device=args.device, sim=args.sim)
    link = None
    try:
        if not getattr(args, "no_link", False):
            link = open_link(args)
            link.ping()
        if getattr(args, "shell", False):
            shell(link)
        else:
            args.func(link, args, sys.stdout)
        return 0
    except (LinkError, ValueError) as exc:
        print("error:", exc, file=sys.stderr)
        return 1
    finally:
        if link is not None:
            link.close()
