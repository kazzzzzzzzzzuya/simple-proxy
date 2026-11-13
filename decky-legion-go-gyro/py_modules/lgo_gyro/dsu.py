"""DSU ("cemuhook" motion provider) UDP server.

DSU is the de-facto standard emulators use to receive motion data:
Dolphin, Cemu, Ryujinx and its forks, yuzu forks (Eden, Citron, Sudachi,
...), RPCS3, PCSX2 and others can all act as a DSU client.

Protocol reference: https://v1993.github.io/cemuhook-protocol/
"""

import struct

try:
    from zlib import crc32
except ImportError:  # pragma: no cover - zlib is practically always there
    from binascii import crc32

PROTOCOL_VERSION = 1001
MAGIC_SERVER = b"DSUS"
MAGIC_CLIENT = b"DSUC"

MSG_VERSION = 0x100000
MSG_PORTS = 0x100001
MSG_DATA = 0x100002

SLOT_COUNT = 4
STATE_DISCONNECTED = 0
STATE_CONNECTED = 2
MODEL_FULL_GYRO = 2
CONN_USB = 1
CONN_BLUETOOTH = 2

BATTERY_NONE = 0x00
BATTERY_DYING = 0x01
BATTERY_LOW = 0x02
BATTERY_MEDIUM = 0x03
BATTERY_HIGH = 0x04
BATTERY_FULL = 0x05

# A client has to repeat its data request; forget it after this long.
CLIENT_TIMEOUT_S = 5.0

_HEADER = struct.Struct("<4sHHII")  # magic, version, length, crc, id
_HEADER_SIZE = _HEADER.size  # 16
_PAD_BODY = struct.Struct(
    "<"
    "B"      # connected
    "I"      # packet number
    "BBBB"   # buttons1, buttons2, home, touch button
    "BBBB"   # LX, LY, RX, RY
    "BBBB"   # analog dpad left, down, right, up
    "BBBB"   # analog Y(square), B(cross), A(circle), X(triangle)
    "BBBB"   # analog R1, L1, R2, L2
    "BBHH"   # touch 1: active, id, x, y
    "BBHH"   # touch 2
    "Q"      # motion timestamp (microseconds)
    "ffffff"  # accel X, Y, Z (g), gyro pitch, yaw, roll (deg/s)
)


def battery_level(percent):
    if percent >= 90:
        return BATTERY_FULL
    if percent >= 60:
        return BATTERY_HIGH
    if percent >= 30:
        return BATTERY_MEDIUM
    if percent >= 12:
        return BATTERY_LOW
    return BATTERY_DYING


class PadState(object):
    """Everything a DSU pad data packet carries for one slot."""

    __slots__ = (
        "connected", "wireless", "battery",
        "buttons1", "buttons2", "home", "touch_button",
        "lx", "ly", "rx", "ry", "l2", "r2",
        "touch_active", "touch_x", "touch_y",
        "timestamp_us", "motion",
    )

    def __init__(self):
        self.connected = False
        self.wireless = False
        self.battery = BATTERY_NONE
        self.clear_inputs()
        self.timestamp_us = 0
        self.motion = (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    def clear_inputs(self):
        self.buttons1 = 0
        self.buttons2 = 0
        self.home = 0
        self.touch_button = 0
        self.lx = self.ly = self.rx = self.ry = 0x80
        self.l2 = self.r2 = 0
        self.touch_active = 0
        self.touch_x = self.touch_y = 0

    def input_key(self):
        return (
            self.buttons1, self.buttons2, self.home, self.lx, self.ly,
            self.rx, self.ry, self.l2, self.r2, self.touch_active,
            self.touch_x, self.touch_y,
        )


def fill_inputs(pad, b, role):
    """Map Legion Go buttons onto the DualShock style DSU layout.

    role "combined" carries the whole gamepad, "left" / "right" only the
    inputs that physically sit on that controller.
    """
    left = role in ("combined", "left")
    right = role in ("combined", "right")
    b1 = 0
    b2 = 0
    home = 0
    pad.lx = pad.ly = pad.rx = pad.ry = 0x80
    pad.l2 = pad.r2 = 0
    pad.touch_active = 0
    pad.touch_x = pad.touch_y = 0

    if left:
        b1 |= (b.left << 7) | (b.down << 6) | (b.right << 5) | (b.up << 4)
        b1 |= (b.ls << 1) | b.view            # L3, Share
        b2 |= (b.lb << 2) | b.lt_d            # L1, L2
        pad.lx = b.lx
        pad.ly = 255 - b.ly                   # DSU: up is positive
        pad.l2 = b.lt
        home |= b.legion_l
    if right:
        b1 |= (b.menu << 3) | (b.rs << 2)     # Options, R3
        b2 |= (b.x << 7) | (b.a << 6) | (b.b << 5) | (b.y << 4)
        b2 |= (b.rb << 3) | (b.rt_d << 1)     # R1, R2
        pad.rx = b.rx
        pad.ry = 255 - b.ry
        pad.r2 = b.rt
        home |= b.legion_r
        if b.touch_x or b.touch_y:
            # Controller touchpad is 0..1000, DS4 touchpad is 1920x943
            pad.touch_active = 1
            pad.touch_x = min(1919, b.touch_x * 1920 // 1000)
            pad.touch_y = min(942, b.touch_y * 943 // 1000)

    pad.buttons1 = b1
    pad.buttons2 = b2
    pad.home = home


def _finish(server_id, payload):
    """Prepend the header and fill in the CRC."""
    head = _HEADER.pack(MAGIC_SERVER, PROTOCOL_VERSION, len(payload), 0, server_id)
    crc = crc32(head + payload) & 0xFFFFFFFF
    return _HEADER.pack(
        MAGIC_SERVER, PROTOCOL_VERSION, len(payload), crc, server_id
    ) + payload


def _slot_mac(slot):
    return bytes([0x4C, 0x47, 0x4F, 0x00, 0x00, 0x01 + slot])


def _slot_header(slot, pad):
    if pad is not None and pad.connected:
        return struct.pack(
            "<BBBB6sB",
            slot,
            STATE_CONNECTED,
            MODEL_FULL_GYRO,
            CONN_BLUETOOTH if pad.wireless else CONN_USB,
            _slot_mac(slot),
            pad.battery,
        )
    return struct.pack("<BBBB6sB", slot, STATE_DISCONNECTED, 0, 0, b"\0" * 6, 0)


class _Client(object):
    __slots__ = ("all", "slots", "macs")

    def __init__(self):
        self.all = 0.0
        self.slots = {}
        self.macs = {}


class DsuServer(object):
    """Protocol logic. Socket handling is left to the caller so that the
    whole plugin can run in a single select() loop."""

    def __init__(self, server_id):
        self.server_id = server_id & 0xFFFFFFFF
        self.pads = [PadState() for _ in range(SLOT_COUNT)]
        self.clients = {}
        self._counters = [0] * SLOT_COUNT
        self._last_prune = 0.0

    # ---- incoming --------------------------------------------------------

    def handle(self, data, addr, now):
        """Process one datagram. Returns a list of reply datagrams."""
        if len(data) < _HEADER_SIZE + 4:
            return []
        magic, version, length, crc, _client_id = _HEADER.unpack_from(data)
        if magic != MAGIC_CLIENT or version > PROTOCOL_VERSION:
            return []
        if length < 4 or _HEADER_SIZE + length > len(data):
            return []
        data = data[: _HEADER_SIZE + length]
        check = crc32(data[:8] + b"\0\0\0\0" + data[12:]) & 0xFFFFFFFF
        if check != crc:
            return []
        (msg,) = struct.unpack_from("<I", data, _HEADER_SIZE)
        body = data[_HEADER_SIZE + 4:]

        if msg == MSG_VERSION:
            payload = struct.pack("<IHH", MSG_VERSION, PROTOCOL_VERSION, 0)
            return [_finish(self.server_id, payload)]

        if msg == MSG_PORTS:
            if len(body) < 4:
                return []
            (count,) = struct.unpack_from("<i", body)
            if count < 0 or count > SLOT_COUNT or len(body) < 4 + count:
                return []
            out = []
            for slot in body[4: 4 + count]:
                if slot >= SLOT_COUNT:
                    continue
                payload = (
                    struct.pack("<I", MSG_PORTS)
                    + _slot_header(slot, self.pads[slot])
                    + b"\0"
                )
                out.append(_finish(self.server_id, payload))
            return out

        if msg == MSG_DATA:
            if len(body) < 8:
                return []
            flags, slot = body[0], body[1]
            mac = bytes(body[2:8])
            client = self.clients.get(addr)
            if client is None:
                client = self.clients[addr] = _Client()
            if flags == 0:
                client.all = now
            else:
                if flags & 0x01 and slot < SLOT_COUNT:
                    client.slots[slot] = now
                if flags & 0x02:
                    client.macs[mac] = now
            return []

        return []

    # ---- outgoing --------------------------------------------------------

    def prune(self, now):
        if now - self._last_prune < 1.0:
            return
        self._last_prune = now
        limit = now - CLIENT_TIMEOUT_S
        dead = []
        for addr, c in self.clients.items():
            if c.all >= limit:
                continue
            if any(t >= limit for t in c.slots.values()):
                continue
            if any(t >= limit for t in c.macs.values()):
                continue
            dead.append(addr)
        for addr in dead:
            del self.clients[addr]

    def subscribers(self, slot, now):
        limit = now - CLIENT_TIMEOUT_S
        mac = _slot_mac(slot)
        out = []
        for addr, c in self.clients.items():
            if (
                c.all >= limit
                or c.slots.get(slot, 0.0) >= limit
                or c.macs.get(mac, 0.0) >= limit
            ):
                out.append(addr)
        return out

    def client_count(self, now):
        self.prune(now)
        return len(self.clients)

    def pad_packet(self, slot):
        """Build the pad data datagram for a slot (increments its counter)."""
        pad = self.pads[slot]
        self._counters[slot] = (self._counters[slot] + 1) & 0xFFFFFFFF
        m = pad.motion
        body = _PAD_BODY.pack(
            1 if pad.connected else 0,
            self._counters[slot],
            pad.buttons1, pad.buttons2, pad.home, pad.touch_button,
            pad.lx, pad.ly, pad.rx, pad.ry,
            255 if pad.buttons1 & 0x80 else 0,
            255 if pad.buttons1 & 0x40 else 0,
            255 if pad.buttons1 & 0x20 else 0,
            255 if pad.buttons1 & 0x10 else 0,
            255 if pad.buttons2 & 0x80 else 0,
            255 if pad.buttons2 & 0x40 else 0,
            255 if pad.buttons2 & 0x20 else 0,
            255 if pad.buttons2 & 0x10 else 0,
            255 if pad.buttons2 & 0x08 else 0,
            255 if pad.buttons2 & 0x04 else 0,
            pad.r2, pad.l2,
            pad.touch_active, 1 if pad.touch_active else 0,
            pad.touch_x, pad.touch_y,
            0, 0, 0, 0,
            pad.timestamp_us & 0xFFFFFFFFFFFFFFFF,
            m[0], m[1], m[2], m[3], m[4], m[5],
        )
        payload = struct.pack("<I", MSG_DATA) + _slot_header(slot, pad) + body
        return _finish(self.server_id, payload)
