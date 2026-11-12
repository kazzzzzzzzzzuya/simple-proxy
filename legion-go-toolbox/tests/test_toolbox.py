"""Tests of the hardware modules. Run: python3 -m unittest discover -s tests"""

import logging
import os
import shutil
import socket
import struct
import tempfile
import threading
import time
import unittest

import helpers  # noqa: F401  (sets up sys.path)

from lgo_toolbox import acpi, battery, config as cfg, controller, fan, fps_probe
from lgo_toolbox import plumber, power, powerled
from lgo_toolbox.hub import Hub

DEFAULT_PROFILE_YAML = """# comment
version: 1
kind: DeviceProfile
name: Default

mapping:
  - name: LeftTop
    source_event:
      gamepad:
        button: LeftTop
    target_events:
      - gamepad:
          button: LeftPaddle1
  - name: Old select mapping
    source_event:
      gamepad:
        button: Select
    target_events:
      - keyboard: KeyEsc
  - name: Dial
    source_event:
      gamepad:
        dial:
          name: LeftStickDial
          direction: clockwise
    target_events:
      - keyboard: KeyVolumeUp
"""


def write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text)


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)


class FakeAcpi(object):
    """Stands in for the firmware behind /proc/acpi/call."""

    def __init__(self):
        self.calls = []
        self.features = {battery.FEATURE_CHARGE_LIMIT: 0, fan.FEATURE_FULL_SPEED: 0}
        self.mode = 0x02
        self.curve = None
        self.light = 0x02
        self.fail = False

    def call(self, method, args, path=acpi.PATH):
        self.calls.append((method, list(args)))
        if self.fail:
            raise acpi.AcpiError("AE_NOT_FOUND")
        if method == acpi.WMAE:
            op, payload = args[1], args[2]
            fid = int.from_bytes(payload[:4], "little")
            if op == 0x11:
                return self.features.get(fid, 0)
            self.features[fid] = int.from_bytes(payload[4:8], "little")
            return None
        if method == acpi.WMAA:
            if args[1] == 0x2D:
                return self.mode
            self.mode = args[2]
            return None
        if method == acpi.WMAB:
            if args[1] == 0x05:          # read the table back
                if self.curve is None:
                    return bytes(44)
                out = bytearray(44)
                for i, level in enumerate(self.curve[6:26:2]):
                    out[4 + 4 * i] = level
                return bytes(out)
            self.curve = bytes(args[2])
            return None
        if method == acpi.WMAF:
            if args[1] == 0x01:
                return bytes([args[2], self.light])
            if args[2][0] == powerled.WMI_LIGHT_ID:
                self.light = args[2][2]
            return None
        raise AssertionError(method)

    def install(self, test, available=True):
        orig = (acpi.call, acpi.available)
        acpi.call = self.call
        acpi.available = lambda path=acpi.PATH: available
        test.addCleanup(lambda: (setattr(acpi, "call", orig[0]),
                                 setattr(acpi, "available", orig[1])))


class AcpiFormat(unittest.TestCase):
    def test_format(self):
        self.assertEqual(
            acpi.format_call(acpi.WMAE, [0, 0x12, bytes([1, 0, 1, 3, 1, 0, 0, 0])]),
            r"\_SB.GZFD.WMAE 0x00 0x12 b0100010301000000")

    def test_parse(self):
        self.assertIsNone(acpi.parse_result("not called\0"))
        self.assertEqual(acpi.parse_result("0xff\0"), 255)
        self.assertEqual(acpi.parse_result("{0x04, 0x02}\0"), bytes([4, 2]))
        with self.assertRaises(acpi.AcpiError):
            acpi.parse_result("Error: AE_NOT_FOUND\0")

    def test_missing_module(self):
        self.assertFalse(acpi.available("/nonexistent/acpi/call"))
        with self.assertRaises(acpi.AcpiError):
            acpi.call(acpi.WMAA, [0, 0x2D, 0], path="/nonexistent/acpi/call")


class Battery(TempDirCase):
    def _supply(self, text):
        write(os.path.join(self.tmp, "BAT0", "charge_types"), text)
        write(os.path.join(self.tmp, "BAT0", "type"), "Battery\n")
        write(os.path.join(self.tmp, "BAT0", "capacity"), "77\n")
        write(os.path.join(self.tmp, "BAT0", "status"), "Discharging\n")
        write(os.path.join(self.tmp, "ADP0", "type"), "Mains\n")

    def test_parse(self):
        self.assertEqual(battery.parse_charge_types("[Standard] Long_Life"),
                         (("Standard", "Long_Life"), "Standard"))
        with self.assertRaises(ValueError):
            battery.parse_charge_types("Standard Long_Life")

    def test_sysfs(self):
        self._supply("[Standard] Long_Life\n")
        limit = battery.ChargeLimit(root=self.tmp, acpi_path="/nonexistent")
        self.assertEqual(limit.status()["enabled"], False)
        self.assertEqual(limit.status()["backend"], "sysfs")
        # A plain file keeps what is written, so emulate the kernel's reply
        path = os.path.join(self.tmp, "BAT0", "charge_types")
        orig = battery.sysio.write_text

        def kernel(p, value):
            if p == path:
                return orig(p, "Standard [Long_Life]" if value == "Long_Life"
                            else "[Standard] Long_Life")
            return orig(p, value)

        battery.sysio.write_text = kernel
        self.addCleanup(setattr, battery.sysio, "write_text", orig)
        after = limit.set(True)
        self.assertTrue(after["enabled"] and after["error"] is None)
        self.assertEqual(limit.set(False)["enabled"], False)
        self.assertEqual(battery.battery_info(self.tmp),
                         {"percent": 77, "status": "Discharging"})

    def test_write_that_does_not_stick_is_reported(self):
        self._supply("[Standard] Long_Life\n")
        limit = battery.ChargeLimit(root=self.tmp, acpi_path="/nonexistent")
        orig = battery.sysio.write_text
        battery.sysio.write_text = lambda p, v: None       # silently ignored
        self.addCleanup(setattr, battery.sysio, "write_text", orig)
        self.assertEqual(limit.set(True)["error"], "The charge mode did not change")

    def test_unsupported_types_are_ignored(self):
        self._supply("[Fast] Trickle\n")
        limit = battery.ChargeLimit(root=self.tmp, acpi_path="/nonexistent")
        self.assertFalse(limit.status()["available"])

    def test_acpi_fallback(self):
        fake = FakeAcpi()
        fake.install(self)
        limit = battery.ChargeLimit(root=self.tmp)
        self.assertEqual(limit.status()["backend"], "acpi_call")
        self.assertTrue(limit.set(True)["enabled"])
        self.assertEqual(fake.calls[-2], (
            acpi.WMAE, [0, 0x12, bytes([1, 0, 1, 3, 1, 0, 0, 0])]))
        self.assertFalse(limit.set(False)["enabled"])


class ControllerCommands(unittest.TestCase):
    def test_rgb(self):
        cmds = controller.rgb_commands("solid", (255, 0, 16), 1.0, 0.5)
        self.assertEqual(cmds[0], bytes.fromhex("050c72010301ff00103f200301"))
        self.assertEqual(cmds[1], bytes.fromhex("050c72010401ff00103f200301"))
        self.assertEqual(cmds[2:4], [bytes.fromhex("05067302030301"),
                                     bytes.fromhex("05067302040301")])
        self.assertEqual(cmds[4:], [bytes.fromhex("05067002030101"),
                                    bytes.fromhex("05067002040101")])
        self.assertEqual(controller.rgb_off_commands(),
                         [bytes.fromhex("05067002030001"), bytes.fromhex("05067002040001")])
        spiral = controller.rgb_commands("spiral", (1, 2, 3), 0.0, 1.0)[0]
        self.assertEqual((spiral[5], spiral[9], spiral[10]), (4, 0, 0))

    def test_remap_and_touchpad(self):
        self.assertEqual(controller.remap_command("Y1", "a"),
                         bytes.fromhex("05076c02031c1201"))
        self.assertEqual(controller.remap_command("M3", "none"),
                         bytes.fromhex("05076c0204220001"))
        self.assertEqual(controller.remap_command("Y3", "menu"),
                         bytes.fromhex("05076c02041e2401"))
        self.assertEqual(controller.touchpad_command(False), bytes.fromhex("05066b02040001"))

    def test_send(self):
        a, b = socket.socketpair(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        cmds = controller.rgb_off_commands()
        self.assertIsNone(controller.send(cmds, opener=lambda: os.dup(b.fileno()), gap_s=0))
        self.assertEqual([a.recv(64), a.recv(64)], cmds)

        def missing():
            raise OSError("Legion Go controllers not found")

        self.assertEqual(controller.send(cmds, opener=missing),
                         "Legion Go controllers not found")
        self.assertIsNone(controller.send([], opener=missing))


class FanCurve(unittest.TestCase):
    def test_sanitize(self):
        self.assertEqual(fan.sanitize_curve([0] * 10), fan.SAFETY_FLOOR)
        self.assertEqual(fan.SAFETY_FLOOR, (44, 48, 55, 60, 71, 79, 87, 87, 100, 100))
        self.assertEqual(fan.sanitize_curve([200, 10, 5, 0, 0, 0, 0, 0, 0, 0]),
                         (115,) * 10)
        self.assertEqual(fan.sanitize_curve([0, 0, 70, 20, 20, 90.4, 0, 0, 0, 0]),
                         (44, 48, 70, 70, 71, 90, 90, 90, 100, 100))
        for bad in ([1] * 9, "x", [1] * 9 + ["a"], [True] * 10, None):
            with self.assertRaises(ValueError):
                fan.sanitize_curve(bad)
        for preset in fan.PRESETS.values():
            self.assertEqual(fan.sanitize_curve(preset), preset)
            self.assertTrue(all(p >= f for p, f in zip(preset, fan.SAFETY_FLOOR)))

    def test_payload(self):
        payload = fan.curve_payload(fan.PRESETS["balanced"])
        self.assertEqual(len(payload), 52)
        self.assertEqual(payload[:6], bytes([0, 0, 10, 0, 0, 0]))
        self.assertEqual(list(payload[6:26:2]), list(fan.PRESETS["balanced"]))
        self.assertEqual(list(payload[31:51:2]), list(fan.TEMPS))

    def test_apply_and_restore(self):
        fake = FakeAcpi()
        fake.install(self)
        ctl = fan.FanControl(sleeper=lambda s: None)
        self.assertTrue(ctl.apply([0] * 10))
        self.assertEqual(fake.mode, 0xFF)          # switched to custom mode
        self.assertEqual(list(fake.curve[6:26:2]), list(fan.SAFETY_FLOOR))
        self.assertEqual(ctl.read_curve(), fan.SAFETY_FLOOR)
        self.assertFalse(ctl.read_full_speed())
        self.assertTrue(ctl.apply(fan.DEFAULT_CURVE, full_speed=True))
        self.assertEqual(fake.features[fan.FEATURE_FULL_SPEED], 1)
        self.assertTrue(ctl.read_full_speed())
        modes = []
        orig = fake.call

        def spy(method, args, path=acpi.PATH):
            if method == acpi.WMAA and args[1] == 0x2C:
                modes.append(args[2])
            return orig(method, args, path)

        acpi.call = spy
        self.assertTrue(ctl.restore())
        self.assertEqual(fake.features[fan.FEATURE_FULL_SPEED], 0)
        self.assertEqual(modes, [0x03, 0xFF])

    def test_errors(self):
        fake = FakeAcpi()
        fake.install(self, available=False)
        ctl = fan.FanControl(sleeper=lambda s: None)
        self.assertFalse(ctl.apply(fan.DEFAULT_CURVE))
        self.assertIn("acpi_call", ctl.error)
        fake.install(self)
        fake.fail = True
        self.assertFalse(ctl.apply(fan.DEFAULT_CURVE))
        self.assertEqual(ctl.error, "AE_NOT_FOUND")

    def test_sensors(self):
        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp)
        write(os.path.join(tmp, "hwmon0", "name"), "acpitz\n")
        write(os.path.join(tmp, "hwmon0", "temp1_input"), "20000\n")
        write(os.path.join(tmp, "hwmon3", "name"), "k10temp\n")
        write(os.path.join(tmp, "hwmon3", "temp1_input"), "61500\n")
        write(os.path.join(tmp, "hwmon5", "name"), "legion\n")
        write(os.path.join(tmp, "hwmon5", "fan1_input"), "4200\n")
        self.assertEqual(fan.read_sensors(tmp), {"temp": 62, "rpm": 4200})
        self.assertEqual(fan.read_sensors("/nonexistent"), {"temp": None, "rpm": None})


class _Ram(powerled._Backend):
    name = "ram"

    def __init__(self, mem):
        self.mem = mem
        self.closed = False

    def read(self, offset):
        return self.mem[offset]

    def write(self, offset, value):
        self.mem[offset] = value

    def close(self):
        self.closed = True


class PowerLight(TempDirCase):
    def test_bit_logic(self):
        mem = bytearray(256)
        mem[0x52] = 0b1101_0101                 # other bits must survive
        led = powerled.PowerLed("83E1", factories=[lambda: _Ram(mem)])
        self.assertTrue(led.available)
        self.assertTrue(led.get())              # bit 5 clear = on
        self.assertTrue(led.set(False))
        self.assertEqual(mem[0x52], 0b1111_0101)
        self.assertEqual(mem[0x58] & 1, 1)      # no breathing while off
        self.assertFalse(led.get())
        self.assertTrue(led.set(True))
        self.assertEqual(mem[0x52], 0b1101_0101)
        self.assertEqual(mem[0x58] & 1, 0)

    def test_go_s_location(self):
        mem = bytearray(256)
        led = powerled.PowerLed("83L3", factories=[lambda: _Ram(mem)])
        led.set(False)
        self.assertEqual(mem[0x10], 1 << 6)

    def test_unknown_model_is_never_touched(self):
        def boom():
            raise AssertionError("must not probe")

        led = powerled.PowerLed("Jupiter", factories=[boom])
        self.assertFalse(led.available)
        self.assertIsNone(led.get())
        self.assertFalse(led.set(True))
        self.assertIn("Jupiter", led.error)

    def test_falls_through_broken_routes(self):
        mem = bytearray(256)

        def broken():
            raise OSError("Operation not permitted")

        led = powerled.PowerLed("83E1", factories=[broken, lambda: _Ram(mem)])
        self.assertTrue(led.available)
        self.assertEqual(led.backend.name, "ram")
        led2 = powerled.PowerLed("83E1", factories=[broken])
        self.assertFalse(led2.available)
        self.assertIn("Operation not permitted", led2.error)

    def test_write_that_does_not_stick(self):
        class ReadOnly(_Ram):
            def write(self, offset, value):
                pass

        led = powerled.PowerLed("83E1", factories=[lambda: ReadOnly(bytearray(256))])
        self.assertFalse(led.set(False))
        self.assertEqual(led.error, "The light did not change")

    def test_mmio_backend_on_a_file(self):
        path = os.path.join(self.tmp, "mem")
        with open(path, "wb") as f:
            f.write(bytes(0x2000))
        backend = powerled.MmioBackend(dev=path, base=0x1300)
        self.addCleanup(backend.close)
        self.assertTrue(backend.set(0x52, 5, False))
        self.assertFalse(backend.get(0x52, 5))
        backend._mm.flush()
        with open(path, "rb") as f:
            data = f.read()
        self.assertEqual(data[0x1300 + 0x52], 1 << 5)
        self.assertEqual(data[0x1300 + 0x58], 1)

    def test_acpi_backend(self):
        fake = FakeAcpi()
        fake.install(self)
        led = powerled.PowerLed("83E1", factories=[powerled.AcpiBackend])
        self.assertTrue(led.get())
        self.assertTrue(led.set(False))
        self.assertEqual(fake.light, 0x01)
        self.assertIn((acpi.WMAF, [0, 0x02, bytes([0x24, 0x00, 0x01])]), fake.calls)
        self.assertTrue(led.set(True))
        self.assertEqual(fake.light, 0x02)


class Plumber(TempDirCase):
    def test_profile_keeps_base_mappings(self):
        text = plumber.build_profile(DEFAULT_PROFILE_YAML)
        self.assertIn("name: " + plumber.PROFILE_NAMES["steam_menu"], text)
        self.assertIn("button: LeftPaddle1", text)
        self.assertIn("LeftStickDial", text)
        self.assertNotIn("KeyEsc", text)          # replaced by the swap
        self.assertEqual(text.count("  - name:"), 2 + 4)
        self.assertEqual(text.count("mapping:"), 1)
        # Default layout: Steam <-> Menu, Quick Access <-> View
        for src, dst in (("Start", "Guide"), ("Guide", "Start"),
                         ("Select", "QuickAccess"), ("QuickAccess", "Select")):
            self.assertIn("button: %s\n    target_events:\n      - gamepad:\n"
                          "          button: %s\n" % (src, dst), text)
        other = plumber.build_profile(DEFAULT_PROFILE_YAML, "steam_view")
        self.assertIn("name: " + plumber.PROFILE_NAMES["steam_view"], other)
        for src, dst in (("Select", "Guide"), ("Guide", "Select"),
                         ("Start", "QuickAccess"), ("QuickAccess", "Start")):
            self.assertIn("button: %s\n    target_events:\n      - gamepad:\n"
                          "          button: %s\n" % (src, dst), other)

    def test_profile_without_usable_base(self):
        for base in (None, "", "name: x\n", "mapping:\n- flush: left\n",
                     "mapping:\n  - name: a\n   odd: indent\n"):
            text = plumber.build_profile(base)
            self.assertEqual(text.count("  - name:"), 4, base)

    def _swap(self, state):
        calls = []

        def runner(cmd, timeout=5.0):
            calls.append(cmd)
            if not state["running"] or not cmd[3].endswith("CompositeDevice1"):
                return 1, "", "Unknown object"
            if cmd[1] == "get-property":
                return 0, 's "%s"\n' % state[cmd[5]], ""
            state["loaded"] = cmd[-1]
            state["loads"] = state.get("loads", 0) + 1
            for name in plumber.PROFILE_NAMES.values():
                if "name: " + name in cmd[-1]:
                    state["ProfileName"], state["ProfilePath"] = name, ""
                    break
            else:
                state["ProfileName"] = "Default"
            return 0, "", ""

        return plumber.MenuSwap(runner=runner,
                                state_file=os.path.join(self.tmp, "base")), calls

    def test_apply_and_restore(self):
        base = os.path.join(self.tmp, "default.yaml")
        write(base, DEFAULT_PROFILE_YAML)
        state = {"running": True, "ProfileName": "Default", "ProfilePath": base}
        swap, calls = self._swap(state)
        self.assertTrue(swap.available())
        self.assertFalse(swap.is_active())
        self.assertTrue(swap.apply(True))
        self.assertTrue(swap.is_active())
        self.assertIn("button: LeftPaddle1", state["loaded"])
        self.assertTrue(swap.is_active("steam_menu"))
        self.assertFalse(swap.is_active("steam_view"))
        before = len(calls)
        self.assertTrue(swap.apply(True))                 # already active: no reload
        self.assertFalse(any(c[1] == "call" for c in calls[before:]))
        # Changing the layout reloads, still on top of the original base
        self.assertTrue(swap.apply(True, "steam_view"))
        self.assertTrue(swap.is_active("steam_view"))
        self.assertIn("button: LeftPaddle1", state["loaded"])
        self.assertEqual(state["loads"], 2)
        # Turning it off loads the remembered base profile again
        self.assertTrue(swap.apply(False))
        self.assertEqual(state["loaded"], DEFAULT_PROFILE_YAML.strip())
        self.assertFalse(swap.is_active())

    def test_steam_controller_type(self):
        calls = []
        state = {"stamp": "100", "manager": True}

        def runner(cmd, timeout=5.0):
            calls.append(cmd)
            if cmd[0] == "systemctl":
                return 0, state["stamp"] + "\n", ""
            if cmd[1] == "get-property" and cmd[-1] == "SupportedTargetDeviceIds":
                if not state["manager"]:
                    return 1, "", "Unknown property"
                return 0, 'as 4 "xb360" "ds5" "deck-uhid" "xbox-elite"\n', ""
            if cmd[1] == "get-property":
                return (0, 's "Default"\n', "") if cmd[3].endswith("Device0") else (1, "", "")
            return 0, "", ""

        path = os.path.join(self.tmp, "pad")
        pad = plumber.SteamController(runner=runner, state_file=path)
        self.assertEqual(pad.supported(), ["deck-uhid", "ds5"])
        state["manager"] = False
        self.assertEqual(pad.supported(), ["deck-uhid", "ds5-edge", "ds5"])
        self.assertFalse(pad.needs_apply("xbox"))
        self.assertTrue(pad.needs_apply("deck-uhid"))
        self.assertTrue(pad.apply("deck-uhid"))
        self.assertEqual(calls[-2][-6:], ["SetTargetDevices", "as", "3",
                                          "deck-uhid", "keyboard", "mouse"])
        self.assertFalse(pad.needs_apply("deck-uhid"))
        # A reloaded plugin knows what InputPlumber was told
        again = plumber.SteamController(runner=runner, state_file=path)
        self.assertFalse(again.needs_apply("deck-uhid"))
        self.assertTrue(again.needs_apply("xbox"))
        state["stamp"] = "200"                            # service restarted
        self.assertTrue(again.needs_apply("deck-uhid"))
        self.assertTrue(again.apply("xbox"))
        self.assertEqual(calls[-2][-5:], ["4", "xbox-elite", "mouse", "keyboard", "touchpad"])
        self.assertFalse(again.apply("ps9"))

    def test_not_running(self):
        swap, _ = self._swap({"running": False})
        self.assertFalse(swap.available())
        self.assertIsNone(swap.is_active())
        self.assertFalse(swap.apply(True))
        self.assertEqual(swap.error, "InputPlumber is not running")


class PowerSysfs(TempDirCase):
    def setUp(self):
        TempDirCase.setUp(self)
        self.fw = os.path.join(self.tmp, "fw")
        self.pp = os.path.join(self.tmp, "pp")
        attrs = os.path.join(self.fw, "lenovo-wmi-other-0", "attributes")
        for name, cur, lo, hi in (("ppt_pl1_spl", 15, 5, 30), ("ppt_pl2_sppt", 20, 5, 32),
                                  ("ppt_pl3_fppt", 25, 5, 41)):
            write(os.path.join(attrs, name, "current_value"), "%d\n" % cur)
            write(os.path.join(attrs, name, "min_value"), "%d\n" % lo)
            write(os.path.join(attrs, name, "max_value"), "%d\n" % hi)
        self.attrs = attrs
        write(os.path.join(self.pp, "platform-profile-0", "name"), "other\n")
        write(os.path.join(self.pp, "platform-profile-1", "name"), "lenovo-wmi-gamezone\n")
        write(os.path.join(self.pp, "platform-profile-1", "choices"),
              "low-power balanced performance custom\n")
        write(os.path.join(self.pp, "platform-profile-1", "profile"), "balanced\n")
        self.limits = power.PowerLimits(self.fw, self.pp, "/nonexistent",
                                        sleeper=lambda s: None)

    def _cur(self, name):
        with open(os.path.join(self.attrs, name, "current_value")) as f:
            return int(f.read())

    def test_discovery(self):
        self.assertTrue(self.limits.available())
        self.assertEqual(self.limits.tdp_range(), (5, 30))
        self.assertEqual(self.limits.profiles(),
                         ["low-power", "balanced", "performance", "custom"])
        self.assertEqual(self.limits.profile(), "balanced")
        self.assertEqual(self.limits.tdp(), 15)

    def test_set_tdp(self):
        self.assertTrue(self.limits.set_tdp(12))
        self.assertEqual(self.limits.profile(), "custom")
        self.assertEqual([self._cur(a) for a in power.ATTRS], [12, 12, 12])
        # Each limit is clamped to its own range
        self.assertTrue(self.limits.set_tdp(40))
        self.assertEqual((self._cur("ppt_pl1_spl"), self._cur("ppt_pl2_sppt"),
                          self._cur("ppt_pl3_fppt")), (30, 32, 40))
        self.assertTrue(self.limits.set_tdp(1))
        self.assertEqual(self._cur("ppt_pl1_spl"), 5)

    def test_set_profile(self):
        self.assertTrue(self.limits.set_profile("performance"))
        self.assertEqual(self.limits.profile(), "performance")
        self.assertFalse(self.limits.set_profile("turbo"))
        self.assertIn("not offered", self.limits.error)

    def test_unavailable(self):
        none = power.PowerLimits("/nonexistent", "/nonexistent", "/nonexistent")
        self.assertFalse(none.available())
        self.assertIn("power-mode driver", none.unavailable_reason())
        self.assertIsNone(none.tdp_range())
        self.assertFalse(none.set_tdp(10))
        no_attrs = power.PowerLimits("/nonexistent", self.pp, "/nonexistent")
        self.assertIn("power limits", no_attrs.unavailable_reason())

    def test_charger_detection(self):
        root = os.path.join(self.tmp, "ps")
        self.assertIsNone(battery.on_external_power(os.path.join(self.tmp, "none")))
        write(os.path.join(root, "BAT0", "type"), "Battery\n")
        write(os.path.join(root, "BAT0", "status"), "Discharging\n")
        self.assertFalse(battery.on_external_power(root))     # battery only
        write(os.path.join(root, "BAT0", "status"), "Not charging\n")
        self.assertTrue(battery.on_external_power(root))      # charge limit reached
        write(os.path.join(root, "ucsi-source-psy-USBC000:001", "type"), "USB\n")
        write(os.path.join(root, "ucsi-source-psy-USBC000:001", "online"), "0\n")
        write(os.path.join(root, "ADP0", "type"), "Mains\n")
        write(os.path.join(root, "ADP0", "online"), "0\n")
        self.assertFalse(battery.on_external_power(root))     # supplies say no
        write(os.path.join(root, "ucsi-source-psy-USBC000:001", "online"), "1\n")
        self.assertTrue(battery.on_external_power(root))
        write(os.path.join(root, "ucsi-source-psy-USBC000:001", "online"), "0\n")
        write(os.path.join(root, "hid-mouse", "type"), "USB\n")
        write(os.path.join(root, "hid-mouse", "scope"), "Device\n")
        write(os.path.join(root, "hid-mouse", "online"), "1\n")
        self.assertFalse(battery.on_external_power(root))     # a gadget's battery

    def test_cpu_boost(self):
        root = os.path.join(self.tmp, "cpu")
        self.assertIsNone(power.cpu_boost_state(root))
        self.assertFalse(power.set_cpu_boost(False, root))
        for i in range(2):
            write(os.path.join(root, "cpu%d" % i, "cpufreq", "boost"), "1\n")
        write(os.path.join(root, "cpuidle", "x"), "")
        self.assertTrue(power.cpu_boost_state(root))
        self.assertTrue(power.set_cpu_boost(False, root))
        self.assertFalse(power.cpu_boost_state(root))


class AutoTdpSimulation(unittest.TestCase):
    """Drive the controller with a simple game model: the frame rate grows
    with the watts until it hits the game's frame cap."""

    def _run(self, fps_per_watt, cap, target, seconds=400, lo=5, hi=30, noise=0.0):
        auto = power.AutoTdp(lo, hi, target)
        now = 0.0
        history = []
        for i in range(seconds):
            wobble = noise * (((i * 7919) % 13) / 6.0 - 1.0)
            fps = min(cap, fps_per_watt(auto.tdp)) + wobble
            auto.step(fps, now)
            history.append(auto.tdp)
            now += 1.0
        return auto, history

    def test_finds_the_lowest_tdp_that_holds_the_target(self):
        # 4 fps per watt, capped at 60: 15 W is the least that reaches 60
        auto, history = self._run(lambda w: 4.0 * w, 60, 60)
        tail = history[-120:]
        self.assertTrue(set(tail) <= {14, 15}, set(tail))
        self.assertGreater(tail.count(15), 100)           # rests at 15 W
        self.assertEqual(history[0], 30)                  # starts from the top

    def test_uncapped_game_is_brought_down_to_the_target(self):
        auto, history = self._run(lambda w: 5.0 * w, 999, 40)   # 8 W = 40 fps
        self.assertTrue(set(history[-100:]) <= {7, 8, 9}, set(history[-100:]))

    def test_goes_to_max_when_the_target_is_out_of_reach(self):
        auto, history = self._run(lambda w: 1.0 * w, 999, 60)
        self.assertEqual(history[-1], 30)
        self.assertEqual(auto.state, "at_max")

    def test_reaches_the_floor_for_light_games(self):
        auto, history = self._run(lambda w: 100.0, 100, 60, lo=8)
        self.assertEqual(history[-1], 8)
        self.assertEqual(auto.state, "at_min")

    def test_recovers_quickly_when_the_game_gets_heavier(self):
        auto = power.AutoTdp(5, 30, 60)
        now = 0.0
        for _ in range(300):                               # light scene
            auto.step(min(60, 6.0 * auto.tdp), now)
            now += 1.0
        self.assertLessEqual(auto.tdp, 11)
        steps = 0
        while min(60, 3.0 * auto.tdp) < 58.5 and steps < 60:   # heavy scene: needs 20 W
            auto.step(min(60, 3.0 * auto.tdp), now)
            now += 1.0
            steps += 1
        self.assertLess(steps, 20)
        self.assertGreaterEqual(auto.tdp, 20)

    def test_settles_despite_noise(self):
        auto, history = self._run(lambda w: 4.0 * w, 60, 60, seconds=900, noise=0.8)
        tail = history[-300:]
        self.assertTrue(set(tail) <= {14, 15, 16}, set(tail))
        changes = sum(1 for a, b in zip(tail, tail[1:]) if a != b)
        self.assertLess(changes, 12)                       # settled, not bouncing

    def test_no_measurement_holds_the_setting(self):
        auto = power.AutoTdp(5, 30, 60, start=18)
        for t in range(50):
            self.assertEqual(auto.step(None, float(t)), 18)
        self.assertEqual(auto.state, "waiting")

    def test_retarget(self):
        auto = power.AutoTdp(5, 30, 60, start=25)
        auto.retarget(5, 20, 60)
        self.assertEqual(auto.tdp, 20)
        auto.retarget(22, 30, 30)
        self.assertEqual((auto.tdp, auto.target), (22, 30.0))


class FakeGamescope(object):
    """A tiny Wayland server that implements what the probe uses."""

    def __init__(self, path, version=7, with_control=True):
        self.path = path
        self.version = version
        self.with_control = with_control
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.bind(path)
        self.sock.listen(2)
        self.pending = {}
        self.requests = 0
        self.bound = threading.Event()
        self.conn = None
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _send(self, obj, opcode, payload=b""):
        self.conn.sendall(struct.pack("<IHH", obj, opcode, 8 + len(payload)) + payload)

    def _serve(self):
        try:
            while True:
                conn, _ = self.sock.accept()
                self.conn = conn
                self.pending = {}
                buf = b""
                registry = None
                control = None
                while True:
                    data = conn.recv(4096)
                    if not data:
                        break
                    buf += data
                    while len(buf) >= 8:
                        obj, opcode, size = struct.unpack_from("<IHH", buf)
                        if len(buf) < size:
                            break
                        body, buf = buf[8:size], buf[size:]
                        if obj == 1 and opcode == 1:            # get_registry
                            (registry,) = struct.unpack("<I", body)
                            globs = [(1, "wl_compositor", 5)]
                            if self.with_control:
                                globs.append((7, "gamescope_control", self.version))
                            for name, iface, ver in globs:
                                self._send(registry, 0, struct.pack("<I", name)
                                           + fps_probe._string(iface) + struct.pack("<I", ver))
                        elif obj == 1 and opcode == 0:          # sync
                            (cb,) = struct.unpack("<I", body)
                            self._send(cb, 0, struct.pack("<I", 0))
                        elif obj == registry and opcode == 0:   # bind
                            (name,) = struct.unpack_from("<I", body)
                            iface, off = fps_probe._read_string(body, 4)
                            ver, control = struct.unpack_from("<II", body, off)
                            assert (name, iface) == (7, "gamescope_control"), (name, iface)
                            self._send(control, 0, struct.pack("<III", 7, 1, 0))  # feature
                            self.bound.set()
                        elif obj == control and opcode == 6:
                            (app,) = struct.unpack("<I", body)
                            with self.lock:
                                self.pending[app] = self.pending.get(app, 0) + 1
                                self.requests += 1
        except OSError:
            pass

    def present(self, app, frametime_s, control=4):
        """The app showed a frame: answer whoever asked (like gamescope)."""
        with self.lock:
            n = self.pending.pop(app, 0)
        ns = int(frametime_s * 1e9)
        for _ in range(n):
            self._send(control, 3, struct.pack("<III", app, ns & 0xFFFFFFFF, ns >> 32))
        return n

    def close(self):
        for s in (self.conn, self.sock):
            try:
                if s is not None:
                    s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                if s is not None:
                    s.close()
            except OSError:
                pass


class FpsProbe(TempDirCase):
    def _server(self, **kw):
        path = os.path.join(self.tmp, "1000", "gamescope-0")
        os.makedirs(os.path.dirname(path))
        server = FakeGamescope(path, **kw)
        self.addCleanup(server.close)
        return server, path

    def test_find_sockets(self):
        _, path = self._server()
        write(path + ".lock", "")
        self.assertEqual(fps_probe.find_sockets(self.tmp), [path])
        self.assertEqual(fps_probe.find_sockets("/nonexistent"), [])

    def test_measures_frame_rate(self):
        server, path = self._server()
        got = []
        stop = threading.Event()
        t = threading.Thread(
            target=fps_probe.measure,
            args=(4242, lambda fps, n: got.append((fps, n)), stop.is_set),
            kwargs={"socket_paths": lambda: [path], "window": 0.25}, daemon=True)
        t.start()
        self.assertTrue(server.bound.wait(3.0))
        end = time.monotonic() + 1.2
        while time.monotonic() < end:
            server.present(4242, 1 / 50.0)      # the game runs at 50 fps
            server.present(999, 1 / 10.0)       # another app: nobody asked
            time.sleep(0.004)
        stop.set()
        t.join(3.0)
        self.assertFalse(t.is_alive())
        rates = [fps for fps, n in got if fps is not None and n > 5]
        self.assertGreaterEqual(len(rates), 2)
        for fps in rates:
            self.assertAlmostEqual(fps, 50.0, delta=0.01)
        # Exactly one request per answered frame (plus the open one)
        frames = sum(n for _, n in got)
        self.assertLessEqual(abs(server.requests - frames), 60)

    def test_reports_nothing_without_frames(self):
        server, path = self._server()
        got = []
        stop = threading.Event()
        t = threading.Thread(
            target=fps_probe.measure, args=(1, lambda f, n: got.append((f, n)), stop.is_set),
            kwargs={"socket_paths": lambda: [path], "window": 0.1}, daemon=True)
        t.start()
        time.sleep(0.5)
        stop.set()
        t.join(3.0)
        self.assertTrue(got and all(f is None and n == 0 for f, n in got))
        self.assertEqual(server.requests, 1)        # asked once, never spammed

    def test_old_or_foreign_compositor(self):
        _, path = self._server(version=5)
        with self.assertRaises(fps_probe.ProbeError) as ctx:
            fps_probe.Connection(path)
        self.assertIn("too old", str(ctx.exception))

    def test_compositor_without_control(self):
        _, path = self._server(with_control=False)
        with self.assertRaises(fps_probe.ProbeError):
            fps_probe.Connection(path)

    def test_monitor_runs_the_probe_as_a_program(self):
        import sys
        script = os.path.join(self.tmp, "probe.py")
        write(script, "import sys, time\n"
                      "for i in range(200):\n"
                      "    print('%s.50 30' % sys.argv[1]); sys.stdout.flush(); time.sleep(0.02)\n")
        mon = power.FpsMonitor(python=sys.executable, script=script)
        self.addCleanup(mon.stop)
        self.assertIsNone(mon.get())
        mon.set_app(57)
        deadline = time.monotonic() + 3.0
        while mon.get() is None and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(mon.get(), 57.5)
        mon.set_app(58)                              # new game: new probe
        deadline = time.monotonic() + 3.0
        while mon.get() != 58.5 and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertEqual(mon.get(), 58.5)
        mon.set_app(None)
        self.assertIsNone(mon.get())

    def test_probe_program_end_to_end(self):
        import subprocess
        import sys
        server, path = self._server()
        script = os.path.join(os.path.dirname(fps_probe.__file__), "fps_probe.py")
        code = ("import sys; sys.argv=['fps_probe.py','77']\n"
                "import runpy, glob\n"
                "import importlib.util as u\n"
                "spec=u.spec_from_file_location('p', %r); m=u.module_from_spec(spec)\n"
                "spec.loader.exec_module(m)\n"
                "m.find_sockets=lambda root='/run/user': [%r]\n"
                "sys.exit(m.main(sys.argv))\n" % (script, path))
        proc = subprocess.Popen([sys.executable, "-u", "-c", code], stdout=subprocess.PIPE)
        self.addCleanup(proc.kill)
        self.assertTrue(server.bound.wait(5.0))
        end = time.monotonic() + 1.3
        while time.monotonic() < end:
            server.present(77, 1 / 40.0)
            time.sleep(0.004)
        proc.terminate()
        out = proc.communicate(timeout=5)[0].decode().split("\n")
        rates = [float(l.split()[0]) for l in out if l and not l.startswith("-")]
        self.assertTrue(rates)
        self.assertAlmostEqual(rates[-1], 40.0, delta=0.01)


class Config(unittest.TestCase):
    def test_defaults_are_valid_and_touch_nothing(self):
        d = cfg.defaults()
        self.assertEqual(cfg.sanitize(d), d)
        self.assertFalse(d["power"]["enabled"] or d["fan"]["enabled"])
        self.assertFalse(d["lighting"]["sticks"]["managed"])
        self.assertFalse(d["lighting"]["power_led"]["managed"])
        self.assertIsNone(d["battery"]["charge_limit"])
        self.assertFalse(d["controls"]["swap_menu"] or d["controls"]["remap_managed"])

    def test_garbage_is_rejected(self):
        d = cfg.sanitize({
            "lighting": {"sticks": {"mode": "disco", "color": [999, -5, "x"],
                                    "brightness": 7, "managed": 1},
                         "power_led": "on"},
            "battery": {"charge_limit": "yes"},
            "fan": {"enabled": True, "curve": [0] * 3},
            "power": {"enabled": True, "profiles": {
                "default": {"tdp": 900, "min_tdp": -3, "target_fps": 1, "mode": "rm -rf /"},
                "1234": {"tdp": 9}, "../etc": {"tdp": 1}, "abc": {}}},
            "controls": {"remap": {"Y1": "a", "Y2": "launch_missiles", "ZZ": "a"},
                         "touchpad": 3},
        })
        s = d["lighting"]["sticks"]
        self.assertEqual((s["mode"], s["color"], s["brightness"], s["managed"]),
                         ("solid", [0, 140, 255], 1.0, False))
        self.assertIsNone(d["battery"]["charge_limit"])
        self.assertEqual(d["fan"]["profiles"]["default"]["curve"], list(cfg.fan.DEFAULT_CURVE))
        p = d["power"]["profiles"]
        self.assertEqual(sorted(p), ["1234", "default"])
        self.assertEqual((p["default"]["tdp"], p["default"]["min_tdp"],
                          p["default"]["target_fps"], p["default"]["mode"]),
                         (cfg.TDP_ABS_MAX, cfg.TDP_ABS_MIN, 15, "custom"))
        self.assertEqual(p["1234"]["tdp"], 9)
        self.assertEqual(d["controls"]["remap"]["Y1"], "a")
        self.assertEqual(d["controls"]["remap"]["Y2"], "none")
        self.assertIsNone(d["controls"]["touchpad"])
        self.assertEqual(cfg.sanitize("nope"), cfg.defaults())

    def test_migration_to_version_2(self):
        old = {"version": 1, "controls": {"steam_controller": "default", "swap_menu": True}}
        d = cfg.sanitize(old)
        self.assertEqual(d["version"], 2)
        self.assertEqual(d["controls"]["steam_controller"], "deck-uhid")
        self.assertEqual(d["controls"]["swap_layout"], "steam_menu")
        self.assertTrue(d["controls"]["swap_menu"])
        self.assertEqual(cfg.sanitize({"version": 1, "controls": {"steam_controller": "ds5"}})
                         ["controls"]["steam_controller"], "ds5")
        # In version 2 files the Xbox pad is a deliberate choice
        self.assertEqual(cfg.sanitize({"version": 2, "controls": {"steam_controller": "xbox"}})
                         ["controls"]["steam_controller"], "xbox")
        self.assertEqual(cfg.sanitize(cfg.sanitize(old)), cfg.sanitize(old))

    def test_fan_profiles_schema(self):
        # 2.3 files: one curve at the top level becomes the default profile
        d = cfg.sanitize({"fan": {"enabled": True, "full_speed": True,
                                  "curve": list(cfg.fan.PRESETS["performance"])}})
        self.assertEqual(d["fan"]["profiles"], {"default": {
            "full_speed": True, "curve": list(cfg.fan.PRESETS["performance"]), "name": ""}})
        self.assertNotIn("curve", d["fan"])
        d = cfg.sanitize({"fan": {"profiles": {"7:ac": {}, "7": {"curve": [90] * 10},
                                               "abc": {}, "7:dc": {}, "../1": {}}}})
        self.assertEqual(sorted(d["fan"]["profiles"]), ["7", "7:ac", "default"])
        self.assertEqual(d["fan"]["profiles"]["7:ac"]["curve"][0], 90)   # from its game
        self.assertEqual(cfg.sanitize(d), d)

    def test_fan_curve_is_made_safe_on_load(self):
        d = cfg.sanitize({"fan": {"curve": [0] * 10}})
        self.assertEqual(d["fan"]["profiles"]["default"]["curve"], list(cfg.fan.SAFETY_FLOOR))


class FakeLimits(object):
    def __init__(self):
        self.mode = "balanced"
        self.value = 15
        self.writes = []
        self.ok = True
        self.error = None

    def available(self):
        return self.ok

    def unavailable_reason(self):
        return None if self.ok else "no driver"

    def profiles(self):
        return ["low-power", "balanced", "performance", "custom"]

    def profile(self):
        return self.mode

    def set_profile(self, name):
        self.mode = name
        self.writes.append(("mode", name))
        return True

    def tdp_range(self):
        return (5, 30)

    def tdp(self):
        return self.value

    def set_tdp(self, watts):
        self.mode = "custom"
        self.value = int(watts)
        self.writes.append(("tdp", int(watts)))
        return True


class FakeFan(object):
    def __init__(self):
        self.ok = True
        self.applied = []
        self.restored = 0
        self.error = None
        self.full_speed = False
        self.curve = None
        self.readable = True

    def read_full_speed(self):
        return self.full_speed if self.readable else None

    def read_curve(self):
        return self.curve if self.readable else None

    def available(self):
        return self.ok

    def apply(self, curve, full_speed=False, ensure_custom=True):
        self.applied.append((tuple(curve), full_speed, ensure_custom))
        if full_speed:
            self.full_speed = True
        else:
            self.full_speed = False
            self.curve = tuple(cfg.fan.sanitize_curve(curve))
        return True

    def restore(self):
        self.restored += 1
        return True


class FakeCharge(object):
    def __init__(self):
        self.enabled = False
        self.ok = True

    def status(self):
        return {"available": self.ok, "enabled": self.enabled if self.ok else None,
                "backend": "sysfs" if self.ok else None,
                "error": None if self.ok else "not exposed"}

    def set(self, enabled):
        if self.ok:
            self.enabled = bool(enabled)
        return self.status()


class FakeLed(object):
    def __init__(self):
        self.on = True
        self.available = True
        self.error = None
        self.history = []

    def get(self):
        return self.on

    def set(self, on):
        self.on = bool(on)
        self.history.append(self.on)
        return True

    def close(self):
        pass


class FakeSwap(object):
    def __init__(self):
        self.active = False
        self.layout = None
        self.running = True
        self.error = None
        self.loads = 0

    def available(self):
        return self.running

    def is_active(self, layout=None):
        if not self.running:
            return None
        return self.active and (layout is None or layout == self.layout)

    def apply(self, enabled, layout="steam_menu"):
        if not self.running:
            self.error = "InputPlumber is not running"
            return False
        if (self.active, self.layout) != (enabled, layout if enabled else self.layout):
            self.loads += 1
        self.active = enabled
        if enabled:
            self.layout = layout
        return True


class FakeSteamPad(object):
    def __init__(self):
        self.kind = "xbox"
        self.applies = []
        self.restarted = False
        self.error = None

    def supported(self):
        return ["deck-uhid", "ds5-edge", "ds5"]

    def needs_apply(self, kind):
        return self.restarted or kind != self.kind

    def apply(self, kind):
        self.kind = kind
        self.restarted = False
        self.applies.append(kind)
        return True


class FakeFps(object):
    def __init__(self):
        self.app = None
        self.value = None
        self.error = None

    def set_app(self, app):
        self.app = app

    def get(self):
        return self.value

    def stop(self):
        self.app = None

    @staticmethod
    def find_python():
        return "/usr/bin/python3"


class HubTests(TempDirCase):
    def setUp(self):
        TempDirCase.setUp(self)
        self.sent = []
        self.ac = False
        self.send_error = None
        self.boost = []
        self.mods = {
            "limits": FakeLimits(), "fan": FakeFan(), "charge": FakeCharge(),
            "led": FakeLed(), "swap": FakeSwap(), "fps": FakeFps(),
            "steam_pad": FakeSteamPad(),
            "send": self._send, "sensors": lambda: {"temp": 55, "rpm": None},
            "battery_info": lambda: {"percent": 80, "status": "Charging"},
            "set_boost": lambda v: self.boost.append(v) or True,
            "on_ac": lambda: self.ac,
            "boost_state": lambda: None,
        }
        self.path = os.path.join(self.tmp, "toolbox.json")
        self.controller_present = True
        log = logging.getLogger("hubtest")
        log.addHandler(logging.NullHandler())
        log.propagate = False
        self.hub = Hub(self.path, logger=log, product="83E1", modules=self.mods,
                       controller_status=lambda: self.controller_present)

    def _send(self, cmds):
        if self.send_error is None:
            self.sent.extend(cmds)
        return self.send_error

    def test_fresh_install_only_sets_the_steam_controller(self):
        self.hub.apply_all("test")
        m = self.mods
        self.assertEqual((m["limits"].writes, m["fan"].applied, m["led"].history,
                          self.sent, m["swap"].loads, m["charge"].enabled),
                         ([], [], [], [], 0, False))
        self.assertEqual(m["steam_pad"].applies, ["deck-uhid"])   # the default
        self.assertEqual(self.hub.errors, {})

    def test_capabilities(self):
        caps = self.hub.capabilities(refresh=True)
        for key in ("power", "fan", "charge_limit", "power_led", "sticks", "remap", "swap_menu"):
            self.assertTrue(caps[key]["available"], key)
        self.mods["fan"].ok = False
        self.mods["swap"].running = False
        caps = self.hub.capabilities(refresh=True)
        self.assertIn("acpi_call", caps["fan"]["reason"])
        self.assertEqual(caps["swap_menu"]["reason"], "InputPlumber is not running")
        other = Hub(self.path, product="Jupiter", modules=self.mods)
        caps = other.capabilities(refresh=True)
        self.assertFalse(caps["power"]["available"] or caps["fan"]["available"]
                         or caps["sticks"]["available"] or caps["charge_limit"]["available"])
        self.assertIn("Jupiter", caps["power"]["reason"])

    def test_charge_limit_and_power_led(self):
        self.hub.update({"battery": {"charge_limit": True}})
        self.assertTrue(self.mods["charge"].enabled)
        self.assertTrue(self.hub.status()["battery"]["charge_limit"])
        self.hub.update({"lighting": {"power_led": {"managed": True, "on": False}}})
        self.assertEqual(self.mods["led"].history, [False])
        self.assertFalse(self.hub.status()["power_led"])
        self.assertTrue(cfg.load(self.path)["battery"]["charge_limit"])     # persisted

    def test_power_led_sleeps_and_wakes(self):
        led = self.mods["led"]
        self.hub.on_suspend()
        self.assertEqual(led.history, [])                  # option off: untouched
        self.hub.update({"lighting": {"power_led": {"off_in_sleep": True}}})
        self.hub.on_suspend()
        self.assertEqual(led.history, [False])
        self.hub.on_resume()
        self.assertEqual(led.history, [False, True])
        # A light the user keeps off stays off after waking
        self.hub.update({"lighting": {"power_led": {"managed": True, "on": False}}})
        del led.history[:]
        self.hub.on_suspend()
        self.hub.on_resume()
        self.assertEqual(led.history, [])

    def test_stick_lights(self):
        self.hub.update({"lighting": {"sticks": {"managed": True, "mode": "spiral",
                                                 "color": [1, 2, 3]}}})
        self.assertEqual(len(self.sent), 6)
        self.assertEqual(self.sent[0][5:9], bytes([4, 1, 2, 3]))
        del self.sent[:]
        self.hub.update({"lighting": {"sticks": {"on": False}}})
        self.assertEqual(self.sent, controller.rgb_off_commands())
        self.send_error = "Legion Go controllers not found"
        self.hub.update({"lighting": {"sticks": {"on": True}}})
        self.assertEqual(self.hub.errors["sticks"], "Legion Go controllers not found")
        self.send_error = None
        self.hub.update({"lighting": {"sticks": {"brightness": 0.5}}})
        self.assertNotIn("sticks", self.hub.errors)

    def test_remap(self):
        self.hub.update({"controls": {"remap_managed": True, "remap": {"Y1": "a", "M3": "rb"}}})
        self.assertEqual(len(self.sent), 5)
        self.assertIn(bytes.fromhex("05076c02031c1201"), self.sent)
        self.assertIn(bytes.fromhex("05076c0204221801"), self.sent)
        del self.sent[:]
        # Turning the feature off puts every button back to "no action"
        self.hub.update({"controls": {"remap_managed": False}})
        self.assertEqual(self.sent, [controller.remap_command(b, "none")
                                     for b in controller.REMAP_BUTTONS])
        del self.sent[:]
        self.hub.update({"controls": {"touchpad": False}})
        self.assertEqual(self.sent, [controller.touchpad_command(False)])

    def test_swap_layouts(self):
        swap = self.mods["swap"]
        self.hub.update({"controls": {"swap_menu": True}})
        self.assertEqual((swap.active, swap.layout), (True, "steam_menu"))   # default
        self.assertTrue(self.hub.status()["swap_menu"]["active"])
        self.hub.update({"controls": {"swap_layout": "steam_view"}})
        self.assertEqual(swap.layout, "steam_view")
        swap.layout = "steam_menu"                # someone loaded the other one
        self.hub._tick(100.0)
        self.assertEqual(swap.layout, "steam_view")
        self.assertEqual(cfg.sanitize({"controls": {"swap_layout": "x"}})
                         ["controls"]["swap_layout"], "steam_menu")

    def test_swap_is_kept_alive(self):
        swap = self.mods["swap"]
        self.hub.update({"controls": {"swap_menu": True}})
        self.assertTrue(swap.active)
        swap.active = False                       # InputPlumber restarted
        self.hub._tick(100.0)
        self.assertTrue(swap.active)
        self.hub.update({"controls": {"swap_menu": False}})
        self.assertFalse(swap.active)
        swap.running = False
        self.hub.update({"controls": {"swap_menu": True}})
        self.assertEqual(self.hub.errors["swap_menu"], "InputPlumber is not running")

    def test_steam_gyro_controller_type(self):
        pad, swap = self.mods["steam_pad"], self.mods["swap"]
        self.assertEqual(self.hub.capabilities(True)["steam_gyro"]["types"],
                         ["deck-uhid", "ds5-edge", "ds5"])
        self.hub.apply_all("test")
        self.assertEqual(pad.applies, ["deck-uhid"])      # the default
        self.hub.update({"controls": {"steam_controller": "xbox"}})
        self.assertEqual(pad.applies, ["deck-uhid", "xbox"])
        self.hub.apply_all("test")
        self.assertEqual(pad.applies, ["deck-uhid", "xbox"])   # nothing to change
        del pad.applies[:]
        self.hub.update({"controls": {"swap_menu": True}})
        orig_sleep = time.sleep
        time.sleep = lambda s: None
        self.addCleanup(setattr, time, "sleep", orig_sleep)
        swap.active = False                               # new pad drops the profile
        self.hub.update({"controls": {"steam_controller": "deck-uhid"}})
        self.assertEqual(pad.applies, ["deck-uhid"])
        self.assertTrue(swap.active)                      # swap put back on top
        self.hub._tick(500.0)
        self.hub.apply_all("resume")
        self.assertEqual(pad.applies, ["deck-uhid"])      # no needless reconnects
        pad.restarted = True                              # InputPlumber restarted
        self.hub._tick(600.0)
        self.assertEqual(pad.applies, ["deck-uhid", "deck-uhid"])
        self.hub.update({"controls": {"steam_controller": "xbox"}})
        self.assertEqual(pad.applies[-1], "xbox")
        self.assertEqual(cfg.sanitize({"controls": {"steam_controller": "ps9"}})
                         ["controls"]["steam_controller"], "deck-uhid")

    def test_per_game_controller_type(self):
        pad, swap = self.mods["steam_pad"], self.mods["swap"]
        orig_sleep = time.sleep
        time.sleep = lambda s: None
        self.addCleanup(setattr, time, "sleep", orig_sleep)
        self.hub.apply_all("test")
        self.assertEqual(pad.applies, ["deck-uhid"])
        self.hub.update({"controls": {"swap_menu": True}})
        # Elden Ring gets a DualSense Edge while it runs
        self.hub.set_running_app(1245620, "ELDEN RING")
        self.assertEqual(self.hub.steam_controller_for(), ("deck-uhid", "default"))
        swap.active = False
        out = self.hub.set_game_controller(1245620, "ds5-edge", "ELDEN RING")
        self.assertEqual(out["controls"]["steam_controller_games"],
                         {"1245620": {"type": "ds5-edge", "name": "ELDEN RING"}})
        self.assertEqual(pad.applies[-1], "ds5-edge")
        self.assertTrue(swap.active)                   # swap put back on top
        self.assertEqual(self.hub.status()["steam_controller"],
                         {"type": "ds5-edge", "source": "game"})
        # Back to the library: the default again
        self.hub.set_running_app(None, "")
        self.assertEqual(pad.applies[-1], "deck-uhid")
        # Another game without its own type: no reconnect at all
        n = len(pad.applies)
        self.hub.set_running_app(400, "Portal")
        self.assertEqual(len(pad.applies), n)
        # Starting Elden Ring again picks its type up by itself
        self.hub.set_running_app(1245620, "ELDEN RING")
        self.assertEqual(pad.applies[-1], "ds5-edge")
        self.hub._tick(900.0)
        self.assertEqual(pad.applies[-1], "ds5-edge")  # keep-alive changes nothing
        self.assertEqual(len(pad.applies), n + 1)
        # Saved across restarts
        self.assertIn("1245620", cfg.load(self.path)["controls"]["steam_controller_games"])
        # Removing it returns that game to the default
        out = self.hub.set_game_controller(1245620, None)
        self.assertEqual(out["controls"]["steam_controller_games"], {})
        self.assertEqual(pad.applies[-1], "deck-uhid")
        # Changing the default while a game with its own type runs: no switch
        self.hub.set_game_controller(1245620, "ds5", "ELDEN RING")
        n = len(pad.applies)
        self.hub.update({"controls": {"steam_controller": "xbox"}})
        self.assertEqual(len(pad.applies), n)
        self.hub.set_running_app(None, "")
        self.assertEqual(pad.applies[-1], "xbox")

    def test_per_game_controller_sanitizing(self):
        d = cfg.sanitize({"version": 2, "controls": {"steam_controller_games": {
            "123": {"type": "ds5", "name": "A" * 300}, "../x": {"type": "ds5"},
            "456": {"type": "ps9"}, "789": "ds5", "42": {"type": "xbox", "name": 7}}}})
        games = d["controls"]["steam_controller_games"]
        self.assertEqual(sorted(games), ["123", "42"])
        self.assertEqual(len(games["123"]["name"]), 120)
        self.assertEqual(games["42"], {"type": "xbox", "name": ""})

    def test_manual_tdp_and_modes(self):
        limits = self.mods["limits"]
        self.hub.update({"power": {"enabled": True, "profiles": {"default": {"tdp": 12}}}})
        self.assertEqual((limits.mode, limits.value), ("custom", 12))
        self.hub.update({"power": {"profiles": {"default": {"tdp": 99}}}})
        self.assertEqual(limits.value, 30)        # clamped to the device range
        self.hub.update({"power": {"profiles": {"default": {"mode": "performance"}}}})
        self.assertEqual(limits.mode, "performance")
        self.hub.update({"power": {"cpu_boost": False}})
        self.assertEqual(self.boost, [False])
        # Something else changes the mode: it is put back
        limits.mode = "balanced"
        self.hub._tick(time.monotonic() + 60)
        self.assertEqual(limits.mode, "performance")
        # Disabling hands the device back in a normal mode
        self.hub.update({"power": {"profiles": {"default": {"mode": "custom"}}}})
        self.hub.update({"power": {"enabled": False}})
        self.assertEqual(limits.mode, "balanced")

    def test_per_game_profiles(self):
        limits = self.mods["limits"]
        self.hub.update({"power": {"enabled": True, "per_game": True, "profiles": {
            "default": {"tdp": 20}, "1091500": {"tdp": 9}}}})
        self.assertEqual(limits.value, 20)
        self.hub.set_running_app(1091500, "Some Game")
        self.assertEqual(limits.value, 9)
        self.assertEqual(self.hub.status()["power"]["profile_key"], "1091500")
        self.hub.set_running_app(7, "Other")
        self.assertEqual(limits.value, 20)
        self.hub.set_running_app(1091500, "Some Game")
        self.hub.delete_power_profile("1091500")
        self.assertEqual(limits.value, 20)
        self.assertNotIn("1091500", self.hub.get_settings()["power"]["profiles"])
        self.hub.set_running_app(None, "")
        self.assertIsNone(self.hub.status()["app"]["id"])

    def test_auto_tdp(self):
        limits, fps = self.mods["limits"], self.mods["fps"]
        self.hub.update({"power": {"enabled": True, "profiles": {"default": {
            "tdp": 25, "min_tdp": 8, "auto": True, "target_fps": 60}}}})
        self.assertEqual(limits.value, 25)            # no game: the set maximum
        self.assertIsNone(fps.app)
        self.hub.set_running_app(321, "Game")
        self.assertEqual(fps.app, 321)
        now = 1000.0
        for _ in range(400):
            fps.value = min(60.0, 5.0 * limits.value)     # needs 12 W for 60 fps
            self.hub._auto_step(now)
            now += 1.0
        self.assertIn(limits.value, (12, 13))
        st = self.hub.status()["power"]
        self.assertTrue(st["auto"]["active"])
        self.assertEqual(st["auto"]["fps"], fps.value)
        self.assertGreaterEqual(min(v for k, v in limits.writes if k == "tdp"), 8)
        # Back in the menu: full configured power again
        self.hub.set_running_app(None, "")
        self.hub._auto_step(now)
        self.assertEqual(limits.value, 25)
        self.hub.update({"power": {"profiles": {"default": {"auto": False}}}})
        self.assertIsNone(fps.app)
        self.assertFalse(self.hub.status()["power"]["auto"]["active"])

    def test_fan(self):
        f = self.mods["fan"]
        self.hub.update({"fan": {"enabled": True, "curve": [0] * 10}})
        self.assertEqual(f.applied[-1], (cfg.fan.SAFETY_FLOOR, False, True))
        # With the power module in a firmware mode the table is not forced
        self.hub.update({"power": {"enabled": True, "profiles": {"default": {
            "mode": "balanced"}}}})
        n = len(f.applied)
        self.hub.update({"fan": {"curve": list(cfg.fan.PRESETS["silent"])}})
        self.assertEqual(len(f.applied), n)
        # Entering custom mode re-applies it
        self.hub.update({"power": {"profiles": {"default": {"mode": "custom"}}}})
        self.assertEqual(f.applied[-1], (cfg.fan.PRESETS["silent"], False, False))
        self.hub.update({"fan": {"enabled": False}})
        self.assertEqual(f.restored, 1)
        f.ok = False
        self.hub.update({"fan": {"enabled": True}})
        self.assertIn("acpi_call", self.hub.errors["fan"])

    def test_fan_profiles_per_game_and_charger(self):
        f = self.mods["fan"]
        P = cfg.fan.PRESETS
        self.hub.update({"fan": {"enabled": True, "per_game": True, "per_power": True,
                                 "profiles": {
                                     "default": {"curve": list(P["silent"])},
                                     "default:ac": {"curve": list(P["performance"])},
                                     "1245620": {"curve": list(P["balanced"]), "name": "ELDEN RING"},
                                     "1245620:ac": {"full_speed": True}}}})
        self.assertEqual(f.applied[-1][:2], (P["silent"], False))      # library, battery
        # Plug in: the charger profile
        self.ac = True
        self.hub._tick(1000.0)
        self.assertEqual(f.applied[-1][:2], (P["performance"], False))
        self.assertEqual(self.hub.status()["fan"]["profile_key"], "default:ac")
        # Start the game on the charger: its charger profile (full speed)
        self.hub.set_running_app(1245620, "ELDEN RING")
        self.assertEqual(f.applied[-1][1], True)
        # Unplug: the game's battery profile
        self.ac = False
        self.hub._tick(1003.0)
        self.assertEqual(f.applied[-1][:2], (P["balanced"], False))
        # Nothing changes: nothing is re-sent
        n = len(f.applied)
        self.hub._tick(1006.0)
        self.hub.set_running_app(1245620, "ELDEN RING")
        self.assertEqual(len(f.applied), n)
        # A game without its own profile on battery: the default
        self.hub.set_running_app(400, "Portal")
        self.assertEqual(f.applied[-1][:2], (P["silent"], False))
        # Removing the game's battery profile: it falls back to the default
        self.hub.set_running_app(1245620, "ELDEN RING")
        self.hub.delete_fan_profile("1245620")
        self.assertEqual(f.applied[-1][:2], (P["silent"], False))
        self.assertNotIn("1245620", self.hub.get_settings()["fan"]["profiles"])
        # Its charger profile is still used on the charger
        self.ac = True
        self.hub._tick(1010.0)
        self.assertEqual(f.applied[-1][1], True)
        # With the separations off everything uses the default
        self.hub.update({"fan": {"per_game": False, "per_power": False}})
        self.assertEqual(f.applied[-1][:2], (P["silent"], False))
        self.assertEqual(self.hub.fan_profile_chain(), ["default"])
        # Saved
        self.assertIn("1245620:ac", cfg.load(self.path)["fan"]["profiles"])

    def test_fan_profile_chain(self):
        self.hub.update({"fan": {"per_game": True, "per_power": True}})
        self.assertEqual(self.hub.fan_profile_chain(app=5, ac=True),
                         ["5:ac", "5", "default:ac", "default"])
        self.assertEqual(self.hub.fan_profile_chain(app=5, ac=False), ["5", "default"])
        self.hub.set_running_app(None, "")
        self.assertEqual(self.hub.fan_profile_chain(ac=True), ["default:ac", "default"])

    def test_fan_watchdog_restores_full_speed(self):
        f = self.mods["fan"]
        self.hub.update({"fan": {"enabled": True, "full_speed": True}})
        self.assertTrue(f.full_speed)
        n = len(f.applied)
        self.hub._check_fan(10.0)
        self.assertEqual(len(f.applied), n)            # still on: nothing to do
        f.full_speed = False                           # firmware dropped it
        self.hub._check_fan(20.0)
        self.assertTrue(f.full_speed)
        self.assertEqual(f.applied[-1][1], True)
        st = self.hub.status()["fan"]
        self.assertEqual(st["restores"], 1)
        f.readable = False                             # cannot read: leave it
        f.full_speed = False
        self.hub._check_fan(30.0)
        self.assertFalse(f.full_speed)

    def test_fan_watchdog_gives_up_when_full_speed_never_reads_on(self):
        f = self.mods["fan"]
        self.hub.update({"fan": {"enabled": True, "full_speed": True}})
        f.read_full_speed = lambda: False              # firmware never reports it
        self.hub._check_fan(10.0)
        n = len(f.applied)
        for t in (20.0, 30.0, 40.0):
            self.hub._check_fan(t)
        self.assertEqual(len(f.applied), n)            # sent once, then stopped
        self.assertEqual(self.hub.status()["fan"]["restores"], 1)
        self.hub.update({"fan": {"full_speed": False}})
        self.assertFalse(self.hub._fan_unverifiable)   # a new setting is checked again

    def test_fan_watchdog_restores_the_curve(self):
        f = self.mods["fan"]
        self.hub.update({"fan": {"enabled": True,
                                 "curve": list(cfg.fan.PRESETS["performance"])}})
        n = len(f.applied)
        self.hub._check_fan(10.0)
        self.assertEqual(len(f.applied), n)
        f.curve = cfg.fan.SAFETY_FLOOR                 # mode change: stock table
        self.hub._check_fan(20.0)
        self.assertEqual(f.curve, cfg.fan.PRESETS["performance"])
        # A firmware mode the plugin chose on purpose is left alone
        self.hub.update({"power": {"enabled": True, "profiles": {"default": {
            "mode": "balanced"}}}})
        f.curve = cfg.fan.SAFETY_FLOOR
        n = len(f.applied)
        self.hub._check_fan(30.0)
        self.assertEqual(len(f.applied), n)

    def test_fan_watchdog_gives_up_on_unreadable_tables(self):
        f = self.mods["fan"]
        self.hub.update({"fan": {"enabled": True}})
        orig = f.read_curve
        f.read_curve = lambda: (1,) * 10               # never matches
        self.hub._check_fan(10.0)
        n = len(f.applied)
        self.hub._check_fan(20.0)
        self.assertEqual(len(f.applied), n)            # stopped re-applying
        f.read_curve = orig
        self.hub.update({"fan": {"curve": list(cfg.fan.PRESETS["silent"])}})
        f.curve = (0,) * 10
        self.hub._check_fan(30.0)                      # checks again after a change
        self.assertEqual(f.curve, cfg.fan.PRESETS["silent"])

    def test_everything_is_reapplied_after_resume_and_reconnect(self):
        self.hub.update({
            "battery": {"charge_limit": True},
            "lighting": {"sticks": {"managed": True}, "power_led": {"managed": True}},
            "controls": {"swap_menu": True, "remap_managed": True},
            "power": {"enabled": True},
            "fan": {"enabled": True},
        })
        m = self.mods
        m["charge"].enabled = False
        m["swap"].active = False
        m["limits"].mode = "balanced"
        del self.sent[:]
        self.hub.on_resume()
        self.hub._tick(time.monotonic() + 10)
        self.assertTrue(m["charge"].enabled and m["swap"].active)
        self.assertEqual(m["limits"].mode, "custom")
        self.assertEqual(len(self.sent), 6 + 5)
        # Controller comes back after being off
        del self.sent[:]
        now = time.monotonic() + 100
        self.controller_present = False
        self.hub._tick(now)
        self.controller_present = True
        self.hub._tick(now + 1)
        self.assertEqual(self.sent, [])
        self.hub._tick(now + 4)
        self.assertEqual(len(self.sent), 6 + 5)

    def test_reset(self):
        self.hub.update({"power": {"enabled": True}, "fan": {"enabled": True},
                         "controls": {"swap_menu": True}})
        out = self.hub.reset()
        self.assertEqual(out, cfg.defaults())
        self.assertFalse(self.mods["swap"].active)
        self.assertEqual(self.mods["fan"].restored, 1)
        self.assertEqual(cfg.load(self.path), cfg.defaults())

    def test_thread_lifecycle(self):
        self.hub.start()
        time.sleep(0.2)
        self.hub.stop()
        self.assertIsNone(self.hub._thread)


if __name__ == "__main__":
    unittest.main()
