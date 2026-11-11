"""Test helpers: synthetic Legion Go state reports and a tiny DSU client."""

import os
import socket
import struct
import sys
import zlib

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "py_modules"))


def _put16(buf, ofs, value):
    buf[ofs:ofs + 2] = struct.pack(">h", int(value))


def make_report(
    left_state=3, right_state=3, left_batt=80, right_batt=60,
    left_ts=0, right_ts=0,
    left_raw_accel=(0, 0, 0), left_raw_gyro=(0, 0, 0),
    right_raw_accel=(0, 0, 0), right_raw_gyro=(0, 0, 0),
    b18=0, b19=0, b20=0, sticks=(0x80, 0x80, 0x80, 0x80),
    lt=0, rt=0, touch=(0, 0),
):
    """Build a 60 byte state report.

    The raw_* tuples are the values exactly as they sit in the report, in
    report order: left = (X, Z, Y), right = (Z, X, Y).
    """
    buf = bytearray(60)
    buf[0] = 0x04
    buf[1] = 0x19
    buf[2] = 0x74
    buf[5] = left_batt
    buf[7] = right_batt
    buf[10] = 1 if left_state in (2, 3) else 0
    buf[11] = 1 if right_state in (2, 3) else 0
    buf[12] = left_state
    buf[13] = right_state
    buf[14:18] = bytes(sticks)
    buf[18], buf[19], buf[20] = b18, b19, b20
    buf[22], buf[23] = lt, rt
    buf[26:28] = struct.pack(">H", touch[0])
    buf[28:30] = struct.pack(">H", touch[1])
    buf[34] = left_ts & 0xFF
    for i, v in enumerate(left_raw_accel):
        _put16(buf, 35 + 2 * i, v)
    for i, v in enumerate(left_raw_gyro):
        _put16(buf, 41 + 2 * i, v)
    buf[47] = right_ts & 0xFF
    for i, v in enumerate(right_raw_accel):
        _put16(buf, 48 + 2 * i, v)
    for i, v in enumerate(right_raw_gyro):
        _put16(buf, 54 + 2 * i, v)
    return bytes(buf)


class DsuClient(object):
    """Minimal DSU client, enough to talk to the server under test."""

    def __init__(self, port, host="127.0.0.1"):
        self.addr = (host, port)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.settimeout(2.0)
        self.id = 0x1234ABCD

    def close(self):
        self.sock.close()

    def _send(self, msg, body, corrupt=False):
        payload = struct.pack("<I", msg) + body
        head = struct.pack("<4sHHII", b"DSUC", 1001, len(payload), 0, self.id)
        crc = zlib.crc32(head + payload) & 0xFFFFFFFF
        if corrupt:
            crc ^= 0xFFFF
        head = struct.pack("<4sHHII", b"DSUC", 1001, len(payload), crc, self.id)
        self.sock.sendto(head + payload, self.addr)

    def request_version(self):
        self._send(0x100000, b"")

    def request_ports(self, slots=(0, 1, 2, 3), corrupt=False):
        self._send(0x100001, struct.pack("<i", len(slots)) + bytes(slots), corrupt)

    def subscribe(self, slot=None):
        if slot is None:
            self._send(0x100002, bytes(8))
        else:
            self._send(0x100002, bytes([1, slot]) + bytes(6))

    def recv(self):
        data, _ = self.sock.recvfrom(2048)
        return parse_packet(data)

    def recv_pad(self, slot, predicate=None, tries=400):
        """Next pad data packet of a slot (optionally matching predicate)."""
        for _ in range(tries):
            p = self.recv()
            if p["type"] == 0x100002 and p["slot"] == slot:
                if predicate is None or predicate(p):
                    return p
        raise AssertionError("no matching pad packet for slot %d" % slot)


def parse_packet(data):
    magic, version, length, crc, sid = struct.unpack_from("<4sHHII", data)
    assert magic == b"DSUS", magic
    assert version == 1001
    assert length == len(data) - 16, (length, len(data))
    check = zlib.crc32(data[:8] + bytes(4) + data[12:]) & 0xFFFFFFFF
    assert check == crc, "bad crc"
    (msg,) = struct.unpack_from("<I", data, 16)
    out = {"type": msg, "server_id": sid, "size": len(data)}
    if msg == 0x100000:
        (out["version"],) = struct.unpack_from("<H", data, 20)
        return out
    slot, state, model, conn = struct.unpack_from("<BBBB", data, 20)
    out.update(slot=slot, state=state, model=model, conn=conn,
               mac=data[24:30], battery=data[30])
    if msg == 0x100002:
        assert len(data) == 100, len(data)
        out["active"] = data[31]
        (out["counter"],) = struct.unpack_from("<I", data, 32)
        out["buttons1"], out["buttons2"], out["home"], out["touch_btn"] = data[36:40]
        out["lx"], out["ly"], out["rx"], out["ry"] = data[40:44]
        out["analog"] = data[44:56]
        out["touch1"] = struct.unpack_from("<BBHH", data, 56)
        (out["timestamp"],) = struct.unpack_from("<Q", data, 68)
        out["accel"] = struct.unpack_from("<fff", data, 76)
        out["gyro"] = struct.unpack_from("<fff", data, 88)
    return out
