"""Battery charge limit ("Long Life" mode, stops charging at about 80 %).

Preferred route is the kernel's power-supply attribute ``charge_types``
(Lenovo WMI battery driver). Where that is missing, Lenovo's firmware
method is used through acpi_call if the module is present.
"""

import os
import re

from . import acpi, sysio

POWER_SUPPLY = "/sys/class/power_supply"
STANDARD = "Standard"
LONG_LIFE = "Long_Life"
FEATURE_CHARGE_LIMIT = 0x03010001


def parse_charge_types(raw):
    """'Standard [Long_Life]' -> (('Standard', 'Long_Life'), 'Long_Life')"""
    active = re.findall(r"\[([^\]]+)\]", raw)
    values = tuple(re.sub(r"[\[\]]", "", raw).split())
    if len(active) != 1 or not values:
        raise ValueError("unexpected charge_types: %r" % raw)
    return values, active[0]


def find_charge_types(root=POWER_SUPPLY):
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return None
    for name in names:
        path = os.path.join(root, name, "charge_types")
        raw = sysio.read_text(path)
        if raw is None:
            continue
        try:
            values, _ = parse_charge_types(raw)
        except ValueError:
            continue
        if STANDARD in values and LONG_LIFE in values:
            return path
    return None


class ChargeLimit(object):
    def __init__(self, root=POWER_SUPPLY, acpi_path=acpi.PATH):
        self._root = root
        self._acpi_path = acpi_path

    def status(self):
        """{'available', 'enabled', 'backend', 'error'}"""
        path = find_charge_types(self._root)
        if path:
            try:
                _, active = parse_charge_types(sysio.read_text(path, ""))
                return {"available": True, "enabled": active == LONG_LIFE,
                        "backend": "sysfs", "error": None}
            except ValueError as e:
                return {"available": False, "enabled": None, "backend": None,
                        "error": str(e)}
        if acpi.available(self._acpi_path):
            try:
                value = acpi.get_feature(FEATURE_CHARGE_LIMIT, self._acpi_path)
                if value in (0, 1):
                    return {"available": True, "enabled": bool(value),
                            "backend": "acpi_call", "error": None}
                return {"available": False, "enabled": None, "backend": None,
                        "error": "Firmware gave an unexpected answer"}
            except acpi.AcpiError as e:
                return {"available": False, "enabled": None, "backend": None,
                        "error": str(e)}
        return {"available": False, "enabled": None, "backend": None,
                "error": "This SteamOS version does not expose the battery "
                         "charge mode"}

    def set(self, enabled):
        """Apply and verify. Returns the resulting status dict."""
        before = self.status()
        if not before["available"] or before["enabled"] == bool(enabled):
            return before
        if before["backend"] == "sysfs":
            err = sysio.write_text(find_charge_types(self._root),
                                   LONG_LIFE if enabled else STANDARD)
            if err:
                before["error"] = "Could not change the charge mode: " + err
                return before
        else:
            try:
                acpi.set_feature(FEATURE_CHARGE_LIMIT, 1 if enabled else 0,
                                 self._acpi_path)
            except acpi.AcpiError as e:
                before["error"] = str(e)
                return before
        after = self.status()
        if after["available"] and after["enabled"] != bool(enabled):
            after["error"] = "The charge mode did not change"
        return after


def battery_info(root=POWER_SUPPLY):
    """Charge level and state of the internal battery."""
    try:
        names = sorted(os.listdir(root))
    except OSError:
        return None
    for name in names:
        base = os.path.join(root, name)
        if sysio.read_text(os.path.join(base, "type")) != "Battery":
            continue
        if sysio.read_text(os.path.join(base, "scope")) == "Device":
            continue   # a peripheral's battery
        cap = sysio.read_int(os.path.join(base, "capacity"))
        if cap is None:
            continue
        return {"percent": cap,
                "status": sysio.read_text(os.path.join(base, "status"), "")}
    return None
