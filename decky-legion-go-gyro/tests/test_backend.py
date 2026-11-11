"""Backend tests. Run with:  python3 -m unittest discover -s tests -v"""

import json
import logging
import math
import os
import shutil
import socket
import tempfile
import time
import unittest

import helpers  # noqa: F401  (sets up sys.path)
from helpers import DsuClient, make_report

from lgo_gyro import legion_hid, motion, settings as cfg, tablet
from lgo_gyro.dsu import DsuServer, PadState, battery_level, fill_inputs
from lgo_gyro.service import GyroService, HidHandle

G_LSB = 1.0 / legion_hid.ACCEL_G_PER_LSB          # raw counts per g
DPS_LSB = 1.0 / legion_hid.GYRO_DPS_PER_LSB       # raw counts per deg/s


def free_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class ReportParsing(unittest.TestCase):
    def test_rejects_other_reports(self):
        self.assertIsNone(legion_hid.parse_report(b""))
        self.assertIsNone(legion_hid.parse_report(bytes(60)))
        other = bytearray(make_report())
        other[2] = 0x6A  # reply to a configuration command
        self.assertIsNone(legion_hid.parse_report(bytes(other)))
        self.assertIsNone(legion_hid.parse_report(make_report()[:40]))

    def test_accepts_64_byte_reads(self):
        self.assertIsNotNone(legion_hid.parse_report(make_report() + bytes(4)))

    def test_connection_states(self):
        r = legion_hid.parse_report(make_report(left_state=2, right_state=3))
        self.assertTrue(r.left.connected and r.left.attached)
        self.assertTrue(r.right.connected and not r.right.attached)
        r = legion_hid.parse_report(make_report(left_state=1, right_state=1))
        self.assertFalse(r.left.connected or r.right.connected)
        self.assertEqual(r.left.battery, 80)
        self.assertEqual(r.right.battery, 60)

    def test_documented_sample_reports(self):
        # Captures from the hhd Legion Go notes
        both_attached = bytes.fromhex(
            "0419740100640464040101010202" "80808080" "000000" "00" "0000"
            "02" "80" "00000000" "80808080"
            "00ff33330e04011a0c0000c4142100c40000000000000000000000000000")
        r = legion_hid.parse_report(both_attached)
        self.assertTrue(r.left.attached and r.right.attached)
        self.assertEqual((r.left.battery, r.right.battery), (100, 100))
        left_detached = bytes.fromhex(
            "0419740100630163040101010302" "80808080" "000000" "00" "0000"
            "02" "80" "00000000" "80808080"
            "00ff013313050131000100c4142100c40000000000000000000000000000")
        r = legion_hid.parse_report(left_detached)
        self.assertTrue(r.left.connected and not r.left.attached)
        self.assertTrue(r.right.attached)
        both_off = bytes.fromhex(
            "0419740100000000000100000101" "80808080" "000000" "00" "0000"
            "02" "00" "00000000" "80808080"
            "00ff01010e04011c0c0000c4142100e40000000000000000000000000000")
        r = legion_hid.parse_report(both_off)
        self.assertFalse(r.left.connected or r.right.connected)

    def test_left_axes(self):
        # report order X, Z, Y - every axis inverted
        r = legion_hid.parse_report(make_report(
            left_raw_accel=(100, 200, 300), left_raw_gyro=(-10, -20, -30)))
        self.assertEqual(r.left.accel, (-100, -300, -200))
        self.assertEqual(r.left.gyro, (10, 30, 20))

    def test_right_axes(self):
        # report order Z, X, Y - Z keeps its sign
        r = legion_hid.parse_report(make_report(
            right_raw_accel=(100, 200, 300), right_raw_gyro=(-10, -20, -30)))
        self.assertEqual(r.right.accel, (-200, -300, 100))
        self.assertEqual(r.right.gyro, (20, 30, -10))

    def test_axis_maps_are_proper_rotations(self):
        # A sensor is a rigid body: its mapping into the gamepad frame must
        # be a pure rotation (determinant +1) for accel and gyro alike.
        def det(m):
            return (m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
                    - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
                    + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0]))

        for side in ("left", "right"):
            for kind in ("accel", "gyro"):
                cols = []
                for i in range(3):
                    raw = [0, 0, 0]
                    raw[i] = 1
                    rep = legion_hid.parse_report(make_report(**{
                        "%s_raw_%s" % (side, kind): tuple(raw)}))
                    cols.append(getattr(getattr(rep, side), kind))
                m = [[cols[c][r] for c in range(3)] for r in range(3)]
                self.assertEqual(det(m), 1, (side, kind))

    def test_buttons(self):
        r = legion_hid.parse_report(make_report(
            b18=0x80 | 0x08, b19=0x80 | 0x04, b20=0x01,
            sticks=(1, 2, 3, 4), lt=50, rt=200, touch=(500, 1000)))
        b = r.buttons
        self.assertTrue(b.legion_l and b.up and b.a and b.lt_d and b.menu)
        self.assertFalse(b.legion_r or b.down or b.b or b.rt_d or b.view)
        self.assertEqual((b.lx, b.ly, b.rx, b.ry), (1, 2, 3, 4))
        self.assertEqual((b.lt, b.rt), (50, 200))
        self.assertEqual((b.touch_x, b.touch_y), (500, 1000))

    def test_commands(self):
        self.assertEqual(
            legion_hid.cmd_enable_gyro("left"),
            [bytes.fromhex("05066a02030101"), bytes.fromhex("05066a07030201")])
        self.assertEqual(
            legion_hid.cmd_enable_gyro("right"),
            [bytes.fromhex("05066a02040101"), bytes.fromhex("05066a07040201")])
        self.assertEqual(
            legion_hid.cmd_disable_gyro("right"),
            [bytes.fromhex("05066a07040101")])


class Discovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _add(self, name, hid_id, desc, iface):
        usb = os.path.join(self.tmp, "usb", "1-3:1.%d" % iface, "0003:%s.%s" % (
            hid_id, name))
        os.makedirs(usb)
        with open(os.path.join(usb, "uevent"), "w") as f:
            f.write("DRIVER=hid-generic\nHID_ID=%s\nHID_NAME=x\n" % hid_id)
        with open(os.path.join(usb, "report_descriptor"), "wb") as f:
            f.write(desc)
        node = os.path.join(self.tmp, "class", name)
        os.makedirs(node)
        os.symlink(usb, os.path.join(node, "device"))
        with open(os.path.join(node, "dev"), "w") as f:
            f.write("243:%d\n" % iface)

    def test_picks_vendor_interface(self):
        gamepad = b"\x05\x01\x09\x05\xa1\x01"
        vendor = b"\x06\xa0\xff\x09\x01\xa1\x01"
        self._add("hidraw0", "0003:000017EF:00006182", gamepad, 0)
        self._add("hidraw1", "0003:000017EF:00006182", gamepad, 1)
        self._add("hidraw2", "0003:000017EF:00006182", vendor, 2)
        self._add("hidraw3", "0003:0000046D:0000C52B", vendor, 2)  # other vendor
        c = legion_hid.find_candidates(os.path.join(self.tmp, "class"))
        self.assertEqual([x.name for x in c], ["hidraw2"])
        self.assertEqual(c[0].mode, "xinput")
        self.assertEqual(c[0].devnum, (243, 2))

    def test_falls_back_to_interface_number(self):
        gamepad = b"\x05\x01\x09\x05\xa1\x01"
        self._add("hidraw4", "0003:000017EF:000061EC", gamepad, 0)
        self._add("hidraw5", "0003:000017EF:000061EC", gamepad, 2)
        c = legion_hid.find_candidates(os.path.join(self.tmp, "class"))
        self.assertEqual([x.name for x in c], ["hidraw5"])
        self.assertEqual(c[0].mode, "dinput")

    def test_no_devices(self):
        self.assertEqual(legion_hid.find_candidates(os.path.join(self.tmp, "x")), [])


class MotionMath(unittest.TestCase):
    def test_rotation(self):
        v = (1.0, 2.0, 3.0)
        self.assertEqual(motion.rotate(v, 0), v)
        self.assertEqual(motion.rotate(motion.rotate(v, 90), 270), v)
        self.assertEqual(motion.rotate(motion.rotate(v, 180), 180), v)
        self.assertEqual(motion.rotate(motion.rotate(v, 90), 90), motion.rotate(v, 180))
        # Held turned 90 degrees clockwise the top edge (-Z) points right (+X)
        self.assertEqual(motion.rotate((0, 0, -1), 90), (1, 0, 0))
        # The face normal never changes
        for deg in motion.ROTATIONS:
            self.assertEqual(motion.rotate((0, 1, 0), deg), (0, 1, 0))

    def test_dsu_convention(self):
        # Flat on a table (gamepad frame +1 g on Y) must read -1 g on DSU Y:
        # that is what a DualShock 4 reports and what Dolphin calls
        # "Accel Up".
        self.assertEqual(motion.to_dsu((0, 1, 0), (0, 0, 0))[:3], (0, -1, 0))
        # Pitching up (right handed around +X) is positive DSU pitch,
        # turning right (clockwise seen from above) positive DSU yaw,
        # rolling right positive DSU roll.
        self.assertEqual(motion.to_dsu((0, 0, 0), (5, 0, 0))[3:], (5, 0, 0))
        self.assertEqual(motion.to_dsu((0, 0, 0), (0, -5, 0))[3:], (0, 5, 0))
        self.assertEqual(motion.to_dsu((0, 0, 0), (0, 0, -5))[3:], (0, 0, 5))

    def _sensor_with(self, gyro, accel=(0.0, 1.0, 0.0)):
        s = motion.Sensor("x")
        s.gyro = gyro
        s.accel = accel
        return s

    def test_process_options(self):
        s = self._sensor_with((10.0, -20.0, -30.0))
        base = motion.process(s, motion.OutputOptions())
        self.assertEqual(base, (0.0, -1.0, 0.0, 10.0, 20.0, 30.0))
        o = motion.OutputOptions({"sensitivity": 2.0, "invert_yaw": True})
        self.assertEqual(motion.process(s, o)[3:], (20.0, -40.0, 60.0))
        o = motion.OutputOptions({"invert_pitch": True, "invert_roll": True})
        self.assertEqual(motion.process(s, o)[3:], (-10.0, 20.0, -30.0))

    def test_deadzone(self):
        o = motion.OutputOptions({"deadzone": 2.0})
        self.assertEqual(motion.process(self._sensor_with((1.0, 1.0, 0.0)), o)[3:],
                         (0.0, 0.0, 0.0))
        p, y, r = motion.process(self._sensor_with((10.0, 0.0, 0.0)), o)[3:]
        self.assertAlmostEqual(p, 8.0)
        self.assertEqual((y, r), (0.0, 0.0))

    def _feed(self, sensor, n, gyro_raw, accel_raw, start=0.0, auto=True, ts0=0,
              jitter=0):
        now = start
        for i in range(n):
            side = legion_hid.SideSample()
            side.connected = True
            side.imu_ts = (ts0 + i) & 0xFF
            j = (i % 3 - 1) * jitter
            side.gyro = tuple(g + j for g in gyro_raw)
            side.accel = accel_raw
            sensor.feed_controller(side, now, auto)
            now += 0.008
        return now

    def test_controller_scaling_and_clock(self):
        s = motion.Sensor("left")
        self._feed(s, 3, (int(100 * DPS_LSB), 0, 0), (0, int(G_LSB), 0), auto=False)
        self.assertAlmostEqual(s.gyro[0], 100.0, delta=0.1)
        self.assertAlmostEqual(s.accel[1], 1.0, delta=0.001)
        t0 = s.timestamp_us
        self._feed(s, 2, (0, 0, 0), (0, int(G_LSB), 0), start=0.024, auto=False, ts0=3)
        self.assertEqual(s.timestamp_us - t0, 16000)  # 2 samples x 8 ms

    def test_duplicate_timestamp_is_not_a_sample(self):
        s = motion.Sensor("left")
        side = legion_hid.SideSample()
        side.connected = True
        side.imu_ts = 7
        self.assertFalse(s.feed_controller(side, 0.0, False))  # baseline
        self.assertFalse(s.feed_controller(side, 0.008, False))
        side.imu_ts = 8
        self.assertTrue(s.feed_controller(side, 0.016, False))
        self.assertTrue(s.is_live(0.02))
        self.assertFalse(s.is_live(5.0))

    def test_glitch_values_are_held(self):
        s = motion.Sensor("left")
        self._feed(s, 3, (100, 100, 100), (0, int(G_LSB), 0), auto=False)
        good = s.gyro
        self._feed(s, 1, (255, -254, 100), (0, int(G_LSB), 0), start=1.0,
                   auto=False, ts0=3)
        self.assertEqual(s.gyro, good)

    def test_auto_calibration_at_rest(self):
        s = motion.Sensor("left")
        bias = (int(3 * DPS_LSB), int(-2 * DPS_LSB), int(1 * DPS_LSB))
        # 4096 LSB/g instead of the assumed scale: magnitude reads ~0.885 g
        self._feed(s, 500, bias, (0, 4096, 0), jitter=2)
        self.assertTrue(s.cal.calibrated and s.cal_dirty)
        self.assertAlmostEqual(s.cal.bias[0], 3.0, delta=0.1)
        self.assertAlmostEqual(s.cal.bias[1], -2.0, delta=0.1)
        self.assertAlmostEqual(max(abs(g) for g in s.gyro), 0.0, delta=0.2)
        self.assertAlmostEqual(s.accel[1], 1.0, delta=0.01)

    def test_no_auto_calibration_while_moving(self):
        s = motion.Sensor("left")
        now = 0.0
        for i in range(500):
            side = legion_hid.SideSample()
            side.connected = True
            side.imu_ts = i & 0xFF
            side.gyro = (int(20 * DPS_LSB * math.sin(i / 20.0)), 0, 0)
            side.accel = (0, int(G_LSB), 0)
            s.feed_controller(side, now, True)
            now += 0.008
        self.assertFalse(s.cal.calibrated)

    def test_steady_turn_is_not_mistaken_for_drift(self):
        s = motion.Sensor("left")
        self._feed(s, 500, (0, int(30 * DPS_LSB), 0), (0, int(G_LSB), 0), jitter=2)
        self.assertFalse(s.cal.calibrated)
        self.assertAlmostEqual(s.gyro[1], 30.0, delta=0.3)

    def test_no_calibration_on_garbage(self):
        s = motion.Sensor("left")
        self._feed(s, 500, (0, 0, 0), (0, 0, 0))  # no gravity: not plausible
        self.assertFalse(s.cal.calibrated)

    def test_manual_calibration(self):
        s = motion.Sensor("right")
        s.start_manual(0.0)
        self.assertTrue(s.calibrating)
        end = self._feed(s, 300, (int(5 * DPS_LSB), 0, 0), (0, int(G_LSB), 0),
                         auto=False)
        s.poll_manual(end)
        self.assertFalse(s.calibrating)
        self.assertEqual(s.manual_result, "ok")
        self.assertAlmostEqual(s.cal.bias[0], 5.0, delta=0.1)

        s.start_manual(10.0)
        s.poll_manual(13.0)
        self.assertEqual(s.manual_result, "nodata")

        s.start_manual(20.0)
        now = 20.0
        for i in range(300):
            side = legion_hid.SideSample()
            side.connected = True
            side.imu_ts = i & 0xFF
            side.gyro = (int(100 * DPS_LSB) * (i % 2), 0, 0)
            side.accel = (0, int(G_LSB), 0)
            s.feed_controller(side, now, False)
            now += 0.008
        s.poll_manual(now)
        self.assertEqual(s.manual_result, "moved")

    def test_calibration_roundtrip(self):
        c = motion.Calibration((1.5, -2.5, 0.25), 1.1, True)
        d = motion.Calibration.from_dict(json.loads(json.dumps(c.to_dict())))
        self.assertEqual((d.bias, d.accel_scale, d.calibrated), (c.bias, 1.1, True))
        bad = motion.Calibration.from_dict({"bias": [1, "x", 3]})
        self.assertEqual(bad.bias, (0.0, 0.0, 0.0))
        self.assertEqual(motion.Calibration.from_dict({"bias": [999, 0, 0]}).bias,
                         (0.0, 0.0, 0.0))


class Settings(unittest.TestCase):
    def test_defaults_are_valid(self):
        d = cfg.defaults()
        self.assertEqual(cfg.sanitize(d), d)
        self.assertEqual(d["slots"]["combined"]["source"], "auto")
        self.assertNotIn("source", d["slots"]["left"])
        self.assertIs(d["advanced"], False)
        self.assertIs(cfg.sanitize({"advanced": True})["advanced"], True)
        self.assertIs(cfg.sanitize({"advanced": "yes"})["advanced"], False)

    def test_sanitize_rejects_garbage(self):
        d = cfg.sanitize({
            "enabled": "yes", "port": 80, "lan": 1,
            "slots": {"left": {"sensitivity": 99, "deadzone": -4, "rotation": 45,
                               "invert_yaw": True},
                      "combined": {"source": "banana"},
                      "right": "nope"},
            "calibration": {"left": {"bias": [1, 2, 3], "accel_scale": 9}},
        })
        self.assertIs(d["enabled"], True)
        self.assertEqual(d["port"], cfg.DEFAULT_PORT)
        self.assertIs(d["lan"], False)
        left = d["slots"]["left"]
        self.assertEqual(left["sensitivity"], cfg.SENSITIVITY_MAX)
        self.assertEqual(left["deadzone"], 0.0)
        self.assertEqual(left["rotation"], 0)
        self.assertIs(left["invert_yaw"], True)
        self.assertEqual(d["slots"]["combined"]["source"], "auto")
        self.assertEqual(d["calibration"]["left"]["bias"], [1.0, 2.0, 3.0])
        self.assertEqual(d["calibration"]["left"]["accel_scale"], 1.0)
        self.assertEqual(cfg.sanitize(None), cfg.defaults())

    def test_merge_and_persistence(self):
        tmp = tempfile.mkdtemp()
        try:
            path = os.path.join(tmp, "sub", "settings.json")
            d = cfg.merge(cfg.defaults(), {"slots": {"right": {"rotation": 90}}})
            self.assertEqual(d["slots"]["right"]["rotation"], 90)
            self.assertEqual(d["slots"]["right"]["sensitivity"], 1.0)
            self.assertTrue(cfg.save(path, d))
            self.assertEqual(cfg.load(path), d)
            with open(path, "w") as f:
                f.write("{broken")
            self.assertEqual(cfg.load(path), cfg.defaults())
        finally:
            shutil.rmtree(tmp)


class DsuProtocol(unittest.TestCase):
    def test_battery_levels(self):
        self.assertEqual([battery_level(p) for p in (100, 75, 45, 20, 3)],
                         [5, 4, 3, 2, 1])

    def test_input_mapping(self):
        rep = legion_hid.parse_report(make_report(
            b18=0x80 | 0x40 | 0x20 | 0x08 | 0x02,   # legion L+R, LS, up, left
            b19=0x80 | 0x10 | 0x08 | 0x01,          # A, Y, LB, RT
            b20=0x02 | 0x01,                        # view, menu
            sticks=(0, 0, 255, 255), lt=10, rt=250, touch=(1000, 500)))
        b = rep.buttons
        pad = PadState()
        fill_inputs(pad, b, "combined")
        self.assertEqual(pad.buttons1, 0x80 | 0x10 | 0x08 | 0x02 | 0x01)
        self.assertEqual(pad.buttons2, 0x40 | 0x10 | 0x04 | 0x02)
        self.assertEqual(pad.home, 1)
        # HID sticks: 0 = up. DSU: 255 = up.
        self.assertEqual((pad.lx, pad.ly, pad.rx, pad.ry), (0, 255, 255, 0))
        self.assertEqual((pad.l2, pad.r2), (10, 250))
        self.assertEqual((pad.touch_active, pad.touch_x, pad.touch_y), (1, 1919, 471))

        fill_inputs(pad, b, "left")
        self.assertEqual(pad.buttons1, 0x80 | 0x10 | 0x02 | 0x01)
        self.assertEqual(pad.buttons2, 0x04)
        self.assertEqual((pad.rx, pad.ry, pad.r2, pad.touch_active), (0x80, 0x80, 0, 0))

        fill_inputs(pad, b, "right")
        self.assertEqual(pad.buttons1, 0x08)
        self.assertEqual(pad.buttons2, 0x40 | 0x10 | 0x02)
        self.assertEqual((pad.lx, pad.ly, pad.l2), (0x80, 0x80, 0))

    def test_ignores_invalid_datagrams(self):
        srv = DsuServer(1)
        self.assertEqual(srv.handle(b"", ("a", 1), 0.0), [])
        self.assertEqual(srv.handle(b"DSUC" + bytes(40), ("a", 1), 0.0), [])
        self.assertEqual(srv.handle(b"XXXX" + bytes(40), ("a", 1), 0.0), [])
        self.assertEqual(srv.clients, {})

    def test_subscriptions_expire(self):
        import struct
        import zlib
        srv = DsuServer(1)

        def req(body):
            payload = struct.pack("<I", 0x100002) + body
            head = struct.pack("<4sHHII", b"DSUC", 1001, len(payload), 0, 7)
            crc = zlib.crc32(head + payload) & 0xFFFFFFFF
            return struct.pack("<4sHHII", b"DSUC", 1001, len(payload), crc, 7) + payload

        srv.handle(req(bytes(8)), ("all", 1), 100.0)
        srv.handle(req(bytes([1, 2]) + bytes(6)), ("slot2", 1), 100.0)
        srv.handle(req(bytes([2, 0, 0x4C, 0x47, 0x4F, 0, 0, 2])), ("mac1", 1), 100.0)
        self.assertEqual(sorted(srv.subscribers(0, 101.0)), [("all", 1)])
        self.assertEqual(sorted(srv.subscribers(1, 101.0)), [("all", 1), ("mac1", 1)])
        self.assertEqual(sorted(srv.subscribers(2, 101.0)), [("all", 1), ("slot2", 1)])
        self.assertEqual(srv.subscribers(2, 106.0), [])
        self.assertEqual(srv.client_count(106.0), 0)


class FakeController(object):
    """Stands in for the hidraw node: a SOCK_SEQPACKET pair keeps report
    boundaries exactly like hidraw does."""

    def __init__(self):
        self.ours, self.theirs = socket.socketpair(
            socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.ours.setblocking(False)
        self.opens = 0
        self.ts = 0

    def opener(self, skip=0):
        if self.theirs is None:
            return None, "Legion Go controllers not found"
        self.opens += 1
        fd = os.dup(self.theirs.fileno())
        os.set_blocking(fd, False)
        return HidHandle(fd, "hidraw9", 0x6182, "xinput"), None

    def send(self, **kw):
        self.ts += 1
        kw.setdefault("left_ts", self.ts)
        kw.setdefault("right_ts", self.ts)
        self.ours.send(make_report(**kw))

    def commands(self, wait=0.0):
        out = []
        deadline = time.monotonic() + wait
        while True:
            try:
                out.append(self.ours.recv(64))
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return out
                time.sleep(0.02)

    def unplug(self):
        self.theirs.close()
        self.theirs = None
        self.ours.close()

    def close(self):
        for s in (self.ours, self.theirs):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass


class FakeTablet(object):
    def __init__(self, callback):
        self.callback = callback
        self.running = False
        self.error = None
        self.present = False

    def available(self):
        return self.present

    def start(self):
        self.running = True

    def stop(self):
        self.running = False


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.path = os.path.join(self.tmp, "settings.json")
        self.port = free_port()
        initial = cfg.defaults()
        initial["port"] = self.port
        initial["auto_calibrate"] = False
        cfg.save(self.path, initial)
        self.pad = FakeController()
        self.tablets = []

        def make_tablet(cb):
            t = FakeTablet(cb)
            self.tablets.append(t)
            return t

        log = logging.getLogger("test")
        log.addHandler(logging.NullHandler())
        log.propagate = False
        self.svc = GyroService(self.path, logger=log, hid_opener=self.pad.opener,
                               tablet_factory=make_tablet)
        self.svc.start()
        self.client = DsuClient(self.port)
        self._wait(lambda: self.svc.get_status()["server"]["listening"])

    def tearDown(self):
        self.client.close()
        self.svc.close()
        self.pad.close()
        shutil.rmtree(self.tmp)

    def _wait(self, cond, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if cond():
                return
            time.sleep(0.02)
        self.fail("condition not reached")

    def _stream(self, n=5, **kw):
        for _ in range(n):
            self.pad.send(**kw)
            time.sleep(0.008)

    def test_version_and_ports(self):
        self.client.request_version()
        p = self.client.recv()
        self.assertEqual((p["type"], p["version"], p["size"]), (0x100000, 1001, 24))

        self.client.request_ports()
        ports = sorted((self.client.recv() for _ in range(4)), key=lambda p: p["slot"])
        self.assertEqual([p["state"] for p in ports], [0, 0, 0, 0])
        self.assertTrue(all(p["size"] == 32 for p in ports))

        self._stream(left_state=2, right_state=3, left_batt=95, right_batt=20)
        self._wait(lambda: self.svc.get_status()["slots"]["right"]["connected"])
        self.client.request_ports()
        ports = sorted((self.client.recv() for _ in range(4)), key=lambda p: p["slot"])
        self.assertEqual([p["state"] for p in ports], [2, 2, 2, 0])
        self.assertEqual([p["model"] for p in ports], [2, 2, 2, 0])
        self.assertEqual(ports[1]["conn"], 1)      # left attached -> wired
        self.assertEqual(ports[2]["conn"], 2)      # right detached -> wireless
        self.assertEqual((ports[1]["battery"], ports[2]["battery"]), (5, 2))
        self.assertEqual(len({p["mac"] for p in ports[:3]}), 3)

        # A datagram with a bad checksum is ignored
        self.client.request_ports(corrupt=True)
        self.client.sock.settimeout(0.3)
        with self.assertRaises(socket.timeout):
            self.client.recv()

    def test_per_controller_motion(self):
        self.client.subscribe()
        self._stream(3)
        # Left: pitching up at 100 deg/s, lying flat.
        # Right: yawing right at 50 deg/s, standing on its bottom edge
        #        (top edge up -> gamepad frame accel -Z -> DSU accZ +1).
        kw = dict(
            left_raw_gyro=(-int(100 * DPS_LSB), 0, 0),
            left_raw_accel=(0, 0, -int(G_LSB)),
            right_raw_gyro=(0, 0, int(50 * DPS_LSB)),
            right_raw_accel=(-int(G_LSB), 0, 0),
        )
        self._stream(10, **kw)
        self.pad.send(**kw)

        left = self.client.recv_pad(1, lambda p: abs(p["gyro"][0]) > 1)
        self.assertEqual(left["active"], 1)
        self.assertAlmostEqual(left["gyro"][0], 100.0, delta=0.2)
        self.assertAlmostEqual(left["gyro"][1], 0.0, delta=0.01)
        self.assertAlmostEqual(left["accel"][1], -1.0, delta=0.01)

        self.pad.send(**kw)
        right = self.client.recv_pad(2, lambda p: abs(p["gyro"][1]) > 1)
        self.assertAlmostEqual(right["gyro"][1], 50.0, delta=0.2)
        self.assertAlmostEqual(right["gyro"][0], 0.0, delta=0.01)
        self.assertAlmostEqual(right["accel"][2], 1.0, delta=0.01)

        # Slot 0 (auto) follows the right controller
        self.pad.send(**kw)
        both = self.client.recv_pad(0, lambda p: abs(p["gyro"][1]) > 1)
        self.assertAlmostEqual(both["gyro"][1], 50.0, delta=0.2)
        self._wait(lambda: self.svc.get_status()["slots"]["combined"]["source"] == "right")

        # Timestamps advance 8 ms per sample, packet counters by one
        self.pad.send(**kw)
        a = self.client.recv_pad(2)
        self.pad.send(**kw)
        b = self.client.recv_pad(2, lambda p: p["counter"] > a["counter"])
        self.assertEqual(b["counter"], a["counter"] + 1)
        self.assertEqual(b["timestamp"] - a["timestamp"], 8000)

    def test_slot_subscription_only_gets_that_slot(self):
        self.client.subscribe(slot=1)
        self._stream(20)
        self.client.sock.settimeout(0.3)
        seen = set()
        try:
            while True:
                seen.add(self.client.recv()["slot"])
        except socket.timeout:
            pass
        self.assertEqual(seen, {1})

    def test_imu_is_switched_on_and_off(self):
        self.pad.send(left_ts=5, right_ts=5)
        self.pad.ts = 5
        # No new IMU samples arrive: the service must enable both IMUs
        deadline = time.monotonic() + 3.0
        cmds = []
        while time.monotonic() < deadline and len(cmds) < 4:
            self.pad.ours.send(make_report(left_ts=5, right_ts=5))
            cmds += self.pad.commands(0.1)
        self.assertIn(bytes.fromhex("05066a02030101"), cmds)
        self.assertIn(bytes.fromhex("05066a07030201"), cmds)
        self.assertIn(bytes.fromhex("05066a02040101"), cmds)
        self.assertIn(bytes.fromhex("05066a07040201"), cmds)

        # Once data flows it must not keep re-sending the commands
        self._stream(40)
        self.pad.commands(0.1)
        t_end = time.monotonic() + 0.8
        while time.monotonic() < t_end:
            self.pad.send()
            time.sleep(0.008)
        self.assertEqual(self.pad.commands(), [])

        # Disabling the left slot while slot 0 uses the right controller
        # switches the left IMU off again
        self.svc.update_settings({"slots": {"left": {"enabled": False}}})
        t_end = time.monotonic() + 1.0
        cmds = []
        while time.monotonic() < t_end:
            self.pad.send()
            cmds += self.pad.commands()
            time.sleep(0.008)
        self.assertEqual(cmds, [bytes.fromhex("05066a07030101")])

        # Stopping the service switches the remaining IMU off
        self.svc.stop()
        self.assertEqual(self.pad.commands(0.2), [bytes.fromhex("05066a07040101")])

    def test_buttons_and_disconnect(self):
        self.client.subscribe()
        self._stream(5)
        self.pad.send(b19=0x80, sticks=(0x80, 0x00, 0x80, 0x80))  # A + stick up
        right = self.client.recv_pad(2, lambda p: p["buttons2"])
        self.assertEqual(right["buttons2"], 0x40)
        self.assertEqual(right["analog"][5], 255)
        self.pad.send(b19=0x80, sticks=(0x80, 0x00, 0x80, 0x80))
        left = self.client.recv_pad(1, lambda p: p["ly"] != 0x80)
        self.assertEqual((left["ly"], left["buttons2"]), (255, 0))

        # Right controller switches off: its slot reports disconnected and
        # slot 0 falls back to the left controller
        self._stream(5, right_state=1)
        gone = self.client.recv_pad(2, lambda p: p["active"] == 0)
        self.assertEqual(gone["state"], 0)
        self._wait(lambda: self.svc.get_status()["slots"]["combined"]["source"] == "left")

        # Receiver disappears (mode switch / suspend): everything disconnects,
        # the service keeps running and picks the device up again
        opens = self.pad.opens
        self.pad.unplug()
        self._wait(lambda: self.svc.get_status()["device"]["error"])
        st = self.svc.get_status()
        self.assertFalse(st["device"]["found"])
        self.assertFalse(any(s["connected"] for s in st["slots"].values()))
        self.assertEqual(st["device"]["error"], "Legion Go controllers not found")
        self.assertTrue(st["running"])
        self.assertEqual(self.pad.opens, opens)

    def test_stale_motion_is_zeroed(self):
        self.client.subscribe(slot=2)
        self._stream(3)
        self._stream(10, right_raw_gyro=(0, 0, int(90 * DPS_LSB)))
        self.client.recv_pad(2, lambda p: abs(p["gyro"][1]) > 80)
        # Stream stops while rotating: reports continue without new samples
        frozen = self.pad.ts
        t_end = time.monotonic() + 1.5
        while time.monotonic() < t_end:
            self.pad.ours.send(make_report(
                left_ts=frozen, right_ts=frozen,
                right_raw_gyro=(0, 0, int(90 * DPS_LSB))))
            time.sleep(0.02)
        self.client.subscribe(slot=2)
        p = self.client.recv_pad(2, lambda p: p["gyro"] == (0.0, 0.0, 0.0))
        self.assertEqual(p["active"], 1)

    def test_settings_are_applied_live_and_persisted(self):
        self.client.subscribe(slot=1)
        out = self.svc.update_settings({"slots": {"left": {
            "sensitivity": 2.0, "invert_pitch": True}}})
        self.assertEqual(out["slots"]["left"]["sensitivity"], 2.0)
        self.assertNotIn("calibration", out)
        kw = dict(left_raw_gyro=(-int(100 * DPS_LSB), 0, 0))
        self._stream(10, **kw)
        self.pad.send(**kw)
        p = self.client.recv_pad(1, lambda p: abs(p["gyro"][0]) > 150)
        self.assertAlmostEqual(p["gyro"][0], -200.0, delta=0.5)
        self._wait(lambda: cfg.load(self.path)["slots"]["left"]["sensitivity"] == 2.0)
        self.assertEqual(cfg.load(self.path)["port"], self.port)

        # Master switch: frees the port and stops talking to the controllers
        self.svc.update_settings({"enabled": False})
        self._wait(lambda: not self.svc.get_status()["server"]["listening"])
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.bind(("127.0.0.1", self.port))
        probe.close()
        self.svc.update_settings({"enabled": True})
        self._wait(lambda: self.svc.get_status()["server"]["listening"])

    def test_port_change_and_conflict(self):
        other = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        other.bind(("127.0.0.1", 0))
        busy = other.getsockname()[1]
        try:
            self.svc.update_settings({"port": busy})
            self._wait(lambda: self.svc.get_status()["server"]["error"])
            st = self.svc.get_status()["server"]
            self.assertFalse(st["listening"])
            self.assertIn("already used", st["error"])
        finally:
            other.close()
        self._wait(lambda: self.svc.get_status()["server"]["listening"], 6.0)
        self.assertEqual(self.svc.get_status()["server"]["port"], busy)

    def test_manual_calibration_flow(self):
        self.assertIsNone(self.svc.request_calibration("left"))  # nothing live
        self.assertIsNone(self.svc.request_calibration("bogus"))
        kw = dict(left_raw_gyro=(-int(4 * DPS_LSB), 0, 0),
                  left_raw_accel=(0, 0, -int(G_LSB)),
                  right_raw_accel=(0, 0, -int(G_LSB)))
        self._stream(10, **kw)
        self.assertEqual(self.svc.request_calibration("left"), "left")
        _, serial, _ = self.svc.calibration_state("left")
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            self.pad.send(**kw)
            time.sleep(0.008)
            if self.svc.calibration_state("left")[1] != serial:
                break
        self.assertEqual(self.svc.calibration_state("left")[2], "ok")
        self._wait(lambda: cfg.load(self.path)["calibration"]["left"]["calibrated"])
        self.assertAlmostEqual(
            cfg.load(self.path)["calibration"]["left"]["bias"][0], 4.0, delta=0.1)
        # Slot 0 resolves to the sensor behind it
        self.assertEqual(self.svc.request_calibration("combined"), "right")

        # Calibrating everything at once covers both streaming controllers
        self.assertEqual(self.svc.request_calibration_all(), ["left", "right"])
        serial = self.svc.calibration_state("right")[1]
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            self.pad.send(**kw)
            time.sleep(0.008)
            if self.svc.calibration_state("right")[1] != serial:
                break
        self.assertEqual(self.svc.calibration_state("right")[2], "ok")
        self.assertEqual(self.svc.calibration_state("left")[2], "ok")

        # Reset wipes tuning and calibration but keeps the panel layout
        self.svc.update_settings({"advanced": True,
                                  "slots": {"right": {"rotation": 180}}})
        out = self.svc.reset_settings()
        self.assertEqual(out["slots"]["right"]["rotation"], 0)
        self.assertIs(out["advanced"], True)
        expected = self.svc.get_defaults()
        expected["advanced"] = True
        self.assertEqual(out, expected)
        self._wait(lambda: not cfg.load(self.path)["calibration"]["left"]["calibrated"])

    def test_tablet_source(self):
        t = self.tablets[0]
        t.present = True
        self.svc.update_settings({"slots": {"combined": {"source": "tablet"}}})
        self._wait(lambda: t.running, 6.0)
        self.client.subscribe(slot=0)
        self._stream(3)
        deadline = time.monotonic() + 3.0
        got = None
        self.client.sock.settimeout(0.2)
        while time.monotonic() < deadline and got is None:
            t.callback((0.0, 1.0, 0.0), (0.0, -30.0, 0.0), time.monotonic())
            try:
                p = self.client.recv()
            except socket.timeout:
                continue
            if p["type"] == 0x100002 and abs(p["gyro"][1]) > 1:
                got = p
        self.assertIsNotNone(got)
        self.assertAlmostEqual(got["gyro"][1], 30.0, delta=0.01)
        self.assertAlmostEqual(got["accel"][1], -1.0, delta=0.01)
        self.assertEqual(self.svc.get_status()["slots"]["combined"]["source"], "tablet")
        # Going back to a controller stops the tablet poller
        self.svc.update_settings({"slots": {"combined": {"source": "auto"}}})
        self._stream(3)
        self._wait(lambda: not t.running)


class TabletSensor(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def _dev(self, idx, name, prefix, raw, scale):
        d = os.path.join(self.tmp, "iio:device%d" % idx)
        os.makedirs(d)
        with open(os.path.join(d, "name"), "w") as f:
            f.write(name + "\n")
        for axis, v in zip("xyz", raw):
            with open(os.path.join(d, "in_%s_%s_raw" % (prefix, axis)), "w") as f:
                f.write("%d\n" % v)
        with open(os.path.join(d, "in_%s_scale" % prefix), "w") as f:
            f.write("%r\n" % scale)

    def test_frame_mapping(self):
        a, g = tablet.to_gamepad_frame((1, 2, 3), (4, 5, 6))
        self.assertEqual(a, (-2, 3, 1))
        self.assertEqual(g, (5, 6, -4))

    def test_scale_detection(self):
        self.assertEqual(tablet.pick_accel_correction(0.98), 10.0)
        self.assertEqual(tablet.pick_accel_correction(9.7), 1.0)
        self.assertEqual(tablet.pick_accel_correction(0.0), 10.0)

    def test_missing(self):
        self.assertIsNone(tablet.find_devices(self.tmp))
        self._dev(0, "gyro_3d", "anglvel", (0, 0, 0), 1.0)
        self.assertIsNone(tablet.find_devices(self.tmp))
        self.assertFalse(tablet.TabletImu(lambda *a: None, base=self.tmp).available())

    def test_polling(self):
        # Tablet flat, screen up: gravity on IIO +Z, under-scaled by 10 like
        # the amd_sfh driver reports it. Rotating 0.02 rad/s (x100) on IIO Y.
        self._dev(1, "accel_3d", "accel", (0, 0, 981), 0.001)
        self._dev(3, "gyro_3d", "anglvel", (0, 200, 0), 0.0001)
        self._dev(2, "als", "illuminance", (1, 1, 1), 1.0)
        got = []
        imu = tablet.TabletImu(lambda a, g, now: got.append((a, g)), base=self.tmp)
        self.assertTrue(imu.available())
        imu.start()
        deadline = time.monotonic() + 5.0
        while len(got) < 5 and time.monotonic() < deadline:
            time.sleep(0.02)
        imu.stop()
        self.assertFalse(imu.running)
        self.assertGreaterEqual(len(got), 5)
        self.assertEqual((imu.accel_correction, imu.gyro_correction), (10.0, 100.0))
        a, g = got[-1]
        self.assertAlmostEqual(a[1], 1.0, delta=0.01)          # flat -> +1 g on Y
        self.assertAlmostEqual(g[0], math.degrees(2.0), delta=0.01)
        self.assertEqual((a[0], a[2], g[1], g[2]), (0.0, 0.0, 0.0, 0.0))


if __name__ == "__main__":
    unittest.main()
