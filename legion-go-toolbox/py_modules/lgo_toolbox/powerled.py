"""Power button light of the Legion Go family.

The light is one bit in the embedded controller (EC) memory: "LEDP".
A second bit, "LEDM", stops the light from breathing while the device
sleeps (BIOS 35 and newer). Both use inverted logic for the light itself:
LEDP = 0 means on.

Three ways to reach those bits are tried in this order, each one verified
by reading the bit back:

1. acpi_call  - Lenovo's own WMI lighting method
2. mmio       - the EC memory window the BIOS maps at 0xFE0B0300
3. ec_port    - the classic EC I/O ports 0x62 / 0x66

Register locations are from DSDT analysis published by hhd-dev/hwinfo and
used by HueSync.
"""

import mmap
import os
import time

from . import acpi

# DMI product name -> (LEDP offset, LEDP bit)
MODELS = {
    "83E1": (0x52, 5),   # Legion Go
    "83N0": (0x52, 5),   # Legion Go 2
    "83N1": (0x52, 5),
    "83L3": (0x10, 6),   # Legion Go S
    "83N6": (0x10, 6),
    "83Q2": (0x10, 6),
    "83Q3": (0x10, 6),
}
LEDM_OFFSET = 0x58
LEDM_BIT = 0

ECMM_BASE = 0xFE0B0300
ECMM_SIZE = 0x100

WMI_LIGHT_ID = 0x04
WMI_MODE_ID = 0x24


class _Backend(object):
    name = ""

    def read(self, offset):
        raise NotImplementedError

    def write(self, offset, value):
        raise NotImplementedError

    def close(self):
        pass

    # Shared bit logic for the two raw EC backends -------------------
    def get(self, offset, bit):
        return (self.read(offset) >> bit) & 1 == 0

    def set(self, offset, bit, on):
        mask = 1 << bit
        cur = self.read(offset)
        new = (cur & ~mask) if on else (cur | mask)
        if new != cur:
            self.write(offset, new & 0xFF)
        # Sleep breathing follows the light: off -> no breathing either.
        try:
            mcur = self.read(LEDM_OFFSET)
            mmask = 1 << LEDM_BIT
            mnew = (mcur & ~mmask) if on else (mcur | mmask)
            if mnew != mcur:
                self.write(LEDM_OFFSET, mnew & 0xFF)
        except (OSError, IOError):
            pass   # older BIOS without LEDM
        return self.get(offset, bit) == on


class MmioBackend(_Backend):
    name = "mmio"

    def __init__(self, dev="/dev/mem", base=ECMM_BASE):
        page = mmap.PAGESIZE
        self._skew = base % page
        self._fd = os.open(dev, os.O_RDWR | getattr(os, "O_SYNC", 0))
        try:
            self._mm = mmap.mmap(
                self._fd, self._skew + ECMM_SIZE, mmap.MAP_SHARED,
                mmap.PROT_READ | mmap.PROT_WRITE, offset=base - self._skew)
        except Exception:
            os.close(self._fd)
            raise

    def read(self, offset):
        return self._mm[self._skew + offset]

    def write(self, offset, value):
        self._mm[self._skew + offset] = value

    def close(self):
        try:
            self._mm.close()
        finally:
            os.close(self._fd)


class PortBackend(_Backend):
    """Standard ACPI EC protocol on I/O ports, via /dev/port."""

    name = "ec_port"
    DATA = 0x62
    CMD = 0x66
    IBF = 0x02
    OBF = 0x01
    TIMEOUT_S = 0.2

    def __init__(self, dev="/dev/port"):
        self._fd = os.open(dev, os.O_RDWR)

    def _inb(self, port):
        return os.pread(self._fd, 1, port)[0]

    def _outb(self, port, value):
        os.pwrite(self._fd, bytes([value & 0xFF]), port)

    def _wait(self, mask, want):
        deadline = time.monotonic() + self.TIMEOUT_S
        while time.monotonic() < deadline:
            if bool(self._inb(self.CMD) & mask) == want:
                return
            time.sleep(0.0005)
        raise OSError("EC did not respond")

    def read(self, offset):
        self._wait(self.IBF, False)
        self._outb(self.CMD, 0x80)
        self._wait(self.IBF, False)
        self._outb(self.DATA, offset)
        self._wait(self.OBF, True)
        return self._inb(self.DATA)

    def write(self, offset, value):
        self._wait(self.IBF, False)
        self._outb(self.CMD, 0x81)
        self._wait(self.IBF, False)
        self._outb(self.DATA, offset)
        self._wait(self.IBF, False)
        self._outb(self.DATA, value)
        self._wait(self.IBF, False)

    def close(self):
        os.close(self._fd)


class AcpiBackend(_Backend):
    name = "acpi_call"

    def get(self, offset, bit):
        res = acpi.call(acpi.WMAF, [0, 0x01, WMI_LIGHT_ID])
        if isinstance(res, (bytes, bytearray)) and len(res) >= 2:
            return res[1] == 0x02
        if isinstance(res, int):
            return (res >> 8) & 0xFF == 0x02
        raise OSError("unexpected lighting reply")

    def set(self, offset, bit, on):
        acpi.call(acpi.WMAF, [0, 0x02, bytes([WMI_LIGHT_ID, 0x00, 0x02 if on else 0x01])])
        try:
            acpi.call(acpi.WMAF, [0, 0x02, bytes([WMI_MODE_ID, 0x00, 0x03 if on else 0x01])])
        except acpi.AcpiError:
            pass   # older BIOS without LEDM
        return self.get(offset, bit) == on


class PowerLed(object):
    def __init__(self, product, factories=None):
        self.product = product
        self.location = MODELS.get(product)
        self.backend = None
        self.error = None
        self._factories = factories
        self._probed = False

    def _candidates(self):
        if self._factories is not None:
            return self._factories
        out = []
        if acpi.available():
            out.append(AcpiBackend)
        out.append(MmioBackend)
        out.append(PortBackend)
        return out

    def _probe(self):
        if self._probed:
            return
        self._probed = True
        if self.location is None:
            self.error = "Not a supported Legion Go model (%s)" % (self.product or "unknown")
            return
        offset, bit = self.location
        errors = []
        for factory in self._candidates():
            backend = None
            try:
                backend = factory()
                backend.get(offset, bit)
                self.backend = backend
                self.error = None
                return
            except Exception as e:   # try the next route
                errors.append("%s: %s" % (getattr(factory, "name", "?"), e))
                if backend is not None:
                    try:
                        backend.close()
                    except Exception:
                        pass
        self.error = "No way to reach the power light (" + "; ".join(errors) + ")"

    @property
    def available(self):
        self._probe()
        return self.backend is not None

    def get(self):
        """True = on, False = off, None = unknown."""
        self._probe()
        if self.backend is None:
            return None
        try:
            return self.backend.get(*self.location)
        except Exception as e:
            self.error = str(e)
            return None

    def set(self, on):
        """Returns True if the light is verifiably in the requested state."""
        self._probe()
        if self.backend is None:
            return False
        try:
            ok = bool(self.backend.set(self.location[0], self.location[1], bool(on)))
            self.error = None if ok else "The light did not change"
            return ok
        except Exception as e:
            self.error = str(e)
            return False

    def close(self):
        if self.backend is not None:
            try:
                self.backend.close()
            except Exception:
                pass
            self.backend = None
        self._probed = False
