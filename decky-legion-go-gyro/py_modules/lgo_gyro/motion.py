"""Motion maths: calibration, per-output tuning and frame conversion.

Everything in here works in the "gamepad frame" used by DualShock 4 /
DualSense controllers (and SDL):

    +X  to the right
    +Y  out of the button face (reads +1 g when the device lies flat)
    +Z  towards the player (bottom edge of the device)

Accelerations are in g, angular rates in degrees per second, rotations are
right handed around the axis.
"""

import math

from .legion_hid import ACCEL_G_PER_LSB, GYRO_DPS_PER_LSB, IMU_TICK_US

ROTATIONS = (0, 90, 180, 270)

# ---- stillness detection / calibration tuning ----------------------------
AUTO_WINDOW_S = 1.5
AUTO_MIN_SAMPLES = 40
AUTO_GYRO_P2P_DPS = 1.2
AUTO_ACCEL_P2P_G = 0.03
AUTO_BIAS_LIMIT_DPS = 6.0

MANUAL_WINDOW_S = 2.0
MANUAL_MIN_SAMPLES = 20
MANUAL_GYRO_P2P_DPS = 8.0
MANUAL_ACCEL_P2P_G = 0.15

ACCEL_SCALE_MIN = 0.75
ACCEL_SCALE_MAX = 1.35
BIAS_LIMIT_DPS = 60.0
GRAVITY_SANE_MIN = 0.6
GRAVITY_SANE_MAX = 1.6

# A sensor is considered live if a new sample arrived this recently.
LIVE_TIMEOUT_S = 0.6
# Resync the sample clock to the host clock if they drift further apart.
CLOCK_SLACK_US = 50000


def rotate(v, degrees):
    """Rotate a vector for a device that is held turned clockwise by
    `degrees` (seen looking down onto the buttons) so that the result is
    again expressed in the player's gamepad frame."""
    x, y, z = v
    if degrees == 90:
        return (-z, y, x)
    if degrees == 180:
        return (-x, y, -z)
    if degrees == 270:
        return (z, y, -x)
    return (x, y, z)


def to_dsu(accel, gyro):
    """Gamepad frame -> DSU/cemuhook wire convention.

    Returns (accX, accY, accZ, pitch, yaw, roll). This is the mapping the
    reference implementation (DS4Windows) applies to DualShock 4 data.
    """
    ax, ay, az = accel
    gx, gy, gz = gyro
    return (-ax, -ay, -az, gx, -gy, -gz)


class Calibration(object):
    """Per physical sensor calibration (stored in the settings file)."""

    __slots__ = ("bias", "accel_scale", "calibrated")

    def __init__(self, bias=(0.0, 0.0, 0.0), accel_scale=1.0, calibrated=False):
        self.bias = tuple(float(b) for b in bias)
        self.accel_scale = float(accel_scale)
        self.calibrated = bool(calibrated)

    def to_dict(self):
        return {
            "bias": [round(b, 4) for b in self.bias],
            "accel_scale": round(self.accel_scale, 5),
            "calibrated": self.calibrated,
        }

    @classmethod
    def from_dict(cls, d):
        try:
            bias = [float(b) for b in d.get("bias", (0, 0, 0))]
            scale = float(d.get("accel_scale", 1.0))
            if len(bias) != 3 or not all(math.isfinite(b) for b in bias):
                raise ValueError("bad bias")
            if any(abs(b) > BIAS_LIMIT_DPS for b in bias):
                raise ValueError("bias out of range")
            if not (ACCEL_SCALE_MIN <= scale <= ACCEL_SCALE_MAX):
                scale = 1.0
            return cls(bias, scale, bool(d.get("calibrated", False)))
        except (TypeError, ValueError, AttributeError):
            return cls()


class _Window(object):
    """Collects samples and reports mean / peak-to-peak per axis."""

    def __init__(self):
        self.reset(0.0)

    def reset(self, now):
        self.start = now
        self.n = 0
        self.sum = [0.0] * 6
        self.lo = [float("inf")] * 6
        self.hi = [float("-inf")] * 6

    def add(self, accel, gyro):
        vals = (accel[0], accel[1], accel[2], gyro[0], gyro[1], gyro[2])
        s, lo, hi = self.sum, self.lo, self.hi
        for i in range(6):
            v = vals[i]
            s[i] += v
            if v < lo[i]:
                lo[i] = v
            if v > hi[i]:
                hi[i] = v
        self.n += 1

    def mean(self):
        n = float(self.n or 1)
        return [v / n for v in self.sum]

    def p2p_accel(self):
        return max(self.hi[i] - self.lo[i] for i in range(3))

    def p2p_gyro(self):
        return max(self.hi[i] - self.lo[i] for i in range(3, 6))


class Sensor(object):
    """State of one physical IMU (left controller, right controller, tablet)."""

    def __init__(self, name, calibration=None):
        self.name = name
        self.cal = calibration or Calibration()
        self.cal_dirty = False

        # Latest calibrated sample
        self.accel = (0.0, 0.0, 0.0)
        self.gyro = (0.0, 0.0, 0.0)
        self.timestamp_us = 0
        self.last_sample_time = 0.0
        self.sample_count = 0
        self.rate_hz = 0.0

        self._last_raw_gyro = [0, 0, 0]
        self._last_ts = None
        self._clock_us = None
        self._rate_t0 = 0.0
        self._rate_n = 0

        self._auto = _Window()
        self._manual = None
        self._manual_deadline = 0.0
        self.manual_result = None  # None | "ok" | "moved" | "nodata"
        self.manual_serial = 0

    # ---- input -----------------------------------------------------------

    def feed_controller(self, side_sample, now, auto_calibrate):
        """Feed a decoded controller sample. Returns True if it is new."""
        ts = side_sample.imu_ts
        if self._last_ts is None:
            self._last_ts = ts
            # Without a previous timestamp we cannot tell whether the
            # sensor is streaming; treat the first report as a baseline.
            self._last_raw_gyro = list(side_sample.gyro)
            return False
        ticks = (ts - self._last_ts) & 0xFF
        if ticks == 0:
            return False
        self._last_ts = ts

        raw = side_sample.gyro
        last = self._last_raw_gyro
        for i in range(3):
            # Firmware quirk: the gyro occasionally emits a bogus 254/255.
            # Keep the previous value for that axis when it happens.
            if abs(raw[i]) not in (254, 255):
                last[i] = raw[i]

        accel = (
            side_sample.accel[0] * ACCEL_G_PER_LSB,
            side_sample.accel[1] * ACCEL_G_PER_LSB,
            side_sample.accel[2] * ACCEL_G_PER_LSB,
        )
        gyro = (
            last[0] * GYRO_DPS_PER_LSB,
            last[1] * GYRO_DPS_PER_LSB,
            last[2] * GYRO_DPS_PER_LSB,
        )
        self._advance_clock(now, ticks * IMU_TICK_US)
        self._store(accel, gyro, now, auto_calibrate)
        return True

    def feed_si(self, accel_g, gyro_dps, now, auto_calibrate):
        """Feed a sample that is already in g / deg/s (tablet sensor)."""
        self._advance_clock(now, None)
        self._store(accel_g, gyro_dps, now, auto_calibrate)
        return True

    def reset_stream(self):
        """Forget stream state after a disconnect."""
        self._last_ts = None
        self._clock_us = None
        self.last_sample_time = 0.0
        self.rate_hz = 0.0
        self._rate_n = 0

    # ---- internals -------------------------------------------------------

    def _advance_clock(self, now, step_us):
        host_us = int(now * 1000000)
        if self._clock_us is None or step_us is None:
            self._clock_us = host_us
        else:
            self._clock_us += step_us
            if abs(self._clock_us - host_us) > CLOCK_SLACK_US:
                self._clock_us = host_us
        if self._clock_us <= self.timestamp_us:
            self._clock_us = self.timestamp_us + 1
        self.timestamp_us = self._clock_us

    def _store(self, accel, gyro, now, auto_calibrate):
        if self._manual is not None:
            self._manual.add(accel, gyro)
        if auto_calibrate and self._manual is None:
            self._auto_step(accel, gyro, now)

        cal = self.cal
        s = cal.accel_scale
        self.accel = (accel[0] * s, accel[1] * s, accel[2] * s)
        self.gyro = (
            gyro[0] - cal.bias[0],
            gyro[1] - cal.bias[1],
            gyro[2] - cal.bias[2],
        )
        self.last_sample_time = now
        self.sample_count += 1

        self._rate_n += 1
        dt = now - self._rate_t0
        if dt >= 1.0:
            self.rate_hz = self._rate_n / dt if self._rate_t0 else 0.0
            self._rate_t0 = now
            self._rate_n = 0

    def _auto_step(self, accel, gyro, now):
        w = self._auto
        if w.n == 0:
            w.reset(now)
        w.add(accel, gyro)
        if now - w.start < AUTO_WINDOW_S:
            return
        if (
            w.n >= AUTO_MIN_SAMPLES
            and w.p2p_gyro() < AUTO_GYRO_P2P_DPS
            and w.p2p_accel() < AUTO_ACCEL_P2P_G
        ):
            self._apply(w, strict=True)
        w.reset(now)

    def _apply(self, window, strict):
        m = window.mean()
        bias = (m[3], m[4], m[5])
        # A real gyro offset is small. Automatic calibration must never
        # mistake a slow, steady turn for sensor drift, so it only accepts
        # small offsets; a manual calibration may correct larger ones.
        limit = AUTO_BIAS_LIMIT_DPS if strict else BIAS_LIMIT_DPS
        if any(abs(b) > limit for b in bias):
            return False
        mag = math.sqrt(m[0] * m[0] + m[1] * m[1] + m[2] * m[2])
        if not (GRAVITY_SANE_MIN <= mag <= GRAVITY_SANE_MAX):
            # A resting sensor must see roughly 1 g. Anything else means
            # the data is not trustworthy enough to calibrate against.
            return False
        scale = self.cal.accel_scale
        wanted = 1.0 / mag
        if ACCEL_SCALE_MIN <= wanted <= ACCEL_SCALE_MAX:
            scale = wanted
        old = self.cal
        changed = (
            not old.calibrated
            or max(abs(bias[i] - old.bias[i]) for i in range(3)) > 0.03
            or abs(scale - old.accel_scale) > 0.003
        )
        if strict and not changed:
            return True
        self.cal = Calibration(bias, scale, True)
        self.cal_dirty = True
        return True

    # ---- manual calibration ------------------------------------------------

    def is_live(self, now):
        return self.last_sample_time > 0 and now - self.last_sample_time < LIVE_TIMEOUT_S

    def start_manual(self, now):
        self._manual = _Window()
        self._manual.reset(now)
        self._manual_deadline = now + MANUAL_WINDOW_S
        self.manual_result = None

    @property
    def calibrating(self):
        return self._manual is not None

    def poll_manual(self, now):
        """Finish a running manual calibration once its window elapsed."""
        w = self._manual
        if w is None or now < self._manual_deadline:
            return
        self._manual = None
        if w.n < MANUAL_MIN_SAMPLES:
            self.manual_result = "nodata"
        elif (
            w.p2p_gyro() > MANUAL_GYRO_P2P_DPS
            or w.p2p_accel() > MANUAL_ACCEL_P2P_G
            or not self._apply(w, strict=False)
        ):
            self.manual_result = "moved"
        else:
            self.manual_result = "ok"
        self.manual_serial += 1
        self._auto.n = 0

    def reset_calibration(self):
        self.cal = Calibration()
        self.cal_dirty = True
        self._auto.n = 0


class OutputOptions(object):
    """User tuning of one DSU slot."""

    __slots__ = (
        "sensitivity", "deadzone", "rotation",
        "invert_pitch", "invert_yaw", "invert_roll",
    )

    def __init__(self, d=None):
        d = d or {}
        self.sensitivity = float(d.get("sensitivity", 1.0))
        self.deadzone = float(d.get("deadzone", 0.0))
        self.rotation = int(d.get("rotation", 0))
        self.invert_pitch = bool(d.get("invert_pitch", False))
        self.invert_yaw = bool(d.get("invert_yaw", False))
        self.invert_roll = bool(d.get("invert_roll", False))


def process(sensor, opt):
    """Apply the slot tuning to a sensor's latest sample.

    Returns the DSU tuple (accX, accY, accZ, pitch, yaw, roll).
    """
    gx, gy, gz = sensor.gyro
    dz = opt.deadzone
    if dz > 0.0:
        mag = math.sqrt(gx * gx + gy * gy + gz * gz)
        if mag <= dz:
            gx = gy = gz = 0.0
        else:
            # Soft deadzone: no jump when leaving it
            k = (mag - dz) / mag
            gx *= k
            gy *= k
            gz *= k
    k = opt.sensitivity
    gyro = rotate((gx * k, gy * k, gz * k), opt.rotation)
    accel = rotate(sensor.accel, opt.rotation)
    ax, ay, az, pitch, yaw, roll = to_dsu(accel, gyro)
    if opt.invert_pitch:
        pitch = -pitch
    if opt.invert_yaw:
        yaw = -yaw
    if opt.invert_roll:
        roll = -roll
    return (ax, ay, az, pitch, yaw, roll)
