#!/usr/bin/env python3
"""Measure a game's real frame rate by asking gamescope.

gamescope (the SteamOS compositor) has a small Wayland interface,
``gamescope_control``. Its ``request_app_performance_stats`` request is
answered with the frame time of the next frame the given app presents -
the same number the performance overlay shows. Asking again after every
answer yields every frame.

This file is both a module and a stand-alone program. The plugin starts it
with the system Python as a separate process:

    python3 fps_probe.py <appid>

It then prints one line every half second: ``<fps> <frames>``. Running it
in its own process keeps the measurement accurate while the plugin's
interpreter is busy, and it needs nothing but the standard library.
"""

import glob
import os
import socket
import struct
import sys
import time

WINDOW_S = 0.5
MIN_VERSION = 6

_DISPLAY = 1
_REGISTRY = 2
_SYNC = 3
_CONTROL = 4

_REQ_PERF = 6          # gamescope_control.request_app_performance_stats
_EVT_PERF = 3          # gamescope_control.app_performance_stats


class ProbeError(Exception):
    pass


def find_sockets(root="/run/user"):
    """gamescope's Wayland sockets, newest first."""
    paths = [p for p in glob.glob(os.path.join(root, "*", "gamescope-[0-9]*"))
             if not p.endswith(".lock")]

    def mtime(p):
        try:
            return os.stat(p).st_mtime
        except OSError:
            return 0

    return sorted(paths, key=mtime, reverse=True)


def _msg(obj, opcode, payload=b""):
    return struct.pack("<IHH", obj, opcode, 8 + len(payload)) + payload


def _string(text):
    raw = text.encode() + b"\0"
    return struct.pack("<I", len(raw)) + raw + b"\0" * (-len(raw) % 4)


def _read_string(data, offset):
    (length,) = struct.unpack_from("<I", data, offset)
    text = data[offset + 4: offset + 4 + max(0, length - 1)].decode("utf-8", "replace")
    return text, offset + 4 + ((length + 3) & ~3)


class Connection(object):
    """Just enough of the Wayland wire protocol to use gamescope_control."""

    def __init__(self, path, timeout=2.0):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._buf = b""
        try:
            self.sock.settimeout(timeout)
            self.sock.connect(path)
            self._bind_control(timeout)
        except Exception:
            self.close()
            raise

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass

    def _send(self, data):
        self.sock.sendall(data)

    def _recv(self, timeout):
        """Returns complete messages as (object, opcode, payload)."""
        self.sock.settimeout(max(timeout, 0.001))
        try:
            chunk = self.sock.recv(65536)
        except socket.timeout:
            return []
        except InterruptedError:
            return []
        if not chunk:
            raise ProbeError("gamescope closed the connection")
        self._buf += chunk
        out = []
        while len(self._buf) >= 8:
            obj, opcode, size = struct.unpack_from("<IHH", self._buf)
            if size < 8:
                raise ProbeError("malformed message")
            if len(self._buf) < size:
                break
            out.append((obj, opcode, self._buf[8:size]))
            self._buf = self._buf[size:]
        return out

    def _bind_control(self, timeout):
        # wl_display.get_registry(new id), then wl_display.sync(new id)
        self._send(_msg(_DISPLAY, 1, struct.pack("<I", _REGISTRY))
                   + _msg(_DISPLAY, 0, struct.pack("<I", _SYNC)))
        name = version = None
        deadline = time.monotonic() + timeout
        done = False
        while not done:
            left = deadline - time.monotonic()
            if left <= 0:
                raise ProbeError("gamescope did not answer")
            for obj, opcode, payload in self._recv(left):
                if obj == _REGISTRY and opcode == 0:      # wl_registry.global
                    (gname,) = struct.unpack_from("<I", payload)
                    iface, off = _read_string(payload, 4)
                    (gver,) = struct.unpack_from("<I", payload, off)
                    if iface == "gamescope_control":
                        name, version = gname, gver
                elif obj == _SYNC and opcode == 0:        # wl_callback.done
                    done = True
                elif obj == _DISPLAY and opcode == 0:     # wl_display.error
                    raise ProbeError("gamescope reported a protocol error")
        if name is None:
            raise ProbeError("this gamescope has no control interface")
        if version < MIN_VERSION:
            raise ProbeError("this gamescope is too old to report frame times")
        # wl_registry.bind(name, interface, version, new id)
        self._send(_msg(_REGISTRY, 0, struct.pack("<I", name)
                        + _string("gamescope_control")
                        + struct.pack("<II", min(version, 7), _CONTROL)))

    def request(self, app_id):
        self._send(_msg(_CONTROL, _REQ_PERF, struct.pack("<I", app_id)))

    def frames(self, timeout):
        """Frame times (seconds) of perf answers that arrived."""
        out = []
        for obj, opcode, payload in self._recv(timeout):
            if obj == _CONTROL and opcode == _EVT_PERF and len(payload) >= 12:
                app, lo, hi = struct.unpack_from("<III", payload)
                out.append((app, ((hi << 32) | lo) / 1e9))
            elif obj == _DISPLAY and opcode == 0:
                raise ProbeError("gamescope reported a protocol error")
        return out


def measure(app_id, emit, stopped, socket_paths=None, window=WINDOW_S,
            clock=time.monotonic):
    """Follow one app until ``stopped()``; calls ``emit(fps, frames)`` once
    per window (fps is None while no frame arrived)."""
    while not stopped():
        paths = socket_paths() if socket_paths else find_sockets()
        conn = None
        for path in paths:
            try:
                conn = Connection(path)
                break
            except (OSError, ProbeError):
                conn = None
        if conn is None:
            emit(None, 0)
            _sleep(1.0, stopped)
            continue
        try:
            conn.request(app_id)
            frames = 0
            total = 0.0
            window_end = clock() + window
            while not stopped():
                for app, seconds in conn.frames(max(0.0, window_end - clock())):
                    if app != app_id:
                        continue
                    # One answer per request: ask for the next frame now.
                    conn.request(app_id)
                    if 0.0 < seconds < 1.0:
                        frames += 1
                        total += seconds
                if clock() >= window_end:
                    emit(frames / total if frames and total > 0 else None, frames)
                    frames = 0
                    total = 0.0
                    window_end = clock() + window
        except (OSError, ProbeError):
            emit(None, 0)
            _sleep(0.5, stopped)
        finally:
            conn.close()


def _sleep(seconds, stopped):
    end = time.monotonic() + seconds
    while time.monotonic() < end and not stopped():
        time.sleep(0.05)


def main(argv):
    if len(argv) != 2 or not argv[1].isdigit():
        sys.stderr.write("usage: fps_probe.py <appid>\n")
        return 2
    app_id = int(argv[1]) & 0xFFFFFFFF
    parent = os.getppid()

    def emit(fps, frames):
        sys.stdout.write("%s %d\n" % ("-" if fps is None else "%.2f" % fps, frames))
        sys.stdout.flush()

    def stopped():
        # Stop when the plugin that started us is gone
        return os.getppid() != parent

    try:
        measure(app_id, emit, stopped)
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
