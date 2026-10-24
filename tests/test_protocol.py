"""Protocol, simulator and CLI tests -- no hardware or display needed.

    python3 -m unittest discover -s tests -v
"""
import io
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from pintester import cli
from pintester.link import (LinkError, normalise_pin, parse_dump, parse_kv, terminator_for)
from pintester.sim import SimLink

DUMP = """DUMP port=2 present=1 detect=1 proto=2
PIN LS_A mode=LEDON level=127 kind=ls user=1 auto=0 bright=50 period=2000 out=50
PIN HS_G mode=PWM level=70 kind=hs user=1 auto=0 freq=5000 duty=70
PIN HS_H mode=INSPECT level=- kind=hs user=0 auto=0 func=gpio dir=in pull=up
END""".splitlines()


class Parsing(unittest.TestCase):
    def test_kv(self):
        self.assertEqual(parse_kv(["a=1", "b=-", "c=x", "junk"]), {"a": 1, "b": None, "c": "x"})

    def test_dump(self):
        port = parse_dump(DUMP)
        self.assertEqual((port.port, port.present, port.detect, port.proto), (2, True, True, 2))
        led = port.pin("ls_a")
        self.assertEqual((led.mode, led.level, led.fields["bright"]), ("LEDON", 127, 50))
        self.assertIn("5000 Hz", port.pin("HS_G").describe())
        hs_h = port.pin("HS_H")
        self.assertIsNone(hs_h.level)
        self.assertIn("pull-up", hs_h.describe())

    def test_names(self):
        self.assertEqual(normalise_pin("ls-b"), "LS_B")
        self.assertEqual(normalise_pin("mosi"), "HS_F")
        with self.assertRaises(ValueError):
            normalise_pin("LS_Z")

    def test_terminators(self):
        self.assertTrue(terminator_for("DUMP")("END"))
        self.assertFalse(terminator_for("DUMP")("PIN LS_A mode=IN"))
        self.assertTrue(terminator_for("PORT")("PORT 3"))
        self.assertTrue(terminator_for("PORT 3")("OK PORT 3"))
        self.assertTrue(terminator_for("SET HS_F ON")("ERR nope"))


PORTINFO = """PORTINFO port=1 present=1 detect=1 eeprom=1 files=3 app=1 scan=ok addr=80 uid=7 size=8192 page=32 ver=2 vid=51758 pid=4660
NAME Hexi GFX board
I2C 80 87
END""".splitlines()


class PortInfoParsing(unittest.TestCase):
    def test_full(self):
        from pintester.link import parse_portinfo
        info = parse_portinfo(PORTINFO)
        self.assertEqual((info.name, info.vid_pid), ("Hexi GFX board", "0xCA2E / 0x1234"))
        self.assertEqual(info.i2c, [80, 87])
        self.assertEqual(info.eeprom_desc, "0x50, 8 KiB, 32-byte pages")
        self.assertTrue(info.eeprom_mounted and info.has_app)
        self.assertEqual(info.files, 3)

    def test_empty_port(self):
        from pintester.link import parse_portinfo
        info = parse_portinfo(["PORTINFO port=4 present=0 detect=0 eeprom=0 files=0 app=0 scan=ok", "END"])
        self.assertIsNone(info.name)
        self.assertIsNone(info.vid_pid)
        self.assertEqual(info.i2c, [])
        self.assertFalse(info.present)

    def test_sim_and_cli(self):
        link = SimLink(latency=0)
        self.assertEqual(link.portinfo(scan=True).name, "Sim Hexpansion")
        link.port(3)
        self.assertIsNone(link.portinfo().name)


class Analog(unittest.TestCase):
    def test_parse_and_describe(self):
        port = parse_dump(["DUMP port=6 present=0 detect=0 proto=2",
                           "PIN HS_F mode=ANALOG level=1234 kind=hs user=1 auto=0 adc=ADC1_CH2 mv=1234 raw=26000",
                           "PIN HS_G mode=IN level=0 kind=hs user=0 auto=0 adc=ADC1_CH3",
                           "PIN LS_A mode=IN level=0 kind=ls user=0 auto=0", "END"])
        f = port.pin("HS_F")
        self.assertEqual((f.adc, f.volts), ("ADC1_CH2", 1.234))
        self.assertIn("1.234 V", f.describe())
        self.assertEqual(port.pin("HS_G").adc, "ADC1_CH3")
        self.assertIsNone(port.pin("LS_A").adc)

    def test_sim_rules(self):
        link = SimLink(latency=0)
        with self.assertRaises(LinkError):
            link.set_mode("HS_F", "ANALOG")          # port 1: no ADC pins
        self.assertIsNone(link.dump().pin("HS_F").adc)
        link.port(6)
        with self.assertRaises(LinkError):
            link.set_mode("LS_A", "ANALOG")          # LS pins never
        link.set_mode("HS_F", "ANALOG")
        pin = link.dump().pin("HS_F")
        self.assertEqual((pin.mode, pin.adc), ("ANALOG", "ADC1_CH2"))
        self.assertTrue(0 <= pin.fields["mv"] <= 3100)
        self.assertTrue(0 <= link.get("HS_F") <= 3100)

    def test_cli(self):
        out, err = io.StringIO(), io.StringIO()
        old = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            rc_bad = cli.main(["--sim", "analog", "HS_F"])
        finally:
            sys.stdout, sys.stderr = old
        self.assertEqual(rc_bad, 1)
        self.assertIn("no ADC", err.getvalue())


class Simulator(unittest.TestCase):
    def setUp(self):
        self.link = SimLink(latency=0)

    def test_roundtrip(self):
        self.assertEqual(self.link.ping(), 2)
        self.assertEqual(self.link.port(), 1)
        self.link.set_mode("HS_G", "PWM", 5000, 70)
        pin = self.link.dump().pin("HS_G")
        self.assertEqual((pin.mode, pin.fields["freq"], pin.fields["duty"]), ("PWM", 5000, 70))
        self.link.set_mode("LS_B", "BREATHE", 15, 80)
        pin = self.link.dump().pin("LS_B")
        self.assertEqual((pin.fields["period"], pin.fields["bright"]), (1500, 80))

    def test_errors(self):
        with self.assertRaises(LinkError):
            self.link.set_mode("HS_F", "LEDON")
        with self.assertRaises(LinkError):
            self.link.set_mode("LS_A", "PWM")
        with self.assertRaises(ValueError):
            self.link.set_mode("LS_A", "NOPE")
        with self.assertRaises(ValueError):
            self.link.port(9)

    def test_port_and_reset(self):
        self.link.set_mode("LS_A", "ON")
        self.link.port(3)                       # empty port: pins default to plain input
        self.assertFalse(self.link.dump().present)
        self.assertEqual(self.link.dump().pin("LS_A").mode, "IN")
        self.link.port(1)
        self.link.set_mode("LS_A", "ON")
        self.link.reset()
        self.assertEqual(self.link.dump().pin("LS_A").mode, "INSPECT")

    def test_inspect(self):
        info = self.link.inspect("HS_F")
        self.assertEqual(info["HS_F"]["func"], "gpio")
        self.assertEqual(len(self.link.inspect()), 9)


class CommandLine(unittest.TestCase):
    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        old = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            rc = cli.main(["--sim", *argv])
        finally:
            sys.stdout, sys.stderr = old
        return rc, out.getvalue(), err.getvalue()

    def test_dump(self):
        rc, out, _ = self.run_cli("dump")
        self.assertEqual(rc, 0)
        self.assertIn("hexpansion present", out)
        self.assertIn("HS_I", out)

    def test_json(self):
        import json
        rc, out, _ = self.run_cli("dump", "--json")
        self.assertEqual(rc, 0)
        self.assertEqual(len(json.loads(out)["pins"]), 9)

    def test_error_exit_code(self):
        rc, _out, err = self.run_cli("led", "HS_F", "on")
        self.assertEqual(rc, 1)
        self.assertIn("LS pins only", err)

    def test_inspect_only_command_and_alias(self):
        for name in ("inspect-only", "hands-off"):
            rc, out, _ = self.run_cli(name, "all")
            self.assertEqual((rc, out.strip()), (0, "ok"))

    def test_shell_lines(self):
        link = SimLink(latency=0)
        parser = cli._shell_parser()
        out = io.StringIO()
        cli.run_line(link, parser, "pwm HS_G 2000 25", out)
        cli.run_line(link, parser, "GET HS_G", out)
        cli.run_line(link, parser, "nonsense", out)
        text = out.getvalue()
        self.assertIn("ok", text)
        self.assertIn("GET HS_G 25", text)
        self.assertIn("unknown command", text)


if __name__ == "__main__":
    unittest.main()
