"""Settings schema of the hardware modules (everything except the gyro,
which keeps its own file)."""

import copy
import json
import os

from . import controller, fan

POWER_PROFILE_DEFAULT = {
    "mode": "custom",      # "custom" or one of the device's power modes
    "tdp": 15,
    "auto": False,         # lowest TDP that holds target_fps
    "target_fps": 60,
    "min_tdp": 8,
}
SETTINGS_VERSION = 2
# Controller InputPlumber presents to Steam. "xbox" is InputPlumber's own
# choice (no gyro in Steam); the default here is the Steam Deck controller.
STEAM_CONTROLLERS = ("deck-uhid", "xbox", "ds5-edge", "ds5")
DEFAULT_STEAM_CONTROLLER = "deck-uhid"
SWAP_LAYOUTS = ("steam_menu", "steam_view")
TARGET_FPS_CHOICES = (30, 40, 45, 48, 50, 60, 72, 90, 120, 144)
TDP_ABS_MIN = 4
TDP_ABS_MAX = 40


def defaults():
    return {
        "version": SETTINGS_VERSION,
        "lighting": {
            "sticks": {
                "managed": False,
                "on": True,
                "mode": "solid",
                "color": [0, 140, 255],
                "brightness": 0.8,
                "speed": 0.5,
            },
            "power_led": {"managed": False, "on": True, "off_in_sleep": False},
        },
        "battery": {"charge_limit": None},
        "fan": {
            "enabled": False,
            "full_speed": False,
            "curve": list(fan.DEFAULT_CURVE),
        },
        "power": {
            "enabled": False,
            "per_game": False,
            "cpu_boost": None,
            "profiles": {"default": dict(POWER_PROFILE_DEFAULT)},
        },
        "controls": {
            "swap_menu": False,
            "swap_layout": SWAP_LAYOUTS[0],
            "steam_controller": DEFAULT_STEAM_CONTROLLER,
            # Per game: {appid: {"type": ..., "name": ...}}
            "steam_controller_games": {},
            "remap_managed": False,
            "remap": {name: "none" for name in controller.REMAP_BUTTONS},
            "touchpad": None,
        },
    }


def _flag(value, fallback):
    return value if isinstance(value, bool) else fallback


def _opt_flag(value, fallback):
    return value if value is None or isinstance(value, bool) else fallback


def _num(value, fallback, lo, hi):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value:
        return fallback
    return max(lo, min(hi, value))


def sanitize_profile(src, base=None):
    out = dict(base or POWER_PROFILE_DEFAULT)
    if not isinstance(src, dict):
        return out
    mode = src.get("mode")
    if isinstance(mode, str) and 0 < len(mode) <= 32 and mode.replace("-", "").replace("_", "").isalnum():
        out["mode"] = mode
    out["tdp"] = int(round(_num(src.get("tdp"), out["tdp"], TDP_ABS_MIN, TDP_ABS_MAX)))
    out["min_tdp"] = int(round(_num(src.get("min_tdp"), out["min_tdp"], TDP_ABS_MIN, TDP_ABS_MAX)))
    out["auto"] = _flag(src.get("auto"), out["auto"])
    out["target_fps"] = int(round(_num(src.get("target_fps"), out["target_fps"], 15, 240)))
    return out


def sanitize(data):
    out = defaults()
    if not isinstance(data, dict):
        return out
    version = data.get("version")
    old = not isinstance(version, int) or isinstance(version, bool) or version < 2

    light = data.get("lighting")
    if isinstance(light, dict):
        s, d = light.get("sticks"), out["lighting"]["sticks"]
        if isinstance(s, dict):
            d["managed"] = _flag(s.get("managed"), d["managed"])
            d["on"] = _flag(s.get("on"), d["on"])
            if s.get("mode") in controller.RGB_MODES:
                d["mode"] = s["mode"]
            color = s.get("color")
            if (isinstance(color, (list, tuple)) and len(color) == 3
                    and all(isinstance(c, (int, float)) and not isinstance(c, bool)
                            for c in color)):
                d["color"] = [int(max(0, min(255, c))) for c in color]
            d["brightness"] = round(float(_num(s.get("brightness"), d["brightness"], 0.0, 1.0)), 3)
            d["speed"] = round(float(_num(s.get("speed"), d["speed"], 0.0, 1.0)), 3)
        p, d = light.get("power_led"), out["lighting"]["power_led"]
        if isinstance(p, dict):
            for key in ("managed", "on", "off_in_sleep"):
                d[key] = _flag(p.get(key), d[key])

    batt = data.get("battery")
    if isinstance(batt, dict):
        out["battery"]["charge_limit"] = _opt_flag(batt.get("charge_limit"), None)

    f = data.get("fan")
    if isinstance(f, dict):
        out["fan"]["enabled"] = _flag(f.get("enabled"), False)
        out["fan"]["full_speed"] = _flag(f.get("full_speed"), False)
        try:
            out["fan"]["curve"] = list(fan.sanitize_curve(f.get("curve")))
        except ValueError:
            pass

    p = data.get("power")
    if isinstance(p, dict):
        d = out["power"]
        d["enabled"] = _flag(p.get("enabled"), False)
        d["per_game"] = _flag(p.get("per_game"), False)
        d["cpu_boost"] = _opt_flag(p.get("cpu_boost"), None)
        profiles = p.get("profiles")
        if isinstance(profiles, dict):
            d["profiles"]["default"] = sanitize_profile(profiles.get("default"))
            for key, value in list(profiles.items())[:500]:
                if key != "default" and isinstance(key, str) and key.isdigit() and len(key) <= 12:
                    d["profiles"][key] = sanitize_profile(value, d["profiles"]["default"])

    c = data.get("controls")
    if isinstance(c, dict):
        d = out["controls"]
        d["swap_menu"] = _flag(c.get("swap_menu"), False)
        if c.get("swap_layout") in SWAP_LAYOUTS:
            d["swap_layout"] = c["swap_layout"]
        steam = c.get("steam_controller")
        if steam == "default":
            # Version 1 called the Xbox pad "default" and used it unless the
            # user picked another type. Version 2 defaults to the Steam Deck
            # controller, so an untouched old setting moves to that.
            steam = DEFAULT_STEAM_CONTROLLER if old else "xbox"
        if steam in STEAM_CONTROLLERS:
            d["steam_controller"] = steam
        games = c.get("steam_controller_games")
        if isinstance(games, dict):
            for key, value in list(games.items())[:500]:
                if not (isinstance(key, str) and key.isdigit() and len(key) <= 12):
                    continue
                if not isinstance(value, dict) or value.get("type") not in STEAM_CONTROLLERS:
                    continue
                name = value.get("name")
                d["steam_controller_games"][key] = {
                    "type": value["type"],
                    "name": name[:120] if isinstance(name, str) else "",
                }
        d["remap_managed"] = _flag(c.get("remap_managed"), False)
        d["touchpad"] = _opt_flag(c.get("touchpad"), None)
        remap = c.get("remap")
        if isinstance(remap, dict):
            for name in controller.REMAP_BUTTONS:
                if remap.get(name) in controller.REMAP_ACTIONS:
                    d["remap"][name] = remap[name]
    return out


def merge(current, patch):
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
    return sanitize(_merge(copy.deepcopy(current), patch))


def load(path):
    try:
        with open(path, "r") as f:
            return sanitize(json.load(f))
    except (OSError, IOError, ValueError):
        return defaults()


def save(path, data):
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
