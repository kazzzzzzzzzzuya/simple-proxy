"""Coordinator of the hardware modules.

Keeps the saved settings applied to the device: at start, after resume,
when a controller reconnects, and continuously for the things that need a
watchful eye (automatic TDP, the swapped menu buttons).
"""

import copy
import logging
import threading
import time

from . import config as cfg
from . import controller, fan, fps_probe, sysio
from .battery import ChargeLimit, battery_info
from .plumber import MenuSwap
from .power import CUSTOM, AutoTdp, FpsMonitor, PowerLimits, cpu_boost_state, set_cpu_boost
from .powerled import PowerLed

LEGION_GO = ("83E1",)
LEGION_GO_2 = ("83N0", "83N1")
LEGION_GO_S = ("83L3", "83N6", "83Q2", "83Q3")
CONTROLLER_MODELS = LEGION_GO + LEGION_GO_2

TICK_S = 1.0
REASSERT_S = 10.0
SWAP_CHECK_S = 5.0
RECONNECT_DELAY_S = 2.0
RESUME_DELAY_S = 3.0

SECTIONS = ("lighting", "battery", "fan", "power", "controls")


class Hub(object):
    def __init__(self, settings_path, logger=None, product=None, modules=None,
                 controller_status=None, state_dir=None):
        self._path = settings_path
        self._log = logger or logging.getLogger("lgo_toolbox")
        self.product = product if product is not None else sysio.dmi("product_name")
        self.vendor = sysio.dmi("sys_vendor")
        self._controller_status = controller_status

        m = modules or {}
        self.limits = m.get("limits") or PowerLimits()
        self.fan = m.get("fan") or fan.FanControl()
        self.charge = m.get("charge") or ChargeLimit()
        self.led = m.get("led") or PowerLed(self.product)
        self.swap = m.get("swap") or MenuSwap(
            state_file=(state_dir + "/inputplumber_base_profile") if state_dir else None)
        self.fps = m.get("fps") or FpsMonitor(logger=self._log)
        self._send = m.get("send") or controller.send
        self._sensors = m.get("sensors") or fan.read_sensors
        self._battery_info = m.get("battery_info") or battery_info
        self._set_boost = m.get("set_boost") or set_cpu_boost
        self._boost_state = m.get("boost_state") or cpu_boost_state

        self._lock = threading.RLock()      # settings
        self._hw = threading.RLock()        # one hardware operation at a time
        self.settings = cfg.load(settings_path)
        self.errors = {}
        self._app = None
        self._app_name = ""

        self._thread = None
        self._stop = threading.Event()
        self._auto = None
        self._auto_applied = None
        self._power_applied_at = 0.0
        self._swap_checked = 0.0
        self._reapply_controller_at = None
        self._reapply_all_at = None
        self._controller_seen = None
        self._led_before_sleep = None
        self._caps = None
        self._caps_at = 0.0
        self._slow = {}

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------

    def start(self):
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="lgo-hub", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(8.0)
        self._thread = None
        self.fps.stop()
        try:
            self.led.close()
        except Exception:
            pass

    def _run(self):
        try:
            self.apply_all("start")
        except Exception:
            self._log.exception("Applying the saved settings failed")
        while not self._stop.wait(TICK_S):
            try:
                self._tick(time.monotonic())
            except Exception:
                self._log.exception("Hub tick failed")

    # ------------------------------------------------------------------
    # capabilities
    # ------------------------------------------------------------------

    def capabilities(self, refresh=False):
        now = time.monotonic()
        if self._caps is not None and not refresh and now - self._caps_at < 30.0:
            return self._caps
        is_go = self.product in LEGION_GO
        known = self.product in LEGION_GO + LEGION_GO_2 + LEGION_GO_S
        not_legion = "This is not a supported Legion Go (%s)" % (self.product or "unknown model")

        def cap(ok, reason=None):
            return {"available": bool(ok), "reason": None if ok else reason}

        caps = {}
        caps["power"] = cap(known and self.limits.available(),
                            not_legion if not known else self.limits.unavailable_reason())
        caps["fps"] = cap(bool(fps_probe.find_sockets()) and FpsMonitor.find_python() is not None,
                          "Frame rates can only be measured in Gaming Mode")
        if not is_go:
            caps["fan"] = cap(False, "Fan curves are only supported on the Legion Go (83E1)")
        else:
            caps["fan"] = cap(self.fan.available(),
                              "Needs the acpi_call kernel module, which SteamOS does not include")
        charge = self.charge.status() if known else {"available": False, "error": not_legion}
        caps["charge_limit"] = cap(charge["available"], charge.get("error"))
        caps["power_led"] = cap(self.led.available, self.led.error)
        ctrl_ok = self.product in CONTROLLER_MODELS
        ctrl_reason = "Only for the detachable Legion Go controllers"
        caps["sticks"] = cap(ctrl_ok, ctrl_reason)
        caps["remap"] = cap(ctrl_ok, ctrl_reason)
        caps["swap_menu"] = cap(self.swap.available(), "InputPlumber is not running")
        self._caps = caps
        self._caps_at = now
        return caps

    # ------------------------------------------------------------------
    # settings
    # ------------------------------------------------------------------

    def get_settings(self):
        with self._lock:
            return copy.deepcopy(self.settings)

    def _save(self):
        if not cfg.save(self._path, self.settings):
            self._log.warning("Could not write %s", self._path)

    def update(self, patch):
        """Merge a partial settings dict, save it and apply what changed."""
        with self._lock:
            old = self.settings
            new = cfg.merge(old, patch)
            self.settings = new
            self._save()
            changed = [s for s in SECTIONS if old[s] != new[s]]
        for section in changed:
            self._apply_section(section, old)
        return self.get_settings()

    def delete_power_profile(self, key):
        with self._lock:
            if key != "default" and key in self.settings["power"]["profiles"]:
                del self.settings["power"]["profiles"][key]
                self._save()
        self._apply_power(force=True)
        return self.get_settings()

    def reset(self):
        with self._lock:
            old = self.settings
            self.settings = cfg.defaults()
            self._save()
        for section in SECTIONS:
            self._apply_section(section, old)
        return self.get_settings()

    def set_running_app(self, app_id, name=""):
        try:
            app = int(app_id) if app_id not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            app = None
        if app == self._app:
            return
        self._app = app
        self._app_name = str(name or "")[:120]
        self._auto = None
        self._apply_power(force=True)

    # ------------------------------------------------------------------
    # applying
    # ------------------------------------------------------------------

    def _err(self, key, message):
        if message:
            self.errors[key] = str(message)
            self._log.warning("%s: %s", key, message)
        else:
            self.errors.pop(key, None)

    def apply_all(self, reason=""):
        self._log.info("Applying saved settings (%s)", reason)
        self.capabilities(refresh=True)
        self._apply_battery()
        self._apply_power_led()
        self._apply_power(force=True)
        self._apply_fan()
        self._apply_controller()
        self._apply_swap()

    def _apply_section(self, section, old):
        if section == "lighting":
            new = self.settings["lighting"]
            if old["lighting"]["sticks"] != new["sticks"]:
                self._apply_sticks()
            if old["lighting"]["power_led"] != new["power_led"]:
                self._apply_power_led()
        elif section == "battery":
            self._apply_battery()
        elif section == "fan":
            self._apply_fan(was_enabled=old["fan"]["enabled"])
        elif section == "power":
            self._apply_power(force=True, was_enabled=old["power"]["enabled"])
        elif section == "controls":
            o, n = old["controls"], self.settings["controls"]
            if o["swap_menu"] != n["swap_menu"]:
                self._apply_swap()
            if (o["remap"], o["remap_managed"], o["touchpad"]) != (
                    n["remap"], n["remap_managed"], n["touchpad"]):
                self._apply_remap(restore=o["remap_managed"] and not n["remap_managed"])

    # ---- battery -----------------------------------------------------------
    def _apply_battery(self):
        want = self.settings["battery"]["charge_limit"]
        if want is None:
            return
        with self._hw:
            status = self.charge.set(want)
        self._invalidate("charge")
        if not status["available"]:
            self._err("charge_limit", status.get("error") or "Not available")
        else:
            self._err("charge_limit", status.get("error"))

    # ---- lighting ----------------------------------------------------------
    def _apply_power_led(self):
        conf = self.settings["lighting"]["power_led"]
        if not conf["managed"]:
            return
        with self._hw:
            if not self.led.available:
                self._err("power_led", self.led.error)
                return
            ok = self.led.set(conf["on"])
        self._invalidate("led")
        self._err("power_led", None if ok else (self.led.error or "The light did not change"))

    def _apply_sticks(self):
        conf = self.settings["lighting"]["sticks"]
        if not conf["managed"] or self.product not in CONTROLLER_MODELS:
            return
        if conf["on"]:
            cmds = controller.rgb_commands(
                conf["mode"], conf["color"], conf["brightness"], conf["speed"])
        else:
            cmds = controller.rgb_off_commands()
        with self._hw:
            self._err("sticks", self._send(cmds))

    # ---- controls ----------------------------------------------------------
    def _apply_remap(self, restore=False):
        conf = self.settings["controls"]
        if self.product not in CONTROLLER_MODELS:
            return
        cmds = []
        if conf["remap_managed"]:
            cmds += [controller.remap_command(b, conf["remap"][b])
                     for b in controller.REMAP_BUTTONS]
        elif restore:
            cmds += [controller.remap_command(b, "none") for b in controller.REMAP_BUTTONS]
        if conf["touchpad"] is not None:
            cmds.append(controller.touchpad_command(conf["touchpad"]))
        if cmds:
            with self._hw:
                self._err("remap", self._send(cmds))

    def _apply_controller(self):
        self._apply_sticks()
        self._apply_remap()

    def _apply_swap(self):
        want = self.settings["controls"]["swap_menu"]
        with self._hw:
            if not want and not self.swap.available():
                self._err("swap_menu", None)
                return
            ok = self.swap.apply(want)
        self._invalidate("swap")
        self._err("swap_menu", None if ok else self.swap.error)

    # ---- fan ---------------------------------------------------------------
    def _apply_fan(self, was_enabled=False):
        conf = self.settings["fan"]
        if self.product not in LEGION_GO:
            return
        with self._hw:
            if not conf["enabled"]:
                if was_enabled and self.fan.available():
                    self.fan.restore()
                self._err("fan", None)
                return
            if not self.fan.available():
                self._err("fan", "Needs the acpi_call kernel module")
                return
            power = self.settings["power"]
            managed_mode = self.active_profile()[1]["mode"] if power["enabled"] else None
            if managed_mode not in (None, CUSTOM) and not conf["full_speed"]:
                # The firmware only follows a custom table in its custom mode.
                self._err("fan", None)
                return
            ok = self.fan.apply(conf["curve"], conf["full_speed"],
                                ensure_custom=managed_mode is None)
        self._err("fan", None if ok else self.fan.error)

    # ---- power ---------------------------------------------------------------
    def active_profile(self):
        """(key, profile) the power module uses right now."""
        power = self.settings["power"]
        key = str(self._app) if self._app is not None else None
        if power["per_game"] and key and key in power["profiles"]:
            return key, power["profiles"][key]
        return "default", power["profiles"]["default"]

    def _tdp_bounds(self, profile):
        rng = self.limits.tdp_range() or (cfg.TDP_ABS_MIN, cfg.TDP_ABS_MAX)
        top = max(rng[0], min(rng[1], profile["tdp"]))
        low = max(rng[0], min(top, profile["min_tdp"]))
        return low, top

    def _apply_power(self, force=False, was_enabled=False):
        power = self.settings["power"]
        with self._hw:
            if not power["enabled"]:
                self._auto = None
                self.fps.set_app(None)
                if was_enabled and self.limits.available():
                    # Hand the device back in a normal firmware mode.
                    choices = self.limits.profiles()
                    for name in ("balanced", "performance"):
                        if name in choices:
                            self.limits.set_profile(name)
                            break
                self._err("power", None)
                return
            if not self.limits.available():
                self._err("power", self.limits.unavailable_reason())
                return
            if power["cpu_boost"] is not None:
                self._set_boost(power["cpu_boost"])
            _, profile = self.active_profile()
            self._power_applied_at = time.monotonic()
            if profile["mode"] != CUSTOM:
                self._auto = None
                self.fps.set_app(None)
                ok = self.limits.profile() == profile["mode"] or \
                    self.limits.set_profile(profile["mode"])
                self._err("power", None if ok else self.limits.error)
                return
            low, top = self._tdp_bounds(profile)
            if profile["auto"]:
                if self._auto is None or force:
                    start = self._auto.tdp if self._auto is not None else top
                    self._auto = AutoTdp(low, top, profile["target_fps"], start=start)
                    self._auto_applied = None
                else:
                    self._auto.retarget(low, top, profile["target_fps"])
                self.fps.set_app(self._app)
                want = self._auto.tdp if self._app is not None else top
            else:
                self._auto = None
                self.fps.set_app(None)
                want = top
            was_custom = self.limits.profile() == CUSTOM
            ok = self.limits.set_tdp(want)
            self._auto_applied = want if ok else None
            self._err("power", None if ok else self.limits.error)
        if ok and not was_custom and self.settings["fan"]["enabled"]:
            time.sleep(0.5)
            self._apply_fan()

    def _auto_step(self, now):
        auto = self._auto
        if auto is None or not self.settings["power"]["enabled"]:
            return
        if self._app is None:
            want = auto.tdp_max
            auto.state = "no_game"
        else:
            want = auto.step(self.fps.get(), now)
        if want != self._auto_applied:
            with self._hw:
                if self.limits.set_tdp(want):
                    self._auto_applied = want
                    self._err("power", None)
                else:
                    self._err("power", self.limits.error)

    # ------------------------------------------------------------------
    # periodic
    # ------------------------------------------------------------------

    def _tick(self, now):
        self._auto_step(now)

        power = self.settings["power"]
        if power["enabled"] and now - self._power_applied_at >= REASSERT_S:
            self._power_applied_at = now
            if self.limits.available():
                _, profile = self.active_profile()
                drifted = self.limits.profile() != profile["mode"]
                if not drifted and profile["mode"] == CUSTOM:
                    want = self._auto_applied if self._auto is not None else \
                        self._tdp_bounds(profile)[1]
                    drifted = want is not None and self.limits.tdp() != want
                if drifted:
                    self._log.info("Power settings were changed from outside, re-applying")
                    self._apply_power()

        if self.settings["controls"]["swap_menu"] and now - self._swap_checked >= SWAP_CHECK_S:
            self._swap_checked = now
            if self.swap.is_active() is False:
                self._apply_swap()

        # Controllers that (re)appear get their lights and mapping again
        if self._controller_status is not None:
            try:
                seen = bool(self._controller_status())
            except Exception:
                seen = False
            if seen and self._controller_seen is False:
                self._reapply_controller_at = now + RECONNECT_DELAY_S
            self._controller_seen = seen
        if self._reapply_controller_at is not None and now >= self._reapply_controller_at:
            self._reapply_controller_at = None
            self._apply_controller()
        if self._reapply_all_at is not None and now >= self._reapply_all_at:
            self._reapply_all_at = None
            self.apply_all("resume")

    # ------------------------------------------------------------------
    # suspend / resume
    # ------------------------------------------------------------------

    def on_suspend(self):
        conf = self.settings["lighting"]["power_led"]
        self._led_before_sleep = None
        if conf["off_in_sleep"] and self.led.available:
            with self._hw:
                state = self.led.get()
                if state:
                    self._led_before_sleep = True
                    self.led.set(False)
        self.fps.set_app(None)

    def on_resume(self):
        if self._led_before_sleep:
            with self._hw:
                self.led.set(True)
        self._led_before_sleep = None
        # Firmware and controllers need a moment after waking up.
        self._reapply_all_at = time.monotonic() + RESUME_DELAY_S

    # ------------------------------------------------------------------
    # status
    # ------------------------------------------------------------------

    def _cached(self, name, fn, ttl=5.0):
        """Slow readings (firmware calls, D-Bus) are refreshed at most every
        `ttl` seconds; ``_invalidate`` forces a fresh one."""
        now = time.monotonic()
        hit = self._slow.get(name)
        if hit is not None and now - hit[0] < ttl:
            return hit[1]
        try:
            value = fn()
        except Exception:
            value = None
        self._slow[name] = (now, value)
        return value

    def _invalidate(self, name):
        self._slow.pop(name, None)

    def status(self):
        s = self.settings
        key, profile = self.active_profile()
        rng = self.limits.tdp_range()
        auto = self._auto
        charge = self._cached("charge", self.charge.status) or {}
        return {
            "model": self.product,
            "errors": dict(self.errors),
            "app": {"id": self._app, "name": self._app_name},
            "power": {
                "profile_key": key,
                "mode": self.limits.profile(),
                "modes": self.limits.profiles(),
                "tdp": self.limits.tdp(),
                "range": list(rng) if rng else None,
                "cpu_boost": self._boost_state(),
                "auto": {
                    "active": auto is not None and s["power"]["enabled"],
                    "state": auto.state if auto is not None else None,
                    "fps": self.fps.get() if auto is not None else None,
                    "probe_error": self.fps.error,
                },
            },
            "fan": self._sensors(),
            "battery": {
                "info": self._battery_info(),
                "charge_limit": charge.get("enabled"),
                "backend": charge.get("backend"),
            },
            "power_led": self._cached("led", self.led.get) if self.led.available else None,
            "swap_menu": {"active": bool(self._cached("swap", self.swap.is_active))
                          if s["controls"]["swap_menu"] else False},
        }
