"""Legion Go Toolbox - Decky Loader backend entry point."""

import asyncio
import os
import sys

import decky

_HERE = os.path.dirname(os.path.realpath(__file__))
_MODULES = os.path.join(_HERE, "py_modules")
if _MODULES not in sys.path:
    sys.path.insert(0, _MODULES)

from lgo_gyro.service import GyroService  # noqa: E402
from lgo_toolbox import __version__  # noqa: E402
from lgo_toolbox.hub import Hub  # noqa: E402

GYRO_SETTINGS = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "gyro.json")
HUB_SETTINGS = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "toolbox.json")


async def _bg(fn, *args):
    """Hardware calls can take a moment: keep them off the event loop."""
    return await asyncio.get_event_loop().run_in_executor(None, fn, *args)


class Plugin:
    gyro = None
    hub = None

    @staticmethod
    def _gyro():
        if Plugin.gyro is None:
            Plugin.gyro = GyroService(GYRO_SETTINGS, logger=decky.logger)
            Plugin.gyro.start()
        return Plugin.gyro

    @staticmethod
    def _hub():
        if Plugin.hub is None:
            gyro = Plugin._gyro()
            Plugin.hub = Hub(
                HUB_SETTINGS, logger=decky.logger,
                controller_status=lambda: gyro.get_status()["device"]["receiving"],
                state_dir=decky.DECKY_PLUGIN_SETTINGS_DIR)
            Plugin.hub.start()
        return Plugin.hub

    @staticmethod
    def _adopt_gyro_settings():
        """Carry tuning and calibration over from the stand-alone
        "Legion Go Gyro" plugin the first time this one runs."""
        if os.path.exists(GYRO_SETTINGS):
            return
        old = os.path.join(os.path.dirname(decky.DECKY_PLUGIN_SETTINGS_DIR.rstrip("/")),
                           "Legion Go Gyro", "settings.json")
        try:
            with open(old, "rb") as src:
                data = src.read()
            with open(GYRO_SETTINGS, "wb") as dst:
                dst.write(data)
            decky.logger.info("Adopted settings from Legion Go Gyro")
        except OSError:
            pass

    # ---- lifecycle -------------------------------------------------------

    async def _main(self):
        decky.logger.info("Legion Go Toolbox %s loading", __version__)
        try:
            os.makedirs(decky.DECKY_PLUGIN_SETTINGS_DIR, exist_ok=True)
            Plugin._adopt_gyro_settings()
            Plugin._hub()
        except Exception:
            decky.logger.exception("Failed to start")

    async def _unload(self):
        hub, gyro = Plugin.hub, Plugin.gyro
        Plugin.hub = Plugin.gyro = None
        if hub is not None:
            await _bg(hub.stop)
        if gyro is not None:
            await _bg(gyro.close)
        decky.logger.info("Legion Go Toolbox unloaded")

    async def _uninstall(self):
        for path in (GYRO_SETTINGS, HUB_SETTINGS):
            try:
                os.remove(path)
            except OSError:
                pass

    async def _migration(self):
        pass

    # ---- whole plugin ----------------------------------------------------

    async def get_state(self):
        hub, gyro = Plugin._hub(), Plugin._gyro()
        return {
            "version": __version__,
            "caps": await _bg(hub.capabilities, True),
            "settings": hub.get_settings(),
            "status": await _bg(hub.status),
            "gyro": {
                "settings": gyro.get_settings(),
                "defaults": gyro.get_defaults(),
                "status": gyro.get_status(),
            },
        }

    async def get_status(self):
        hub = Plugin._hub()
        return {"hub": await _bg(hub.status), "gyro": Plugin._gyro().get_status()}

    async def update_settings(self, patch):
        return await _bg(Plugin._hub().update, patch)

    async def reset_settings(self):
        return await _bg(Plugin._hub().reset)

    async def delete_power_profile(self, key):
        return await _bg(Plugin._hub().delete_power_profile, str(key))

    async def smooth_plan(self, fps):
        return await _bg(Plugin._hub().smooth_plan, int(fps))

    async def set_smooth_game(self, app_id, fps, name, prefix, added_command):
        return await _bg(Plugin._hub().set_smooth_game, app_id,
                         None if fps is None else int(fps), name, prefix, added_command)

    async def delete_fan_profile(self, key):
        return await _bg(Plugin._hub().delete_fan_profile, str(key))

    async def set_game_controller(self, app_id, kind, name):
        """kind None removes the game's own controller type."""
        return await _bg(Plugin._hub().set_game_controller, app_id, kind, name)

    async def set_running_app(self, app_id, name):
        await _bg(Plugin._hub().set_running_app, app_id, name)

    async def on_suspend(self):
        await _bg(Plugin._hub().on_suspend)

    async def on_resume(self):
        await _bg(Plugin._hub().on_resume)

    # ---- gyro ------------------------------------------------------------

    async def gyro_set_settings(self, patch):
        return Plugin._gyro().update_settings(patch)

    async def gyro_reset_settings(self):
        return Plugin._gyro().reset_settings()

    async def gyro_calibrate(self, target):
        svc = Plugin._gyro()
        name = svc.request_calibration(target)
        if name is None:
            return "nodata"
        return (await Plugin._await_calibration(svc, [name]))[name]

    async def gyro_calibrate_all(self):
        svc = Plugin._gyro()
        return await Plugin._await_calibration(svc, svc.request_calibration_all())

    @staticmethod
    async def _await_calibration(svc, names):
        serials = {n: svc.calibration_state(n)[1] for n in names}
        results = {}
        for _ in range(90):
            if len(results) == len(names):
                break
            await asyncio.sleep(0.1)
            for n in names:
                _, serial, result = svc.calibration_state(n)
                if n not in results and serial != serials[n]:
                    results[n] = result or "nodata"
        for n in names:
            results.setdefault(n, "nodata")
        return results
