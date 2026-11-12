"""Lenovo firmware (WMI) calls through the ``acpi_call`` kernel module.

The Legion Go BIOS exposes fan curve, charge limit and power light through
WMI methods under ``\\_SB.GZFD`` - the same interface Lenovo's Legion Space
uses on Windows. Linux has no generic way to invoke them from user space
except the out-of-tree ``acpi_call`` module (/proc/acpi/call). SteamOS does
not ship that module, so everything here is optional: callers must check
``available()`` and offer another route where one exists.

Method names and argument layouts are firmware facts documented by the
hhd-dev/adjustor and LegionGoRemapper projects.
"""

import os
import threading

from . import sysio

PATH = "/proc/acpi/call"

WMAA = r"\_SB.GZFD.WMAA"   # power mode
WMAB = r"\_SB.GZFD.WMAB"   # fan table
WMAE = r"\_SB.GZFD.WMAE"   # feature get/set
WMAF = r"\_SB.GZFD.WMAF"   # lighting

_lock = threading.Lock()
_probe_done = False


class AcpiError(Exception):
    pass


def available(path=PATH):
    """True if /proc/acpi/call exists (loading the module if installed)."""
    global _probe_done
    if os.path.exists(path):
        return True
    if path != PATH or _probe_done:
        return False
    _probe_done = True   # modprobe only once per plugin load
    sysio.run(["modprobe", "acpi_call"], timeout=5.0)
    return os.path.exists(path)


def format_call(method, args):
    cmd = method
    for arg in args:
        if isinstance(arg, (bytes, bytearray)):
            cmd += " b" + bytes(arg).hex()
        else:
            cmd += " 0x%02x" % int(arg)
    return cmd


def parse_result(raw):
    """Decode what /proc/acpi/call returns: int, bytes or None."""
    text = raw.replace("\0", "").strip()
    if not text or text == "not called":
        return None
    if text.startswith("0x"):
        try:
            return int(text, 16)
        except ValueError:
            raise AcpiError("unexpected reply: %r" % text)
    if text.startswith("{") and text.endswith("}"):
        body = text[1:-1].strip()
        try:
            return bytes(int(b.strip(), 16) for b in body.split(",") if b.strip())
        except ValueError:
            raise AcpiError("unexpected reply: %r" % text)
    raise AcpiError(text)


def call(method, args, path=PATH):
    """Invoke a method and return its decoded result."""
    cmd = format_call(method, args)
    with _lock:
        try:
            with open(path, "w") as f:
                f.write(cmd)
            with open(path, "r") as f:
                raw = f.read()
        except (OSError, IOError) as e:
            raise AcpiError("acpi_call failed: %s" % (e.strerror or e))
    return parse_result(raw)


def get_feature(feature_id, path=PATH):
    return call(WMAE, [0, 0x11, int(feature_id).to_bytes(4, "little")], path)


def set_feature(feature_id, value, path=PATH):
    payload = int(feature_id).to_bytes(4, "little") + int(value).to_bytes(4, "little")
    return call(WMAE, [0, 0x12, payload], path)
