"""Settings schema, validation and persistence."""

import copy
import json
import os

from .motion import ROTATIONS

DEFAULT_PORT = 26760
PORT_CHOICES = (26760, 26761, 26762, 26763)
SOURCES = ("auto", "right", "left", "tablet")
SLOT_NAMES = ("combined", "left", "right")
SLOT_INDEX = {"combined": 0, "left": 1, "right": 2}
SENSOR_NAMES = ("left", "right", "tablet")

SENSITIVITY_MIN = 0.1
SENSITIVITY_MAX = 5.0
DEADZONE_MAX = 10.0

_OUTPUT_DEFAULTS = {
    "enabled": True,
    "sensitivity": 1.0,
    "deadzone": 0.0,
    "rotation": 0,
    "invert_pitch": False,
    "invert_yaw": False,
    "invert_roll": False,
}

_CAL_DEFAULT = {"bias": [0.0, 0.0, 0.0], "accel_scale": 1.0, "calibrated": False}


def defaults():
    slots = {name: dict(_OUTPUT_DEFAULTS) for name in SLOT_NAMES}
    slots["combined"]["source"] = "auto"
    return {
        "version": 1,
        "enabled": True,
        # UI only: False shows the simple panel, True all options.
        "advanced": False,
        "port": DEFAULT_PORT,
        "lan": False,
        "auto_calibrate": True,
        "slots": slots,
        "calibration": {name: copy.deepcopy(_CAL_DEFAULT) for name in SENSOR_NAMES},
    }


def _num(value, fallback, lo, hi):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return fallback
    if value != value:  # NaN
        return fallback
    return max(lo, min(hi, float(value)))


def _flag(value, fallback):
    return value if isinstance(value, bool) else fallback


def sanitize(data):
    """Return a complete, valid settings dict built from untrusted input."""
    out = defaults()
    if not isinstance(data, dict):
        return out

    out["enabled"] = _flag(data.get("enabled"), out["enabled"])
    out["advanced"] = _flag(data.get("advanced"), out["advanced"])
    out["lan"] = _flag(data.get("lan"), out["lan"])
    out["auto_calibrate"] = _flag(data.get("auto_calibrate"), out["auto_calibrate"])
    port = data.get("port")
    if isinstance(port, int) and not isinstance(port, bool) and 1024 <= port <= 65535:
        out["port"] = port

    slots = data.get("slots")
    if isinstance(slots, dict):
        for name in SLOT_NAMES:
            src = slots.get(name)
            if not isinstance(src, dict):
                continue
            dst = out["slots"][name]
            dst["enabled"] = _flag(src.get("enabled"), dst["enabled"])
            dst["sensitivity"] = round(_num(
                src.get("sensitivity"), dst["sensitivity"],
                SENSITIVITY_MIN, SENSITIVITY_MAX), 3)
            dst["deadzone"] = round(_num(
                src.get("deadzone"), dst["deadzone"], 0.0, DEADZONE_MAX), 3)
            if src.get("rotation") in ROTATIONS and not isinstance(
                    src.get("rotation"), bool):
                dst["rotation"] = int(src["rotation"])
            for key in ("invert_pitch", "invert_yaw", "invert_roll"):
                dst[key] = _flag(src.get(key), dst[key])
            if name == "combined" and src.get("source") in SOURCES:
                dst["source"] = src["source"]

    cal = data.get("calibration")
    if isinstance(cal, dict):
        from .motion import Calibration
        for name in SENSOR_NAMES:
            if isinstance(cal.get(name), dict):
                out["calibration"][name] = Calibration.from_dict(cal[name]).to_dict()
    return out


def merge(current, patch):
    """Deep-merge a partial settings dict into the current settings."""
    def _merge(a, b):
        res = dict(a)
        for k, v in b.items():
            if isinstance(v, dict) and isinstance(res.get(k), dict):
                res[k] = _merge(res[k], v)
            else:
                res[k] = v
        return res

    if not isinstance(patch, dict):
        return sanitize(current)
    return sanitize(_merge(current, patch))


def load(path):
    try:
        with open(path, "r") as f:
            return sanitize(json.load(f))
    except (OSError, IOError, ValueError):
        return defaults()


def save(path, data):
    """Atomic write. Returns True on success."""
    try:
        folder = os.path.dirname(path)
        if folder and not os.path.isdir(folder):
            os.makedirs(folder)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except (OSError, IOError):
        return False
