#!/usr/bin/env python3
"""Print live motion data from the Legion Go Gyro DSU server.

Run it on the Legion Go (Desktop mode terminal or SSH) to check that motion
data is flowing without starting an emulator:

    python3 dsu_monitor.py            # all slots, 127.0.0.1:26760
    python3 dsu_monitor.py --slot 2   # only the right controller
"""

import argparse
import socket
import struct
import time
import zlib

NAMES = {0: "handheld", 1: "left", 2: "right", 3: "-"}


def packet(msg, body, client_id=0x4C474F01):
    payload = struct.pack("<I", msg) + body
    head = struct.pack("<4sHHII", b"DSUC", 1001, len(payload), 0, client_id)
    crc = zlib.crc32(head + payload) & 0xFFFFFFFF
    return struct.pack("<4sHHII", b"DSUC", 1001, len(payload), crc, client_id) + payload


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=26760)
    ap.add_argument("--slot", type=int, choices=(0, 1, 2), default=None)
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(0.5)
    addr = (args.host, args.port)
    last_request = 0.0
    latest = {}
    last_print = 0.0
    print("Listening to %s:%d (Ctrl+C to stop)" % addr)
    try:
        while True:
            now = time.monotonic()
            if now - last_request > 1.0:
                last_request = now
                if args.slot is None:
                    sock.sendto(packet(0x100002, bytes(8)), addr)
                else:
                    sock.sendto(packet(0x100002, bytes([1, args.slot]) + bytes(6)), addr)
            try:
                data, _ = sock.recvfrom(2048)
            except socket.timeout:
                if now - last_print > 2.0:
                    last_print = now
                    print("no data - is the plugin running and a controller connected?")
                continue
            if len(data) < 100 or data[:4] != b"DSUS":
                continue
            if struct.unpack_from("<I", data, 16)[0] != 0x100002:
                continue
            slot, active = data[20], data[31]
            accel = struct.unpack_from("<fff", data, 76)
            gyro = struct.unpack_from("<fff", data, 88)
            latest[slot] = (active, accel, gyro)
            if now - last_print >= 0.1:
                last_print = now
                parts = []
                for s in sorted(latest):
                    active, a, g = latest[s]
                    if not active:
                        parts.append("%-8s off" % NAMES.get(s, s))
                        continue
                    parts.append(
                        "%-8s acc %+5.2f %+5.2f %+5.2f  gyro P%+7.1f Y%+7.1f R%+7.1f"
                        % ((NAMES.get(s, s),) + a + g))
                print(" | ".join(parts))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
