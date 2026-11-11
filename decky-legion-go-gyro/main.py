"""Legion Go Gyro - Decky Loader backend entry point."""

import asyncio
import os
import sys

import decky

_HERE = os.path.dirname(os.path.realpath(__file__))
_MODULES = os.path.join(_HERE, "py_modules")
if _MODULES not in sys.path:
    sys.path.insert(0, _MODULES)

from lgo_gyro import __version__  # noqa: E402
from lgo_gyro.service import GyroService  # noqa: E402

SETTINGS_FILE = os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")


class Plugin:
    service = None

    @staticmethod
    def _svc():
        if Plugin.service is None:
            Plugin.service = GyroService(SETTINGS_FILE, logger=decky.logger)
            Plugin.service.start()
        return Plugin.service

    # ---- lifecycle -------------------------------------------------------

    async def _main(self):
        decky.logger.info("Legion Go Gyro %s loading", __version__)
        try:
            Plugin._svc()
        except Exception:
            decky.logger.exception("Failed to start the gyro service")

    async def _unload(self):
        svc = Plugin.service
        Plugin.service = None
        if svc is not None:
            # Switches the controller IMUs off again and frees the port.
            await asyncio.get_event_loop().run_in_executor(None, svc.close)
        decky.logger.info("Legion Go Gyro unloaded")

    async def _uninstall(self):
        try:
            os.remove(SETTINGS_FILE)
        except OSError:
            pass

    async def _migration(self):
        pass

    # ---- API used by the frontend ---------------------------------------

    async def get_state(self):
        svc = Plugin._svc()
        return {
            "version": __version__,
            "settings": svc.get_settings(),
            "defaults": svc.get_defaults(),
            "status": svc.get_status(),
        }

    async def get_status(self):
        return Plugin._svc().get_status()

    async def set_settings(self, patch):
        return Plugin._svc().update_settings(patch)

    async def reset_settings(self):
        return Plugin._svc().reset_settings()

    async def calibrate(self, target):
        """Calibrate the sensor behind a slot ("combined", "left", "right").

        Resolves once the measurement finished. Result is one of:
        "ok", "moved" (device was not at rest), "nodata" (sensor inactive).
        """
        svc = Plugin._svc()
        name = svc.request_calibration(target)
        if name is None:
            return "nodata"
        return (await Plugin._await_calibration(svc, [name]))[name]

    async def calibrate_all(self):
        """Calibrate every sensor that is currently sending data.

        Returns {sensor name: result}; empty if no sensor is active.
        """
        svc = Plugin._svc()
        return await Plugin._await_calibration(svc, svc.request_calibration_all())

    @staticmethod
    async def _await_calibration(svc, names):
        serials = {n: svc.calibration_state(n)[1] for n in names}
        results = {}
        for _ in range(60):
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
