"""Small file / process helpers shared by the hardware modules."""

import os
import subprocess


def read_text(path, default=None):
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except (OSError, IOError, UnicodeError):
        return default


def read_int(path, default=None):
    value = read_text(path)
    if value is None:
        return default
    try:
        return int(value.split()[0])
    except (ValueError, IndexError):
        return default


def write_text(path, value):
    """Write a sysfs attribute. Returns None on success, else the error."""
    try:
        with open(path, "w") as f:
            f.write(str(value))
        return None
    except (OSError, IOError) as e:
        return e.strerror or str(e)


def clean_env():
    """Environment for child processes.

    Decky's bundled Python sets LD_LIBRARY_PATH to its own libraries, which
    breaks system tools; they must run with the system's defaults.
    """
    env = dict(os.environ)
    env.pop("LD_LIBRARY_PATH", None)
    env.pop("LD_PRELOAD", None)
    env.pop("PYTHONHOME", None)
    env.pop("PYTHONPATH", None)
    return env


def run(cmd, timeout=5.0):
    """Run a command. Returns (returncode, stdout, stderr); rc -1 if it
    could not be started or timed out."""
    try:
        p = subprocess.run(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            timeout=timeout, env=clean_env(), stdin=subprocess.DEVNULL,
        )
        return (p.returncode, p.stdout.decode("utf-8", "replace"),
                p.stderr.decode("utf-8", "replace"))
    except (OSError, subprocess.SubprocessError) as e:
        return -1, "", str(e)


def dmi(field):
    return read_text("/sys/class/dmi/id/" + field, "") or ""
