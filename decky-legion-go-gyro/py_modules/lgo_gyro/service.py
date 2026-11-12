"""The gyro service: one thread that reads the controllers, processes the
motion data and serves it to DSU clients."""

import collections
import errno
import logging
import os
import select
import socket
import struct
import threading
import time

from . import legion_hid, settings as cfg
from .dsu import (
    BATTERY_NONE, DsuServer, battery_level, fill_inputs,
)
from .motion import Calibration, OutputOptions, Sensor, process
from .tablet import TabletImu

TICK_S = 0.25
RESCAN_S = 1.0
SOCKET_RETRY_S = 2.0
ENABLE_RETRY_S = 2.0
ENABLE_GRACE_S = 0.3
NO_REPORT_ROTATE_S = 3.0
REPORT_STALE_S = 1.0
CAL_SAVE_INTERVAL_S = 15.0
MAX_READS_PER_WAKE = 64

_GONE = (errno.ENODEV, errno.EIO, errno.ENXIO, errno.EBADF, errno.EPIPE,
         errno.ECONNRESET, errno.ENOTCONN)


class HidHandle(object):
    """An opened controller state interface."""

    def __init__(self, fd, name, pid, mode):
        self.fd = fd
        self.name = name
        self.pid = pid
        self.mode = mode


def default_hid_opener(skip=0):
    """Find and open the Legion Go state interface.

    `skip` rotates through the candidates when one of them turned out not
    to deliver state reports. Returns (HidHandle | None, error | None).
    """
    cands = legion_hid.find_candidates()
    if not cands:
        return None, "Legion Go controllers not found"
    cand = cands[skip % len(cands)]
    try:
        fd = legion_hid.open_candidate(cand)
    except OSError as e:
        return None, "Cannot open %s: %s" % (cand.name, e.strerror or e)
    return HidHandle(fd, cand.name, cand.pid, cand.mode), None


def _dmi_model():
    try:
        with open("/sys/class/dmi/id/product_name", "r") as f:
            return f.read().strip()
    except (OSError, IOError):
        return ""


class _Side(object):
    """Connection bookkeeping of one controller."""

    def __init__(self):
        self.connected = False
        self.attached = False
        self.battery = 0
        self.last_enable = 0.0
        self.we_enabled = False


class GyroService(object):
    def __init__(self, settings_path, logger=None, hid_opener=None,
                 tablet_factory=None):
        self._path = settings_path
        self._log = logger or logging.getLogger("lgo_gyro")
        self._hid_opener = hid_opener or default_hid_opener

        self._lock = threading.Lock()
        self._settings = cfg.load(settings_path)
        self._pending_settings = None
        self._commands = collections.deque()

        self._thread = None
        self._stop = threading.Event()
        self._wake_r, self._wake_w = os.pipe()
        for fd in (self._wake_r, self._wake_w):
            os.set_blocking(fd, False)

        self.sensors = {}
        for name in cfg.SENSOR_NAMES:
            self.sensors[name] = Sensor(
                name, Calibration.from_dict(self._settings["calibration"][name])
            )
        self._sides = {"left": _Side(), "right": _Side()}

        self._tablet_samples = collections.deque(maxlen=64)
        make_tablet = tablet_factory or (
            lambda cb: TabletImu(cb, logger=self._log))
        self._tablet = make_tablet(self._on_tablet_sample)
        self._tablet_available = False
        self._tablet_checked = 0.0

        self._dsu = DsuServer(struct.unpack("<I", os.urandom(4))[0])
        self._sock = None
        self._sock_bound = None
        self._sock_error = None
        self._sock_retry = 0.0

        self._hid = None
        self._hid_error = None
        self._hid_skip = 0
        self._hid_scan = 0.0
        self._hid_opened = 0.0
        self._last_report = 0.0

        self._opts = {}
        self._combined_source = None
        self._last_input_key = {}
        self._last_sent = [0.0] * 4
        self._cal_saved = 0.0
        self._manual_serials = [0] * len(cfg.SENSOR_NAMES)
        self._io_open = False
        self._model = _dmi_model()
        self._status = self._build_status(time.monotonic())
        self._load_options()

    # ------------------------------------------------------------------
    # Public API (called from the Decky/asyncio thread)
    # ------------------------------------------------------------------

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="lgo-gyro", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake()
        t = self._thread
        if t is not None:
            t.join(5.0)
        self._thread = None

    def close(self):
        """Stop and release everything. The service cannot be reused."""
        self.stop()
        for fd in (self._wake_r, self._wake_w):
            try:
                os.close(fd)
            except OSError:
                pass

    def get_settings(self):
        with self._lock:
            return self._export_settings()

    def update_settings(self, patch):
        with self._lock:
            base = self._pending_settings or self._settings
            new = cfg.merge(base, patch)
            # Calibration is owned by the service thread.
            new["calibration"] = base["calibration"]
            self._pending_settings = new
            out = self._export_settings(new)
        self._wake()
        return out

    def get_defaults(self):
        return self._export_settings(cfg.defaults())

    def reset_settings(self):
        with self._lock:
            base = self._pending_settings or self._settings
            new = cfg.defaults()
            # The panel layout is a view preference, not a setting to reset.
            new["advanced"] = base["advanced"]
            self._pending_settings = new
            out = self._export_settings(new)
        self._commands.append(("reset_calibration", None))
        self._wake()
        return out

    def get_status(self):
        return self._status

    def request_calibration(self, target):
        """Start a manual calibration. Returns the sensor name that is
        being calibrated, or None if the target has no live sensor."""
        if target not in cfg.SLOT_NAMES:
            return None
        name = target if target != "combined" else self._combined_source
        if name is None:
            return None
        sensor = self.sensors[name]
        if not sensor.is_live(time.monotonic()):
            return None
        self._commands.append(("calibrate", name))
        self._wake()
        return name

    def request_calibration_all(self):
        """Start a manual calibration of every sensor that is streaming.
        Returns the list of sensor names being calibrated."""
        now = time.monotonic()
        names = [n for n in cfg.SENSOR_NAMES if self.sensors[n].is_live(now)]
        for name in names:
            self._commands.append(("calibrate", name))
        if names:
            self._wake()
        return names

    def calibration_state(self, sensor_name):
        s = self.sensors[sensor_name]
        return s.calibrating, s.manual_serial, s.manual_result

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _export_settings(self, src=None):
        src = src or self._pending_settings or self._settings
        out = {k: v for k, v in src.items() if k != "calibration"}
        out["slots"] = {k: dict(v) for k, v in src["slots"].items()}
        return out

    def _wake(self):
        try:
            os.write(self._wake_w, b"\0")
        except OSError:
            pass

    def _on_tablet_sample(self, accel_g, gyro_dps, now):
        self._tablet_samples.append((accel_g, gyro_dps, now))
        self._wake()

    def _load_options(self):
        slots = self._settings["slots"]
        self._opts = {name: OutputOptions(slots[name]) for name in cfg.SLOT_NAMES}

    def _save(self):
        with self._lock:
            data = dict(self._settings)
            data["calibration"] = {
                name: self.sensors[name].cal.to_dict()
                for name in cfg.SENSOR_NAMES
            }
            self._settings = data
        if not cfg.save(self._path, data):
            self._log.warning("Could not write settings to %s", self._path)
        for s in self.sensors.values():
            s.cal_dirty = False
        self._cal_saved = time.monotonic()

    # ------------------------------------------------------------------
    # Thread
    # ------------------------------------------------------------------

    def _run(self):
        self._log.info("Legion Go Gyro service started")
        last_tick = 0.0
        try:
            while not self._stop.is_set():
                now = time.monotonic()
                self._apply_pending(now)

                if not self._settings["enabled"]:
                    self._shutdown_io()
                    self._status = self._build_status(now)
                    self._wait([self._wake_r], 0.5)
                    self._drain_wake()
                    self._run_commands(now)
                    continue

                self._io_open = True
                self._ensure_socket(now)
                self._ensure_hid(now)

                rlist = [self._wake_r]
                if self._sock is not None:
                    rlist.append(self._sock)
                if self._hid is not None:
                    rlist.append(self._hid.fd)
                ready = self._wait(rlist, TICK_S)

                now = time.monotonic()
                if self._hid is not None and self._hid.fd in ready:
                    self._read_hid(now)
                if self._wake_r in ready:
                    self._drain_wake()
                    self._run_commands(now)
                    self._process_tablet()
                if self._sock is not None and self._sock in ready:
                    self._read_socket(now)

                if now - last_tick >= TICK_S:
                    last_tick = now
                    self._tick(now)
        except Exception:
            self._log.exception("Legion Go Gyro service crashed")
        finally:
            self._shutdown_io()
            if any(s.cal_dirty for s in self.sensors.values()):
                self._save()
            self._status = self._build_status(time.monotonic())
            self._log.info("Legion Go Gyro service stopped")

    @staticmethod
    def _wait(rlist, timeout):
        try:
            ready, _, _ = select.select(rlist, [], [], timeout)
            return ready
        except (OSError, ValueError):
            time.sleep(0.05)
            return []

    def _drain_wake(self):
        try:
            while os.read(self._wake_r, 256):
                pass
        except OSError:
            pass

    def _apply_pending(self, now):
        with self._lock:
            new = self._pending_settings
            self._pending_settings = None
            if new is None:
                return
            old = self._settings
            self._settings = new
        if (old["port"], old["lan"]) != (new["port"], new["lan"]):
            self._close_socket()
            self._sock_retry = 0.0
        self._load_options()
        self._save()

    def _run_commands(self, now):
        while self._commands:
            cmd, arg = self._commands.popleft()
            if cmd == "calibrate":
                self.sensors[arg].start_manual(now)
            elif cmd == "reset_calibration":
                for s in self.sensors.values():
                    s.reset_calibration()
                self._save()

    # ---- socket ----------------------------------------------------------

    def _ensure_socket(self, now):
        if self._sock is not None or now < self._sock_retry:
            return
        host = "0.0.0.0" if self._settings["lan"] else "127.0.0.1"
        port = self._settings["port"]
        sock = None
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((host, port))
            sock.setblocking(False)
        except OSError as e:
            if sock is not None:
                sock.close()
            if e.errno == errno.EADDRINUSE:
                self._sock_error = (
                    "Port %d is already used by another motion server" % port)
            else:
                self._sock_error = "Cannot open port %d: %s" % (
                    port, e.strerror or e)
            self._sock_retry = now + SOCKET_RETRY_S
            return
        self._sock = sock
        self._sock_bound = (host, sock.getsockname()[1])
        self._sock_error = None
        self._log.info("DSU server listening on %s:%d", *self._sock_bound)

    def _close_socket(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._sock_bound = None
        self._dsu.clients.clear()

    def _read_socket(self, now):
        sock = self._sock
        for _ in range(MAX_READS_PER_WAKE):
            try:
                data, addr = sock.recvfrom(2048)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                # e.g. ICMP "port unreachable" from a client that went away
                continue
            for reply in self._dsu.handle(data, addr, now):
                self._sendto(reply, addr)

    def _sendto(self, data, addr):
        try:
            self._sock.sendto(data, addr)
        except OSError:
            pass

    def _send_slot(self, slot, now):
        if self._sock is None:
            return
        self._last_sent[slot] = now
        addrs = self._dsu.subscribers(slot, now)
        if not addrs:
            return
        packet = self._dsu.pad_packet(slot)
        for addr in addrs:
            self._sendto(packet, addr)

    # ---- controllers -----------------------------------------------------

    def _ensure_hid(self, now):
        if self._hid is not None:
            # A wrong interface never produces state reports: try the next.
            idle = now - max(self._last_report, self._hid_opened)
            if idle > NO_REPORT_ROTATE_S and len(legion_hid.find_candidates()) > 1:
                self._log.info("No state reports on %s, trying next interface",
                               self._hid.name)
                self._close_hid()
                self._hid_skip += 1
                self._hid_scan = 0.0
            else:
                return
        if now - self._hid_scan < RESCAN_S:
            return
        self._hid_scan = now
        try:
            handle, error = self._hid_opener(self._hid_skip)
        except Exception as e:
            handle, error = None, "%s: %s" % (type(e).__name__, e)
        if handle is None:
            self._hid_error = error
            return
        self._hid = handle
        self._hid_error = None
        self._hid_opened = now
        self._last_report = 0.0
        for side in self._sides.values():
            side.last_enable = now - ENABLE_RETRY_S + ENABLE_GRACE_S
            side.we_enabled = False
        self._log.info("Opened %s (%04x, %s mode)",
                       handle.name, handle.pid, handle.mode)

    def _close_hid(self, disable=True):
        hid = self._hid
        if hid is None:
            return
        if disable:
            for name, side in self._sides.items():
                if side.we_enabled:
                    self._write_hid(legion_hid.cmd_disable_gyro(name))
                    side.we_enabled = False
        self._hid = None
        try:
            os.close(hid.fd)
        except OSError:
            pass
        for name, side in self._sides.items():
            side.connected = False
            side.attached = False
            side.we_enabled = False
            self.sensors[name].reset_stream()

    def _write_hid(self, commands):
        hid = self._hid
        if hid is None:
            return False
        for cmd in commands:
            try:
                os.write(hid.fd, cmd)
            except (BlockingIOError, InterruptedError):
                return False
            except OSError as e:
                self._log.warning("HID write failed: %s", e)
                return False
        return True

    def _read_hid(self, now):
        hid = self._hid
        for _ in range(MAX_READS_PER_WAKE):
            try:
                buf = os.read(hid.fd, legion_hid.READ_SIZE)
            except (BlockingIOError, InterruptedError):
                return
            except OSError as e:
                if e.errno in _GONE:
                    self._log.info("Controller interface went away (%s)", e)
                else:
                    self._log.warning("HID read failed: %s", e)
                self._close_hid(disable=False)
                self._hid_scan = now
                self._on_disconnect(now)
                return
            if not buf:
                self._close_hid(disable=False)
                self._hid_scan = now
                self._on_disconnect(now)
                return
            report = legion_hid.parse_report(buf)
            if report is not None:
                self._last_report = now
                self._on_report(report, now)

    def _on_disconnect(self, now):
        """All controllers vanished (mode switch, suspend, ...)."""
        self._combined_source = self._resolve_combined()
        for slot_name in ("left", "right"):
            self._update_controller_slot(slot_name, None, False, now)
        self._update_combined(None, {}, now)

    def _on_report(self, rep, now):
        auto = self._settings["auto_calibrate"]
        fresh = {}
        for name, sample in (("left", rep.left), ("right", rep.right)):
            side = self._sides[name]
            sensor = self.sensors[name]
            if side.connected and not sample.connected:
                sensor.reset_stream()
                side.we_enabled = False
            elif sample.connected and not side.connected:
                # (Re)connected: (re)enable its IMU right away if needed
                side.last_enable = now - ENABLE_RETRY_S + ENABLE_GRACE_S
            side.connected = sample.connected
            side.attached = sample.attached
            side.battery = sample.battery
            fresh[name] = (
                sensor.feed_controller(sample, now, auto)
                if sample.connected else False
            )

        self._combined_source = self._resolve_combined()
        for name in ("left", "right"):
            self._update_controller_slot(name, rep.buttons, fresh[name], now)
        self._update_combined(rep.buttons, fresh, now)

    def _update_controller_slot(self, name, buttons, fresh, now):
        slot = cfg.SLOT_INDEX[name]
        pad = self._dsu.pads[slot]
        side = self._sides[name]
        was = pad.connected
        pad.connected = bool(
            self._settings["slots"][name]["enabled"] and side.connected
            and buttons is not None
        )
        if not pad.connected:
            if was:
                pad.clear_inputs()
                self._last_input_key.pop(slot, None)
                self._send_slot(slot, now)
            return
        pad.wireless = not side.attached
        pad.battery = battery_level(side.battery)
        fill_inputs(pad, buttons, name)
        if fresh:
            sensor = self.sensors[name]
            pad.motion = process(sensor, self._opts[name])
            pad.timestamp_us = sensor.timestamp_us
        key = pad.input_key()
        if fresh or not was or key != self._last_input_key.get(slot):
            self._last_input_key[slot] = key
            self._send_slot(slot, now)

    def _resolve_combined(self):
        """Which sensor feeds the combined (whole handheld) slot."""
        conf = self._settings["slots"]["combined"]
        if not conf["enabled"]:
            return None
        source = conf["source"]
        left = self._sides["left"].connected
        right = self._sides["right"].connected
        if source == "right":
            return "right" if right else None
        if source == "left":
            return "left" if left else None
        if source == "tablet":
            return "tablet" if self._tablet_available else None
        if right:
            return "right"
        if left:
            return "left"
        if self._tablet_available:
            return "tablet"
        return None

    def _update_combined(self, buttons, fresh, now):
        pad = self._dsu.pads[0]
        source = self._combined_source if buttons is not None else (
            "tablet" if self._combined_source == "tablet" else None)
        conf = self._settings["slots"]["combined"]
        any_controller = buttons is not None and (
            self._sides["left"].connected or self._sides["right"].connected)
        was = pad.connected
        pad.connected = bool(conf["enabled"] and (any_controller or source == "tablet"))
        if not pad.connected:
            if was:
                pad.clear_inputs()
                self._last_input_key.pop(0, None)
                self._send_slot(0, now)
            return
        if buttons is not None:
            fill_inputs(pad, buttons, "combined")
        else:
            pad.clear_inputs()
        attached = [s for s in self._sides.values() if s.connected and s.attached]
        connected = [s for s in self._sides.values() if s.connected]
        pad.wireless = bool(connected) and not attached
        pad.battery = (
            battery_level(min(s.battery for s in connected))
            if connected else BATTERY_NONE
        )
        new_motion = False
        if source in ("left", "right") and fresh.get(source):
            sensor = self.sensors[source]
            pad.motion = process(sensor, self._opts["combined"])
            pad.timestamp_us = max(sensor.timestamp_us, pad.timestamp_us + 1)
            new_motion = True
        key = pad.input_key()
        if new_motion or not was or key != self._last_input_key.get(0):
            self._last_input_key[0] = key
            self._send_slot(0, now)

    # ---- tablet ----------------------------------------------------------

    def _process_tablet(self):
        if not self._tablet_samples:
            return
        auto = self._settings["auto_calibrate"]
        sensor = self.sensors["tablet"]
        pad = self._dsu.pads[0]
        while self._tablet_samples:
            accel, gyro, now = self._tablet_samples.popleft()
            sensor.feed_si(accel, gyro, now, auto)
            if self._combined_source != "tablet" or not pad.connected:
                continue
            pad.motion = process(sensor, self._opts["combined"])
            pad.timestamp_us = max(sensor.timestamp_us, pad.timestamp_us + 1)
            self._send_slot(0, now)

    # ---- periodic --------------------------------------------------------

    def _needs_imu(self, name):
        return bool(
            self._settings["slots"][name]["enabled"]
            or self._combined_source == name
        )

    def _tick(self, now):
        # Tablet sensor availability (cheap sysfs lookup, every 2 s)
        if now - self._tablet_checked >= 2.0:
            self._tablet_checked = now
            try:
                self._tablet_available = bool(self._tablet.available())
            except Exception:
                self._tablet_available = False

        flowing = (
            self._hid is not None
            and self._last_report > 0
            and now - self._last_report < REPORT_STALE_S
        )
        if flowing:
            self._combined_source = self._resolve_combined()
        else:
            # No state reports: nothing we knew about the controllers is
            # still trustworthy.
            for name, side in self._sides.items():
                if side.connected:
                    side.connected = False
                    side.attached = False
                    self.sensors[name].reset_stream()
            self._combined_source = self._resolve_combined()
            for name in ("left", "right"):
                self._update_controller_slot(name, None, False, now)
            self._update_combined(None, {}, now)

        want_tablet = self._combined_source == "tablet"
        if want_tablet and not self._tablet.running:
            self._tablet.start()
        elif not want_tablet and self._tablet.running:
            self._tablet.stop()
            self.sensors["tablet"].reset_stream()

        # Keep the controller IMUs switched on (or off)
        if self._hid is not None:
            for name, side in self._sides.items():
                sensor = self.sensors[name]
                want = self._needs_imu(name)
                if want and side.connected and not sensor.is_live(now):
                    if now - side.last_enable >= ENABLE_RETRY_S:
                        side.last_enable = now
                        if self._write_hid(legion_hid.cmd_enable_gyro(name)):
                            side.we_enabled = True
                elif not want and side.we_enabled and side.connected:
                    self._write_hid(legion_hid.cmd_disable_gyro(name))
                    side.we_enabled = False

        # Manual calibrations, stale motion, keep-alives
        saved = self._manual_serials != [
            s.manual_serial for s in self.sensors.values()]
        for name, sensor in self.sensors.items():
            had = sensor.manual_serial
            sensor.poll_manual(now)
            if sensor.manual_serial != had and sensor.cal_dirty:
                saved = True
        for slot_name in cfg.SLOT_NAMES:
            slot = cfg.SLOT_INDEX[slot_name]
            pad = self._dsu.pads[slot]
            if not pad.connected:
                continue
            src = slot_name if slot_name != "combined" else self._combined_source
            live = src is not None and self.sensors[src].is_live(now)
            if not live and any(pad.motion[3:]):
                # Never leave a rotation "stuck" when the data stops
                m = pad.motion
                pad.motion = (m[0], m[1], m[2], 0.0, 0.0, 0.0)
                pad.timestamp_us += 1
                self._send_slot(slot, now)
            elif now - self._last_sent[slot] >= TICK_S:
                self._send_slot(slot, now)

        self._dsu.prune(now)
        self._manual_serials = [s.manual_serial for s in self.sensors.values()]
        dirty = any(s.cal_dirty for s in self.sensors.values())
        if dirty and (saved or now - self._cal_saved >= CAL_SAVE_INTERVAL_S):
            self._save()

        self._status = self._build_status(now)

    def _shutdown_io(self):
        if not self._io_open:
            return
        self._io_open = False
        if self._tablet.running:
            self._tablet.stop()
        self.sensors["tablet"].reset_stream()
        self._close_hid(disable=True)
        self._close_socket()
        for pad in self._dsu.pads:
            pad.connected = False
            pad.clear_inputs()
        self._combined_source = None
        legion_hid.cleanup_private_nodes()

    # ---- status ----------------------------------------------------------

    def _build_status(self, now):
        s = self._settings
        hid = self._hid
        slots = {}
        for name in cfg.SLOT_NAMES:
            slot = cfg.SLOT_INDEX[name]
            pad = self._dsu.pads[slot]
            src = name if name != "combined" else self._combined_source
            sensor = self.sensors[src] if src else None
            entry = {
                "slot": slot,
                "connected": pad.connected,
                "source": src,
                "live": bool(sensor and sensor.is_live(now)),
                "rate": round(sensor.rate_hz, 1) if sensor else 0.0,
                "accel": [round(v, 3) for v in pad.motion[:3]],
                "gyro": [round(v, 1) for v in pad.motion[3:]],
                "calibrated": bool(sensor and sensor.cal.calibrated),
                "calibrating": bool(sensor and sensor.calibrating),
                "rest_noise": (
                    round(sensor.rest_noise_dps, 1)
                    if sensor and sensor.rest_noise_dps is not None else None),
            }
            if name != "combined":
                side = self._sides[name]
                entry["controller_connected"] = side.connected
                entry["attached"] = side.attached
                entry["battery"] = side.battery if side.connected else None
            slots[name] = entry

        return {
            "enabled": s["enabled"],
            "running": self._thread is not None and self._thread.is_alive(),
            "model": self._model,
            "device": {
                "found": hid is not None,
                "receiving": bool(hid and now - self._last_report < 1.0
                                  and self._last_report > 0),
                "mode": legion_hid.MODE_LABELS.get(hid.mode, hid.mode) if hid else None,
                "error": None if hid else self._hid_error,
            },
            "server": {
                "listening": self._sock is not None,
                "host": self._sock_bound[0] if self._sock_bound else None,
                "port": self._sock_bound[1] if self._sock_bound else s["port"],
                "clients": len(self._dsu.clients),
                "error": self._sock_error,
            },
            "tablet": {
                "available": self._tablet_available,
                "active": self._tablet.running,
                "error": getattr(self._tablet, "error", None),
            },
            "slots": slots,
        }
