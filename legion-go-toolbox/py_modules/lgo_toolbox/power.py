"""Power limits (TDP) and automatic TDP for a target frame rate.

SteamOS exposes the Legion Go's firmware power limits through two kernel
interfaces (the lenovo-wmi drivers):

* a platform profile (quiet / balanced / performance / custom)
* three limits that apply in the custom profile: sustained (SPL),
  slow (SPPT) and fast (FPPT) package power, each with its own range

Nothing here talks to the CPU directly; the firmware enforces the limits.
"""

import os
import subprocess
import sys
import threading
import time

from . import sysio

FW_ATTR_ROOT = "/sys/class/firmware-attributes"
PROFILE_CLASS_ROOT = "/sys/class/platform-profile"
LEGACY_PROFILE = "/sys/firmware/acpi/platform_profile"

ATTR_SPL = "ppt_pl1_spl"
ATTR_SPPT = "ppt_pl2_sppt"
ATTR_FPPT = "ppt_pl3_fppt"
ATTRS = (ATTR_FPPT, ATTR_SPPT, ATTR_SPL)

CUSTOM = "custom"
WRITE_GAP_S = 0.3


class PowerLimits(object):
    """Reads and writes the firmware power limits."""

    def __init__(self, fw_root=FW_ATTR_ROOT, profile_root=PROFILE_CLASS_ROOT,
                 legacy_profile=LEGACY_PROFILE, sleeper=time.sleep):
        self._fw_root = fw_root
        self._profile_root = profile_root
        self._legacy = legacy_profile
        self._sleep = sleeper
        self._lock = threading.Lock()
        self.error = None

    # ---- discovery -----------------------------------------------------
    def _attr_dir(self):
        try:
            names = sorted(os.listdir(self._fw_root))
        except OSError:
            return None
        for name in names:
            if not name.startswith("lenovo-wmi-other"):
                continue
            base = os.path.join(self._fw_root, name, "attributes")
            if all(os.path.exists(os.path.join(base, a, "current_value")) for a in ATTRS):
                return base
        return None

    def _profile_paths(self):
        """(profile file, choices file) or (None, None)."""
        try:
            names = sorted(os.listdir(self._profile_root))
        except OSError:
            names = []
        for name in names:
            base = os.path.join(self._profile_root, name)
            if sysio.read_text(os.path.join(base, "name")) == "lenovo-wmi-gamezone":
                return os.path.join(base, "profile"), os.path.join(base, "choices")
        if os.path.exists(self._legacy):
            return self._legacy, self._legacy + "_choices"
        return None, None

    def available(self):
        return self._attr_dir() is not None and self._profile_paths()[0] is not None

    def unavailable_reason(self):
        if self._profile_paths()[0] is None:
            return "This SteamOS version has no Legion power-mode driver"
        if self._attr_dir() is None:
            return "This SteamOS version does not expose the firmware power limits"
        return None

    # ---- profile ---------------------------------------------------------
    def profiles(self):
        _, choices = self._profile_paths()
        raw = sysio.read_text(choices, "") if choices else ""
        return raw.split()

    def profile(self):
        path, _ = self._profile_paths()
        return sysio.read_text(path) if path else None

    def set_profile(self, name):
        path, _ = self._profile_paths()
        if not path:
            self.error = self.unavailable_reason()
            return False
        if name not in self.profiles():
            self.error = "Power mode '%s' is not offered by this device" % name
            return False
        with self._lock:
            for attempt in range(3):
                err = sysio.write_text(path, name)
                if not err and sysio.read_text(path) == name:
                    self.error = None
                    return True
                self._sleep(0.3 * (attempt + 1))
        self.error = "Could not switch the power mode" + (": " + err if err else "")
        return False

    # ---- limits ------------------------------------------------------------
    def _range(self, base, attr):
        lo = sysio.read_int(os.path.join(base, attr, "min_value"))
        hi = sysio.read_int(os.path.join(base, attr, "max_value"))
        return lo, hi

    def tdp_range(self):
        """(min, max) watts of the sustained limit, or None."""
        base = self._attr_dir()
        if not base:
            return None
        lo, hi = self._range(base, ATTR_SPL)
        if lo is None or hi is None or lo <= 0 or hi < lo:
            return None
        return lo, hi

    def tdp(self):
        base = self._attr_dir()
        if not base:
            return None
        return sysio.read_int(os.path.join(base, ATTR_SPL, "current_value"))

    def set_tdp(self, watts):
        """Switch to the custom mode and set all three limits to `watts`
        (each clamped to its own range). Returns True if it took effect."""
        base = self._attr_dir()
        if not base:
            self.error = self.unavailable_reason()
            return False
        watts = int(round(watts))
        if self.profile() != CUSTOM and not self.set_profile(CUSTOM):
            return False
        with self._lock:
            first = True
            for attr in ATTRS:
                lo, hi = self._range(base, attr)
                value = watts
                if lo is not None:
                    value = max(lo, value)
                if hi is not None:
                    value = min(hi, value)
                path = os.path.join(base, attr, "current_value")
                if sysio.read_int(path) == value:
                    continue
                if not first:
                    self._sleep(WRITE_GAP_S)
                first = False
                err = sysio.write_text(path, value)
                if err:
                    self.error = "Could not set the power limit: " + err
                    return False
            self.error = None
            return True


def set_cpu_boost(enabled, cpu_root="/sys/devices/system/cpu"):
    """Turbo boost on/off. Returns True if a switch was found and set."""
    value = "1" if enabled else "0"
    paths = []
    global_switch = os.path.join(cpu_root, "cpufreq", "boost")
    if os.path.exists(global_switch):
        paths.append(global_switch)
    else:
        try:
            for name in sorted(os.listdir(cpu_root)):
                p = os.path.join(cpu_root, name, "cpufreq", "boost")
                if name.startswith("cpu") and name[3:].isdigit() and os.path.exists(p):
                    paths.append(p)
        except OSError:
            pass
    ok = False
    for p in paths:
        if sysio.write_text(p, value) is None:
            ok = True
    return ok


def cpu_boost_state(cpu_root="/sys/devices/system/cpu"):
    """True / False, or None if the kernel has no boost switch."""
    for p in (os.path.join(cpu_root, "cpufreq", "boost"),
              os.path.join(cpu_root, "cpu0", "cpufreq", "boost")):
        raw = sysio.read_text(p)
        if raw in ("0", "1"):
            return raw == "1"
    return None


# ---------------------------------------------------------------------------
# Frame rate source
# ---------------------------------------------------------------------------

class FpsMonitor(object):
    """Runs fps_probe.py with the system Python and keeps its last value."""

    STALE_S = 2.5

    def __init__(self, python=None, script=None, logger=None):
        self._python = python
        self._script = script or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "fps_probe.py")
        self._log = logger
        self._proc = None
        self._thread = None
        self._app = None
        self._lock = threading.Lock()
        self._fps = None
        self._at = 0.0
        self.error = None

    @staticmethod
    def find_python():
        for cand in ("/usr/bin/python3", "/usr/bin/python"):
            if os.path.isfile(cand) and os.access(cand, os.X_OK):
                return cand
        return None

    def set_app(self, app_id):
        """Follow this app (None stops measuring)."""
        if app_id == self._app and (app_id is None or self._alive()):
            return
        self.stop()
        self._app = app_id
        if app_id is None:
            return
        python = self._python or self.find_python()
        if not python:
            self.error = "No system Python to measure the frame rate with"
            return
        try:
            self._proc = subprocess.Popen(
                [python, "-u", self._script, str(int(app_id))],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL, env=sysio.clean_env(),
                close_fds=True)
        except OSError as e:
            self.error = "Could not start the frame rate probe: %s" % e
            self._proc = None
            return
        self.error = None
        self._thread = threading.Thread(
            target=self._reader, args=(self._proc,), name="lgo-fps", daemon=True)
        self._thread.start()

    def _alive(self):
        return self._proc is not None and self._proc.poll() is None

    def _reader(self, proc):
        try:
            for raw in proc.stdout:
                parts = raw.decode("ascii", "replace").split()
                if len(parts) not in (2, 3):
                    continue
                try:
                    low = None if len(parts) < 3 or parts[2] == "-" else float(parts[2])
                except ValueError:
                    low = None
                try:
                    fps = None if parts[0] == "-" else float(parts[0])
                except ValueError:
                    continue
                with self._lock:
                    if proc is self._proc:
                        self._fps = fps
                        self._low = low
                        self._at = time.monotonic()
        except (OSError, ValueError):
            pass

    def get(self):
        """Current fps, or None when there is no fresh measurement."""
        with self._lock:
            if self._fps is None or time.monotonic() - self._at > self.STALE_S:
                return None
            return self._fps

    def get_low(self):
        """Frame rate of the slowest frame in the last half second (the
        stutter measure), or None."""
        with self._lock:
            if self._fps is None or time.monotonic() - self._at > self.STALE_S:
                return None
            return getattr(self, "_low", None)

    def stop(self):
        proc = self._proc
        self._proc = None
        self._app = None
        with self._lock:
            self._fps = None
            self._low = None
        if proc is not None:
            try:
                proc.terminate()
                try:
                    proc.wait(1.0)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(1.0)
            except OSError:
                pass
            try:
                proc.stdout.close()
            except (OSError, ValueError):
                pass


# ---------------------------------------------------------------------------
# Automatic TDP
# ---------------------------------------------------------------------------

class AutoTdp(object):
    """Finds the lowest TDP that still holds a target frame rate.

    Call ``step(fps, now)`` about once a second. It returns the TDP to use.

    * Below target -> raise right away (bigger steps when far below).
    * At target for a while -> try one watt less.
    * If that lower setting cannot hold the target, go back up and leave
      that setting alone for a while (longer each time it fails), so the
      controller settles instead of bouncing.
    """

    OK_RATIO = 0.97          # at or above this share of the target is "holding"
    LOW_RATIO = 0.90         # below this is "far below"
    SETTLE_S = 2.5           # ignore readings right after a change
    HOLD_S = 6.0             # must hold this long before trying lower
    AVOID_S = 45.0           # first time a level fails
    AVOID_MAX_S = 600.0

    def __init__(self, tdp_min, tdp_max, target_fps, start=None):
        self.tdp_min = int(tdp_min)
        self.tdp_max = int(max(tdp_max, tdp_min))
        self.target = float(target_fps)
        self.tdp = int(min(self.tdp_max, max(self.tdp_min, start if start else self.tdp_max)))
        self._changed_at = None
        self._good_since = None
        self._probing_from = None       # tdp we came down from
        self._avoid = {}                # tdp -> (until, penalty seconds)
        self.state = "starting"

    def retarget(self, tdp_min, tdp_max, target_fps):
        self.tdp_min = int(tdp_min)
        self.tdp_max = int(max(tdp_max, tdp_min))
        if float(target_fps) != self.target:
            self.target = float(target_fps)
            self._avoid.clear()
            self._good_since = None
        self.tdp = int(min(self.tdp_max, max(self.tdp_min, self.tdp)))

    def _set(self, tdp, now):
        tdp = int(min(self.tdp_max, max(self.tdp_min, tdp)))
        if tdp != self.tdp:
            self.tdp = tdp
            self._changed_at = now
            self._good_since = None
        return self.tdp

    SMOOTH_RATIO = 0.85      # slowest frame at least this share of the target
    ROUGH_WINDOWS = 2        # this many rough readings in a row add power

    def _rough(self, low):
        if low is None or self.target <= 0:
            return False
        return low / self.target < self.SMOOTH_RATIO

    def step(self, fps, now, low=None):
        """`low` is the frame rate of the slowest frame in the reading.
        With it, a setting only counts as holding the target while frames
        also arrive evenly, so power is never cut into stutter."""
        rough = self._rough(low)
        self._rough_run = (getattr(self, "_rough_run", 0) + 1) if rough else 0
        if fps is None:
            # No measurement (menu, loading, paused): keep what we have.
            self._good_since = None
            self.state = "waiting"
            return self.tdp
        if self._changed_at is not None and now - self._changed_at < self.SETTLE_S:
            self.state = "settling"
            return self.tdp

        ratio = fps / self.target if self.target > 0 else 1.0
        if ratio >= self.OK_RATIO and rough and self._rough_run < self.ROUGH_WINDOWS:
            # One uneven reading (a hitch): do not lower, do not react yet.
            self._good_since = None
            self.state = "holding"
            return self.tdp
        if ratio < self.OK_RATIO or rough:
            if self._probing_from is not None and self.tdp < self._probing_from:
                # The level we just tried is too low: remember that and go
                # straight back to the one that worked.
                _, penalty = self._avoid.get(self.tdp, (0.0, self.AVOID_S / 2))
                penalty = min(self.AVOID_MAX_S, penalty * 2)
                self._avoid[self.tdp] = (now + penalty, penalty)
                back = self._probing_from
                self._probing_from = None
                self.state = "raising"
                return self._set(back, now)
            self._probing_from = None
            if self.tdp >= self.tdp_max:
                self.state = "at_max"
                self._good_since = None
                return self.tdp
            self.state = "raising"
            self._rough_run = 0
            return self._set(self.tdp + (3 if ratio < self.LOW_RATIO else 1), now)

        # Holding the target
        if self._good_since is None:
            self._good_since = now
        self._probing_from = None
        if self.tdp <= self.tdp_min:
            self.state = "at_min"
            return self.tdp
        if now - self._good_since < self.HOLD_S:
            self.state = "holding"
            return self.tdp
        lower = self.tdp - 1
        until, _ = self._avoid.get(lower, (0.0, 0.0))
        if now < until:
            self.state = "optimal"
            return self.tdp
        self._probing_from = self.tdp
        self.state = "lowering"
        return self._set(lower, now)


if __name__ == "__main__":   # pragma: no cover
    sys.exit(0)
