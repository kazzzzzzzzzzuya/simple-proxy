"""Tablet (built-in) motion sensor of the Legion Go.

The tablet's IMU is exposed by the AMD Sensor Fusion Hub as two IIO
devices, "gyro_3d" and "accel_3d". They are polled through sysfs, which
works no matter whether another program (InputPlumber, iio-sensor-proxy)
already owns the IIO buffer.

Axis mapping and scale corrections follow InputPlumber's device profile
for the Legion Go (50-legion_go.yaml).
"""

import math
import os
import threading
import time

IIO_BASE = "/sys/bus/iio/devices"
GYRO_NAME = "gyro_3d"
ACCEL_NAME = "accel_3d"

POLL_HZ = 100.0
G = 9.80665
RETRY_S = 5.0

# The amd_sfh HID descriptors report a scale that is off by a power of
# ten. InputPlumber corrects accel x10 and gyro x100; the accel factor is
# verified against gravity at runtime and the gyro factor follows it.
_ACCEL_CORRECTIONS = (10.0, 1.0, 100.0, 0.1)
_GYRO_FOR_ACCEL = {10.0: 100.0, 1.0: 1.0, 100.0: 100.0, 0.1: 100.0}


def _read(path):
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except (OSError, IOError):
        return None


def _read_float(path, default):
    v = _read(path)
    if v is None:
        return default
    try:
        return float(v)
    except ValueError:
        return default


def find_devices(base=IIO_BASE):
    """Returns (gyro_dir, accel_dir) or None if the tablet IMU is absent."""
    gyro = accel = None
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return None
    for d in names:
        if not d.startswith("iio:device"):
            continue
        path = os.path.join(base, d)
        name = _read(os.path.join(path, "name"))
        if name == GYRO_NAME and gyro is None:
            if os.path.exists(os.path.join(path, "in_anglvel_x_raw")):
                gyro = path
        elif name == ACCEL_NAME and accel is None:
            if os.path.exists(os.path.join(path, "in_accel_x_raw")):
                accel = path
    if gyro and accel:
        return gyro, accel
    return None


def to_gamepad_frame(accel_iio, gyro_iio):
    """IIO sensor axes -> gamepad frame (see motion.py)."""
    ax, ay, az = accel_iio
    gx, gy, gz = gyro_iio
    return (-ay, az, ax), (gy, gz, -gx)


def pick_accel_correction(magnitude_ms2):
    """Choose the power-of-ten correction that makes gravity ~9.81 m/s^2."""
    best = None
    best_err = None
    for c in _ACCEL_CORRECTIONS:
        if magnitude_ms2 <= 0:
            break
        err = abs(math.log10(magnitude_ms2 * c / G))
        if best_err is None or err < best_err:
            best, best_err = c, err
    return best if best is not None else _ACCEL_CORRECTIONS[0]


class _Channel(object):
    """One sysfs raw attribute kept open and re-read with pread()."""

    def __init__(self, path):
        self.fd = os.open(path, os.O_RDONLY)

    def read(self):
        return int(os.pread(self.fd, 32, 0).strip() or b"0")

    def close(self):
        try:
            os.close(self.fd)
        except OSError:
            pass


class TabletImu(object):
    """Background poller. `on_sample(accel_g, gyro_dps, now)` is called from
    the poller thread for every sample."""

    def __init__(self, on_sample, logger=None, base=IIO_BASE):
        self._on_sample = on_sample
        self._log = logger
        self._base = base
        self._thread = None
        self._stop = threading.Event()
        self.error = None
        self.accel_correction = None
        self.gyro_correction = None

    def available(self):
        return find_devices(self._base) is not None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self):
        if self.running:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="lgo-gyro-tablet", daemon=True
        )
        self._thread.start()

    def stop(self):
        t = self._thread
        self._stop.set()
        if t is not None and t is not threading.current_thread():
            t.join(2.0)
        self._thread = None

    # ---- thread ----------------------------------------------------------

    def _run(self):
        while not self._stop.is_set():
            try:
                self._poll_loop()
            except Exception as e:  # keep trying, the sensor may come back
                self.error = "%s: %s" % (type(e).__name__, e)
                if self._log:
                    self._log.warning("Tablet IMU error: %s", self.error)
            if self._stop.wait(RETRY_S):
                break

    def _poll_loop(self):
        found = find_devices(self._base)
        if not found:
            self.error = "Tablet motion sensor not found"
            return
        gyro_dir, accel_dir = found

        gyro_scale = _read_float(os.path.join(gyro_dir, "in_anglvel_scale"), 1.0)
        accel_scale = _read_float(os.path.join(accel_dir, "in_accel_scale"), 1.0)
        gyro_ofs = _read_float(os.path.join(gyro_dir, "in_anglvel_offset"), 0.0)
        accel_ofs = _read_float(os.path.join(accel_dir, "in_accel_offset"), 0.0)

        chans = []
        try:
            for axis in "xyz":
                chans.append(_Channel(os.path.join(accel_dir, "in_accel_%s_raw" % axis)))
            for axis in "xyz":
                chans.append(_Channel(os.path.join(gyro_dir, "in_anglvel_%s_raw" % axis)))

            period = 1.0 / POLL_HZ
            probe = []
            accel_corr = self.accel_correction
            gyro_corr = self.gyro_correction
            self.error = None
            next_t = time.monotonic()

            while not self._stop.is_set():
                raw = [c.read() for c in chans]
                now = time.monotonic()
                accel = [(raw[i] + accel_ofs) * accel_scale for i in range(3)]
                gyro = [(raw[i + 3] + gyro_ofs) * gyro_scale for i in range(3)]

                if accel_corr is None:
                    # Learn the scale correction from gravity first.
                    probe.append(math.sqrt(sum(a * a for a in accel)))
                    if len(probe) >= 15:
                        probe.sort()
                        accel_corr = pick_accel_correction(probe[len(probe) // 2])
                        gyro_corr = _GYRO_FOR_ACCEL[accel_corr]
                        self.accel_correction = accel_corr
                        self.gyro_correction = gyro_corr
                        if self._log:
                            self._log.info(
                                "Tablet IMU scale corrections: accel x%g, gyro x%g",
                                accel_corr, gyro_corr,
                            )
                else:
                    accel_g = [a * accel_corr / G for a in accel]
                    gyro_dps = [math.degrees(g * gyro_corr) for g in gyro]
                    a, g = to_gamepad_frame(accel_g, gyro_dps)
                    self._on_sample(a, g, now)

                next_t += period
                delay = next_t - time.monotonic()
                if delay > 0:
                    if self._stop.wait(delay):
                        break
                else:
                    # Sensor reads are slower than the target rate
                    next_t = time.monotonic()
        finally:
            for c in chans:
                c.close()
