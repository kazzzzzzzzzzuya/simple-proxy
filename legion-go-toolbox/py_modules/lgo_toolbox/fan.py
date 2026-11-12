"""Custom fan curve through the Legion Go firmware.

The BIOS keeps a ten point fan table (one fan level for 10, 20 ... 100 C)
that it applies while the device is in its "custom" power mode. Writing
that table needs Lenovo's WMI method, i.e. the acpi_call kernel module.
The fan itself always stays under firmware control: this only changes the
table the firmware follows, and thermal protection keeps working.
"""

import os
import time

from . import acpi, sysio

TEMPS = (10, 20, 30, 40, 50, 60, 70, 80, 90, 100)
MAX_LEVEL = 115
FEATURE_FULL_SPEED = 0x04020000

MODE_CUSTOM = 0xFF
MODE_PERFORMANCE = 0x03

# The curve may never go below these levels (per temperature), so a
# mis-drawn curve cannot cook the device.
SAFETY_FLOOR = (0, 0, 0, 0, 0, 0, 30, 50, 70, 90)

PRESETS = {
    "silent":      (0, 0, 0, 0, 25, 35, 45, 60, 80, 100),
    "balanced":    (0, 0, 0, 25, 35, 45, 60, 75, 90, 100),
    "performance": (0, 0, 30, 40, 50, 65, 80, 95, 105, 115),
}
DEFAULT_CURVE = PRESETS["balanced"]


def sanitize_curve(points):
    """Clamp to the valid range, apply the safety floor and make the curve
    non-decreasing. Raises ValueError for unusable input."""
    if not isinstance(points, (list, tuple)) or len(points) != len(TEMPS):
        raise ValueError("A fan curve needs %d points" % len(TEMPS))
    out = []
    highest = 0
    for i, p in enumerate(points):
        if isinstance(p, bool) or not isinstance(p, (int, float)):
            raise ValueError("Fan curve points must be numbers")
        v = int(round(p))
        v = max(SAFETY_FLOOR[i], min(MAX_LEVEL, max(0, v)))
        highest = max(highest, v)
        out.append(highest)
    return tuple(out)


def curve_payload(points):
    payload = bytes([0x00, 0x00, 0x0A, 0x00, 0x00, 0x00])
    for p in points:
        payload += bytes([p, 0x00])
    payload += bytes([0x00, 0x0A, 0x00, 0x00, 0x00])
    for t in TEMPS:
        payload += bytes([t, 0x00])
    payload += bytes([0x00])
    return payload


def read_sensors(hwmon="/sys/class/hwmon"):
    """CPU temperature (C) and fan speed (RPM) where the kernel has them."""
    temp = None
    rpm = None
    try:
        names = sorted(os.listdir(hwmon))
    except OSError:
        names = []
    for name in names:
        base = os.path.join(hwmon, name)
        chip = sysio.read_text(os.path.join(base, "name"), "")
        if temp is None and chip in ("k10temp", "zenpower", "coretemp"):
            milli = sysio.read_int(os.path.join(base, "temp1_input"))
            if milli is not None:
                temp = round(milli / 1000.0)
        if rpm is None:
            value = sysio.read_int(os.path.join(base, "fan1_input"))
            if value is not None:
                rpm = value
    return {"temp": temp, "rpm": rpm}


class FanControl(object):
    def __init__(self, acpi_path=acpi.PATH, sleeper=time.sleep):
        self._path = acpi_path
        self._sleep = sleeper
        self.error = None

    def available(self):
        return acpi.available(self._path)

    def _try(self, fn, *args):
        try:
            fn(*args)
            self.error = None
            return True
        except acpi.AcpiError as e:
            self.error = str(e)
            return False

    def _mode(self):
        try:
            return acpi.call(acpi.WMAA, [0, 0x2D, 0], self._path)
        except acpi.AcpiError:
            return None

    def _set_mode(self, mode):
        return self._try(acpi.call, acpi.WMAA, [0, 0x2C, mode], self._path)

    def apply(self, points, full_speed=False, ensure_custom=True):
        """Activate a curve (or full speed). Returns True on success."""
        if not self.available():
            self.error = "The acpi_call kernel module is not installed"
            return False
        if full_speed:
            return self._try(acpi.set_feature, FEATURE_FULL_SPEED, 1, self._path)
        curve = sanitize_curve(points)
        if not self._try(acpi.set_feature, FEATURE_FULL_SPEED, 0, self._path):
            return False
        self._sleep(0.5)
        if ensure_custom and self._mode() != MODE_CUSTOM:
            if not self._set_mode(MODE_CUSTOM):
                return False
            self._sleep(0.3)
        return self._try(acpi.call, acpi.WMAB, [0, 0x06, curve_payload(curve)],
                         self._path)

    def restore(self):
        """Hand the fan table back to the firmware default."""
        if not self.available():
            return False
        if not self._try(acpi.set_feature, FEATURE_FULL_SPEED, 0, self._path):
            return False
        # Leaving and re-entering custom mode reloads the stock table.
        was_custom = self._mode() == MODE_CUSTOM
        if was_custom:
            if not self._set_mode(MODE_PERFORMANCE):
                return False
            self._sleep(0.5)
            return self._set_mode(MODE_CUSTOM)
        return True
