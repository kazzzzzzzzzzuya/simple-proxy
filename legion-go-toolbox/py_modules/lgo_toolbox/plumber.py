"""Swap the menu buttons through InputPlumber.

SteamOS routes the Legion Go controllers through InputPlumber. Loading a
small device profile into it swaps View <-> Legion L (Steam) and
Menu <-> Legion R (Quick Access). The profile that was active before is
kept as the base, so its own mappings stay in effect.
"""

import os
import re

from . import sysio

DEST = "org.shadowblip.InputPlumber"
IFACE = "org.shadowblip.Input.CompositeDevice"
PATH_FMT = "/org/shadowblip/InputPlumber/CompositeDevice%d"
PROFILE_NAME = "Legion Go Toolbox - swapped menu buttons"
FALLBACK_BASE = "/usr/share/inputplumber/profiles/default.yaml"

_SWAPS = (
    ("Select", "Guide"),
    ("Guide", "Select"),
    ("Start", "QuickAccess"),
    ("QuickAccess", "Start"),
)


def _mapping_entries():
    out = []
    for src, dst in _SWAPS:
        out.append(
            "  - name: %s to %s\n"
            "    source_event:\n"
            "      gamepad:\n"
            "        button: %s\n"
            "    target_events:\n"
            "      - gamepad:\n"
            "          button: %s\n" % (src, dst, src, dst))
    return "".join(out)


def build_profile(base_text=None):
    """Profile YAML with the four swaps, on top of the base profile's own
    mappings when the base has the usual shape."""
    kept = ""
    if base_text:
        kept = _base_mappings(base_text)
    return (
        "version: 1\n"
        "kind: DeviceProfile\n"
        "name: %s\n"
        "mapping:\n%s%s" % (PROFILE_NAME, kept, _mapping_entries()))


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
    swapped = {s for s, _ in _SWAPS}
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

    def is_active(self):
        path = self._find_device()
        if not path:
            return None
        return self._get(path, "ProfileName") == PROFILE_NAME

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
    def apply(self, enabled):
        """Make InputPlumber match the wanted state. True on success."""
        device = self._find_device()
        if not device:
            self.error = "InputPlumber is not running"
            return False
        active = self._get(device, "ProfileName") == PROFILE_NAME
        if enabled:
            if active:
                self.error = None
                return True
            current = self._get(device, "ProfilePath")
            if current and os.path.isfile(current):
                self._remember_base(current)
            base = self._base_path()
            base_text = sysio.read_text(base) if base else None
            return self._load(device, build_profile(base_text))
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
