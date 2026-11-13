"""Smooth mode: an even frame cap plus the matching screen refresh rate.

* The cap comes from MangoHud's limiter with ``fps_limit_method=early``
  (the Linux counterpart of RTSS's front-edge / async limiter), switched
  on per game through the game's launch options. Games without MangoHud's
  Vulkan layer fall back to DXVK's own limiter (DirectX 9-11 under Proton).
* The refresh rate comes from gamescope: the screen switches to the highest
  rate that is an exact multiple of the cap (60 -> 60 Hz, 72 -> 144 Hz), so
  every frame is shown for the same number of refreshes. gamescope's own
  limiter stays off, so two limiters never stack.
"""

import glob
import json
import os

from . import fps_probe

CANDIDATES = (30, 36, 40, 45, 48, 60, 72, 90, 120, 144)
LAYER_DIRS = ("/usr/share/vulkan/implicit_layer.d", "/etc/vulkan/implicit_layer.d")


def mangohud_env(dirs=LAYER_DIRS):
    """Environment variable that switches MangoHud's Vulkan layer on, or
    None if the layer is not installed."""
    for d in dirs:
        for path in sorted(glob.glob(os.path.join(d, "*.json"))):
            if "mangohud" not in os.path.basename(path).lower():
                continue
            try:
                with open(path, "r") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            layers = data.get("layers") or [data.get("layer") or {}]
            for layer in layers:
                env = layer.get("enable_environment") if isinstance(layer, dict) else None
                if isinstance(env, dict) and env:
                    return sorted(env)[0]
    return None


def launch_prefix(fps, env=None):
    """Launch-option text that caps a game at `fps` (exact, removable)."""
    fps = int(fps)
    if env:
        return "%s=1 MANGOHUD_CONFIG=fps_limit=%d,fps_limit_method=early,no_display" % (env, fps)
    return "DXVK_FRAME_RATE=%d" % fps


def fps_choices(rates):
    """Caps that divide one of the screen's refresh rates evenly."""
    if not rates:
        return [c for c in CANDIDATES if c in (30, 60)]
    top = max(rates)
    return [c for c in CANDIDATES if c <= top and any(r % c == 0 for r in rates)]


def refresh_for(fps, rates):
    """The refresh rate gamescope will pick for `fps` (None: no match)."""
    for r in sorted(rates or (), reverse=True):
        if r % fps == 0:
            return r
    return None


class Gamescope(object):
    """Short connections to gamescope's control interface."""

    def __init__(self, sockets=fps_probe.find_sockets, connect=fps_probe.Connection):
        self._sockets = sockets
        self._connect = connect
        self.error = None

    def _with(self, fn):
        for path in self._sockets():
            try:
                conn = self._connect(path)
            except (OSError, fps_probe.ProbeError) as e:
                self.error = str(e)
                continue
            try:
                out = fn(conn)
                self.error = None
                return out, True
            except (OSError, fps_probe.ProbeError) as e:
                self.error = str(e)
            finally:
                conn.close()
        if self.error is None:
            self.error = "gamescope is not running (Gaming Mode only)"
        return None, False

    def display(self):
        info, _ = self._with(lambda c: c.display_info())
        return info

    def set_refresh(self, fps, internal=True):
        _, ok = self._with(lambda c: c.set_refresh_cycle(fps, internal))
        return ok
