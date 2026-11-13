"""Lenovo Legion Go controller HID protocol.

The Legion Go controllers hang off a USB receiver inside the tablet
(vendor 17EF). One of its HID interfaces (vendor usage page 0xFFA0, USB
interface 2) continuously streams a 60 byte "state" report that contains
every button, both sticks and - once it has been switched on - the full
6-axis IMU data of *each* controller separately. The same interface
accepts short configuration commands.

Layout and commands follow what the open source Handheld Daemon (hhd) and
InputPlumber projects have documented for this device.
"""

import errno
import os
import stat

LEN_VID = 0x17EF
LEN_PIDS = {
    # Legion Go (original firmware)
    0x6182: "xinput",
    0x6183: "dinput",
    0x6184: "dual_dinput",
    0x6185: "fps",
    # Legion Go with 2025 firmware / Legion Go 2
    0x61EB: "xinput",
    0x61EC: "dinput",
    0x61ED: "dual_dinput",
    0x61EE: "fps",
}

MODE_LABELS = {
    "xinput": "XInput",
    "dinput": "DInput",
    "dual_dinput": "Dual DInput",
    "fps": "FPS",
}

REPORT_ID = 0x04
REPORT_CMD_STATE = 0x74
REPORT_LEN = 60
READ_SIZE = 64

# The controller IMU timestamp is a free running 8 bit counter, one count
# per 8 ms sample.
IMU_TICK_US = 8000

# 16 bit gyro, +-2000 deg/s full scale.
GYRO_DPS_PER_LSB = 2000.0 / 32768.0
# Accelerometer scale as measured by hhd (0.00212 m/s^2 per LSB). It is
# refined per controller by the calibration routine (see motion.py).
ACCEL_G_PER_LSB = 0.00212 / 9.80665

SIDES = ("left", "right")
_SIDE_ID = {"left": 0x03, "right": 0x04}


def cmd_enable_gyro(side):
    """Commands that switch a controller's IMU on and make it stream."""
    rc = _SIDE_ID[side]
    return [
        # Enable the gyro if it is disabled
        bytes([0x05, 0x06, 0x6A, 0x02, rc, 0x01, 0x01]),
        # Enable the high quality (full 6-axis) report
        bytes([0x05, 0x06, 0x6A, 0x07, rc, 0x02, 0x01]),
    ]


def cmd_disable_gyro(side):
    """Stop the high quality IMU report again (saves controller battery)."""
    rc = _SIDE_ID[side]
    return [bytes([0x05, 0x06, 0x6A, 0x07, rc, 0x01, 0x01])]


def _be16(buf, ofs):
    v = (buf[ofs] << 8) | buf[ofs + 1]
    return v - 0x10000 if v & 0x8000 else v


class SideSample(object):
    """Decoded per-controller part of a state report."""

    __slots__ = ("connected", "attached", "battery", "imu_ts", "accel", "gyro")

    def __init__(self):
        self.connected = False
        self.attached = False
        self.battery = 0
        self.imu_ts = 0
        # Raw sensor counts, already rotated into the "gamepad frame":
        #   +X = to the right, +Y = out of the button face (up when the
        #   device lies flat), +Z = towards the player (bottom edge).
        # This is the frame DualShock 4 / DualSense controllers use.
        self.accel = (0, 0, 0)
        self.gyro = (0, 0, 0)


class Buttons(object):
    """Decoded buttons / sticks of a state report."""

    __slots__ = (
        "legion_l", "legion_r", "ls", "rs",
        "up", "down", "left", "right",
        "a", "b", "x", "y", "lb", "lt_d", "rb", "rt_d",
        "y1", "y2", "y3", "m2", "m3", "view", "menu", "m1",
        "lx", "ly", "rx", "ry", "lt", "rt",
        "touch_x", "touch_y",
    )

    def __init__(self):
        for name in self.__slots__:
            setattr(self, name, 0)
        self.lx = self.ly = self.rx = self.ry = 0x80


class Report(object):
    __slots__ = ("left", "right", "buttons")

    def __init__(self, left, right, buttons):
        self.left = left
        self.right = right
        self.buttons = buttons


def is_state_report(buf):
    return (
        len(buf) >= REPORT_LEN
        and buf[0] == REPORT_ID
        and buf[2] == REPORT_CMD_STATE
    )


def parse_report(buf):
    """Parse a raw hidraw read. Returns a Report or None."""
    if not is_state_report(buf):
        return None

    left = SideSample()
    right = SideSample()

    left.battery = min(buf[5], 100)
    right.battery = min(buf[7], 100)

    # Byte 12/13: 1 = not connected, 2 = attached to the tablet,
    # 3 = detached (wireless). Byte 10/11 bit 0: connected flag.
    ls, rs = buf[12], buf[13]
    left.attached = ls == 2
    right.attached = rs == 2
    left.connected = ls in (2, 3) or bool(buf[10] & 0x01)
    right.connected = rs in (2, 3) or bool(buf[11] & 0x01)

    # Left controller IMU (big endian, sensor order X, Z, Y, all inverted)
    left.imu_ts = buf[34]
    left.accel = (-_be16(buf, 35), -_be16(buf, 39), -_be16(buf, 37))
    left.gyro = (-_be16(buf, 41), -_be16(buf, 45), -_be16(buf, 43))

    # Right controller IMU (sensor is mounted rotated: order Z, X, Y)
    right.imu_ts = buf[47]
    right.accel = (-_be16(buf, 50), -_be16(buf, 52), _be16(buf, 48))
    right.gyro = (-_be16(buf, 56), -_be16(buf, 58), _be16(buf, 54))

    b = Buttons()
    b18, b19, b20 = buf[18], buf[19], buf[20]
    b.legion_l = b18 >> 7 & 1
    b.legion_r = b18 >> 6 & 1
    b.ls = b18 >> 5 & 1
    b.rs = b18 >> 4 & 1
    b.up = b18 >> 3 & 1
    b.down = b18 >> 2 & 1
    b.left = b18 >> 1 & 1
    b.right = b18 & 1
    b.a = b19 >> 7 & 1
    b.b = b19 >> 6 & 1
    b.x = b19 >> 5 & 1
    b.y = b19 >> 4 & 1
    b.lb = b19 >> 3 & 1
    b.lt_d = b19 >> 2 & 1
    b.rb = b19 >> 1 & 1
    b.rt_d = b19 & 1
    b.y1 = b20 >> 7 & 1
    b.y2 = b20 >> 6 & 1
    b.y3 = b20 >> 5 & 1
    b.m1 = b20 >> 4 & 1
    b.m2 = b20 >> 3 & 1
    b.m3 = b20 >> 2 & 1
    b.view = b20 >> 1 & 1
    b.menu = b20 & 1
    b.lx, b.ly, b.rx, b.ry = buf[14], buf[15], buf[16], buf[17]
    b.lt, b.rt = buf[22], buf[23]
    b.touch_x = (buf[26] << 8) | buf[27]
    b.touch_y = (buf[28] << 8) | buf[29]

    return Report(left, right, b)


# --------------------------------------------------------------------------
# Device discovery
# --------------------------------------------------------------------------

SYS_HIDRAW = "/sys/class/hidraw"
# Usage page 0xFFA0 in a HID report descriptor: 06 A0 FF
_VENDOR_USAGE_PAGE = b"\x06\xa0\xff"
_PRIVATE_NODE_DIR = "/dev/.legion-go-gyro"


class HidCandidate(object):
    __slots__ = ("name", "pid", "mode", "interface", "vendor_page", "devnum")

    def __init__(self, name, pid, interface, vendor_page, devnum):
        self.name = name
        self.pid = pid
        self.mode = LEN_PIDS.get(pid, "unknown")
        self.interface = interface
        self.vendor_page = vendor_page
        self.devnum = devnum

    def rank(self):
        # Prefer the vendor usage page, then USB interface 2.
        return (0 if self.vendor_page else 1, 0 if self.interface == 2 else 1,
                self.name)


def _read_file(path, binary=False):
    try:
        with open(path, "rb") as f:
            data = f.read()
        return data if binary else data.decode("utf-8", "replace")
    except (OSError, IOError):
        return None


def _parse_hid_id(uevent):
    """HID_ID=0003:000017EF:00006182 -> (0x17EF, 0x6182)"""
    for line in uevent.splitlines():
        if line.startswith("HID_ID="):
            parts = line.split("=", 1)[1].strip().split(":")
            if len(parts) == 3:
                try:
                    return int(parts[1], 16), int(parts[2], 16)
                except ValueError:
                    return None
    return None


def _interface_number(sys_device_path):
    """USB interface number from .../1-3:1.2/0003:17EF:6182.000A"""
    try:
        real = os.path.realpath(sys_device_path)
    except OSError:
        return None
    parent = os.path.basename(os.path.dirname(real))
    if ":" in parent and "." in parent:
        try:
            return int(parent.rsplit(".", 1)[1])
        except ValueError:
            return None
    return None


def find_candidates(sys_hidraw=SYS_HIDRAW):
    """All hidraw nodes that may be the Legion Go state interface, best first."""
    out = []
    try:
        names = sorted(os.listdir(sys_hidraw))
    except OSError:
        return out

    for name in names:
        if not name.startswith("hidraw"):
            continue
        base = os.path.join(sys_hidraw, name)
        uevent = _read_file(os.path.join(base, "device", "uevent"))
        if not uevent:
            continue
        ids = _parse_hid_id(uevent)
        if not ids or ids[0] != LEN_VID or ids[1] not in LEN_PIDS:
            continue
        desc = _read_file(os.path.join(base, "device", "report_descriptor"), True)
        vendor_page = bool(desc) and _VENDOR_USAGE_PAGE in desc
        iface = _interface_number(os.path.join(base, "device"))
        if not vendor_page and iface != 2:
            continue
        devnum = None
        dev = _read_file(os.path.join(base, "dev"))
        if dev and ":" in dev:
            try:
                major, minor = dev.strip().split(":")
                devnum = (int(major), int(minor))
            except ValueError:
                devnum = None
        out.append(HidCandidate(name, ids[1], iface, vendor_page, devnum))

    out.sort(key=lambda c: c.rank())
    return out


def _private_node(cand):
    """Create our own device node for a hidraw device.

    Input managers (e.g. InputPlumber on SteamOS) may hide or move the
    original /dev/hidrawN node. The plugin runs as root, so it can always
    create its own node from the kernel's major:minor numbers.
    """
    if not cand.devnum:
        return None
    try:
        if not os.path.isdir(_PRIVATE_NODE_DIR):
            os.mkdir(_PRIVATE_NODE_DIR, 0o700)
        path = os.path.join(_PRIVATE_NODE_DIR, cand.name)
        try:
            os.unlink(path)
        except OSError:
            pass
        os.mknod(path, 0o600 | stat.S_IFCHR, os.makedev(*cand.devnum))
        return path
    except OSError:
        return None


def open_candidate(cand):
    """Open a hidraw candidate read/write, non-blocking. Returns fd or raises."""
    flags = os.O_RDWR | os.O_NONBLOCK | getattr(os, "O_CLOEXEC", 0)
    last = None
    paths = [
        "/dev/" + cand.name,
        "/dev/inputplumber/sources/" + cand.name,
    ]
    for path in paths:
        try:
            return os.open(path, flags)
        except OSError as e:
            last = e
    path = _private_node(cand)
    if path:
        try:
            return os.open(path, flags)
        except OSError as e:
            last = e
    raise last if last else OSError(errno.ENOENT, "no device node for " + cand.name)


def cleanup_private_nodes():
    try:
        for name in os.listdir(_PRIVATE_NODE_DIR):
            try:
                os.unlink(os.path.join(_PRIVATE_NODE_DIR, name))
            except OSError:
                pass
        os.rmdir(_PRIVATE_NODE_DIR)
    except OSError:
        pass
