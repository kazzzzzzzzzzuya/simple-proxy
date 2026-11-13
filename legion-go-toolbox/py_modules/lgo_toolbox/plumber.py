"""Swap the menu buttons through InputPlumber.

SteamOS routes the Legion Go controllers through InputPlumber. Loading a
small device profile into it swaps the Steam (Legion L) and Quick Access
(Legion R) buttons with View and Menu. Two layouts are offered:

* ``steam_menu`` (default): Steam <-> Menu, Quick Access <-> View
* ``steam_view``:           Steam <-> View, Quick Access <-> Menu

The profile that was active before is kept as the base, so its own
mappings stay in effect.
"""

import os
import re

from . import sysio

DEST = "org.shadowblip.InputPlumber"
IFACE = "org.shadowblip.Input.CompositeDevice"
PATH_FMT = "/org/shadowblip/InputPlumber/CompositeDevice%d"
FALLBACK_BASE = "/usr/share/inputplumber/profiles/default.yaml"

# InputPlumber button names: Guide = Steam (Legion L), QuickAccess = Quick
# Access (Legion R), Select = View, Start = Menu.
LAYOUTS = {
    "steam_menu": (
        ("Start", "Guide"),
        ("Guide", "Start"),
        ("Select", "QuickAccess"),
        ("QuickAccess", "Select"),
    ),
    "steam_view": (
        ("Select", "Guide"),
        ("Guide", "Select"),
        ("Start", "QuickAccess"),
        ("QuickAccess", "Start"),
    ),
}
DEFAULT_LAYOUT = "steam_menu"
PROFILE_NAMES = {
    "steam_menu": "Legion Go Toolbox - Steam/Menu and QAM/View swapped",
    "steam_view": "Legion Go Toolbox - Steam/View and QAM/Menu swapped",
}
OUR_PROFILES = frozenset(PROFILE_NAMES.values()) | {
    "Legion Go Toolbox - swapped menu buttons",       # version 2.0 / 2.1
}
_SWAPPED_BUTTONS = frozenset(src for pairs in LAYOUTS.values() for src, _ in pairs)


def _mapping_entries(layout):
    out = []
    for src, dst in LAYOUTS[layout]:
        out.append(
            "  - name: %s to %s\n"
            "    source_event:\n"
            "      gamepad:\n"
            "        button: %s\n"
            "    target_events:\n"
            "      - gamepad:\n"
            "          button: %s\n" % (src, dst, src, dst))
    return "".join(out)


def build_profile(base_text=None, layout=DEFAULT_LAYOUT):
    """Profile YAML with the four swaps, on top of the base profile's own
    mappings when the base has the usual shape."""
    kept = ""
    if base_text:
        kept = _base_mappings(base_text)
    return (
        "version: 1\n"
        "kind: DeviceProfile\n"
        "name: %s\n"
        "mapping:\n%s%s" % (PROFILE_NAMES[layout], kept, _mapping_entries(layout)))


def _base_mappings(text):
    """The entries of the base profile's top-level ``mapping:`` list, minus
    any that use one of the four swapped buttons as their source. Returns
    '' if the file does not look like a plain profile."""
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(r"^mapping:\s*$", line):
            start = i + 1
            break
    if start is None:
        return ""
    body = []
    for line in lines[start:]:
        if line.strip() == "" or line.lstrip().startswith("#"):
            continue
        if not line.startswith(" "):
            break               # next top-level key
        body.append(line)
    if not body or not body[0].startswith("  - "):
        return ""
    entries = []
    for line in body:
        if line.startswith("  - "):
            entries.append([line])
        elif line.startswith("    ") and entries:
            entries[-1].append(line)
        else:
            return ""           # unexpected layout: do not guess
    swapped = _SWAPPED_BUTTONS
    kept = []
    for entry in entries:
        block = "\n".join(entry)
        m = re.search(r"source_event:\s*\n\s+gamepad:\s*\n\s+button:\s*(\S+)", block)
        if m and m.group(1) in swapped:
            continue
        kept.append(block + "\n")
    return "".join(kept)


class MenuSwap(object):
    def __init__(self, runner=sysio.run, state_file=None):
        self._run = runner
        self._state_file = state_file
        self._device = None
        self.error = None

    # ---- dbus ----------------------------------------------------------
    def _get(self, path, prop):
        rc, out, _ = self._run(["busctl", "get-property", DEST, path, IFACE, prop])
        if rc != 0:
            return None
        out = out.strip()
        m = re.match(r'^s "(.*)"$', out)
        return m.group(1) if m else ""

    def _find_device(self):
        for idx in range(4):
            path = PATH_FMT % idx
            if self._get(path, "ProfileName") is not None:
                self._device = path
                return path
        self._device = None
        return None

    def available(self):
        return self._find_device() is not None

    def is_active(self, layout=None):
        """True if our profile (for `layout`, or any layout) is loaded,
        None if InputPlumber is not running."""
        path = self._find_device()
        if not path:
            return None
        name = self._get(path, "ProfileName")
        if layout is None:
            return name in OUR_PROFILES
        return name == PROFILE_NAMES[layout]

    # ---- base profile bookkeeping ---------------------------------------
    def _remember_base(self, path):
        if self._state_file and path:
            sysio.write_text(self._state_file, path)

    def _base_path(self):
        saved = sysio.read_text(self._state_file) if self._state_file else None
        for cand in (saved, FALLBACK_BASE):
            if cand and os.path.isfile(cand):
                return cand
        return None

    def _load(self, device, text):
        rc, _, err = self._run(
            ["busctl", "call", DEST, device, IFACE, "LoadProfileFromYaml", "s", text])
        if rc != 0:
            self.error = (err.strip() or "InputPlumber refused the profile")
            return False
        self.error = None
        return True

    # ---- actions ----------------------------------------------------------
    def apply(self, enabled, layout=DEFAULT_LAYOUT):
        """Make InputPlumber match the wanted state. True on success."""
        if layout not in LAYOUTS:
            layout = DEFAULT_LAYOUT
        device = self._find_device()
        if not device:
            self.error = "InputPlumber is not running"
            return False
        name = self._get(device, "ProfileName")
        active = name in OUR_PROFILES
        if enabled:
            if name == PROFILE_NAMES[layout]:
                self.error = None
                return True
            current = self._get(device, "ProfilePath")
            if not active and current and os.path.isfile(current):
                self._remember_base(current)
            base = self._base_path()
            base_text = sysio.read_text(base) if base else None
            return self._load(device, build_profile(base_text, layout))
        if not active:
            self.error = None
            return True
        base = self._base_path()
        if base:
            text = sysio.read_text(base)
            if text:
                return self._load(device, text)
        # No base profile to return to: an empty profile removes the swap.
        return self._load(device, "version: 1\nkind: DeviceProfile\nname: Default\nmapping: []\n")


# ---------------------------------------------------------------------------
# Controller type presented to Steam
# ---------------------------------------------------------------------------

MANAGER_PATH = "/org/shadowblip/InputPlumber/Manager"
MANAGER_IFACE = "org.shadowblip.InputManager"

# What InputPlumber's own Legion Go configuration creates: an Xbox Elite pad.
XBOX_TARGETS = ("xbox-elite", "mouse", "keyboard", "touchpad")
# Controller types Steam gives gyro to.
GYRO_TARGETS = ("deck-uhid", "ds5-edge", "ds5")


class SteamController(object):
    """Chooses which controller InputPlumber emulates.

    The default (an Xbox pad) has no motion sensor as far as Steam is
    concerned. Emulating a Steam Deck or DualSense controller instead makes
    InputPlumber pass the tablet's motion sensor on, and Steam Input then
    offers its usual gyro options in every game.
    """

    def __init__(self, runner=sysio.run, state_file="/tmp/.lgo-toolbox-steam-controller"):
        self._run = runner
        self._state_file = state_file
        self._applied = None        # (kind, InputPlumber start stamp)
        self.error = None
        # Remember across plugin reloads what InputPlumber was last told
        # (the file lives in /tmp, so a reboot starts clean like it does).
        saved = sysio.read_text(state_file) if state_file else None
        if saved and "\n" in saved:
            kind, stamp = saved.split("\n", 1)
            if kind == "default":       # name used by version 2.1
                kind = "xbox"
            self._applied = (kind, stamp)

    def _device(self):
        for idx in range(4):
            path = PATH_FMT % idx
            rc, _, _ = self._run(["busctl", "get-property", DEST, path, IFACE, "ProfileName"])
            if rc == 0:
                return path
        return None

    def supported(self):
        """Gyro-capable controller types this InputPlumber can emulate."""
        rc, out, _ = self._run(["busctl", "get-property", DEST, MANAGER_PATH,
                                MANAGER_IFACE, "SupportedTargetDeviceIds"])
        if rc != 0 or not out.strip().startswith("as "):
            # Older InputPlumber without the list: these three are long-standing.
            return list(GYRO_TARGETS) if self._device() else []
        ids = [p.strip('"') for p in out.split()[2:]]
        return [t for t in GYRO_TARGETS if t in ids]

    def _stamp(self):
        """Changes whenever the InputPlumber service is (re)started."""
        rc, out, _ = self._run(["systemctl", "show", "inputplumber", "-p",
                                "ActiveEnterTimestampMonotonic", "--value"])
        return out.strip() if rc == 0 else ""

    def needs_apply(self, kind):
        """True if InputPlumber is not (or no longer) emulating `kind`."""
        if self._applied is None:
            return kind != "xbox"      # InputPlumber starts with its Xbox pad
        return self._applied != (kind, self._stamp())

    def apply(self, kind):
        device = self._device()
        if not device:
            self.error = "InputPlumber is not running"
            return False
        if kind == "xbox":
            targets = list(XBOX_TARGETS)
        elif kind in GYRO_TARGETS:
            targets = [kind, "keyboard", "mouse"]
        else:
            self.error = "Unknown controller type"
            return False
        rc, _, err = self._run(["busctl", "call", DEST, device, IFACE, "SetTargetDevices",
                                "as", str(len(targets))] + targets)
        if rc != 0:
            self.error = err.strip() or "InputPlumber refused the controller type"
            return False
        self.error = None
        self._applied = (kind, self._stamp())
        if self._state_file:
            sysio.write_text(self._state_file, "%s\n%s" % self._applied)
        return True
