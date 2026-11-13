"""Configuration commands for the Legion Go controllers.

Stick lights, back-button remapping and the touchpad switch are set with
short vendor commands on the same HID interface the gyro data comes from.
Command layouts follow the protocol notes of Handheld Daemon (hhd) and
LegionGoRemapper / Legion Center.
"""

import os
import time

from lgo_gyro import legion_hid

LEFT = 0x03
RIGHT = 0x04

# ---- stick lights ----------------------------------------------------------
RGB_MODES = {"solid": 1, "pulse": 2, "rainbow": 3, "spiral": 4}
RGB_PROFILE = 3


def _level(value):
    return min(max(int(64 * float(value)), 0), 63)


def rgb_commands(mode, color, brightness, speed):
    """Commands that set and activate a light effect on both sticks."""
    m = RGB_MODES[mode]
    r, g, b = (min(max(int(c), 0), 255) for c in color)
    bright = _level(brightness)
    period = _level(1.0 - float(speed))
    out = []
    for ctrl in (LEFT, RIGHT):
        out.append(bytes([0x05, 0x0C, 0x72, 0x01, ctrl, m, r, g, b,
                          bright, period, RGB_PROFILE, 0x01]))
    for ctrl in (LEFT, RIGHT):
        out.append(bytes([0x05, 0x06, 0x73, 0x02, ctrl, RGB_PROFILE, 0x01]))
    for ctrl in (LEFT, RIGHT):
        out.append(bytes([0x05, 0x06, 0x70, 0x02, ctrl, 0x01, 0x01]))
    return out


def rgb_off_commands():
    return [bytes([0x05, 0x06, 0x70, 0x02, ctrl, 0x00, 0x01]) for ctrl in (LEFT, RIGHT)]


# ---- back buttons ------------------------------------------------------------
# button -> (controller, code)
REMAP_BUTTONS = {
    "Y1": (LEFT, 0x1C),
    "Y2": (LEFT, 0x1D),
    "Y3": (RIGHT, 0x1E),
    "M2": (RIGHT, 0x21),
    "M3": (RIGHT, 0x22),
}

REMAP_ACTIONS = {
    "none": 0x00,
    "ls_click": 0x03, "ls_up": 0x04, "ls_down": 0x05, "ls_left": 0x06, "ls_right": 0x07,
    "rs_click": 0x08, "rs_up": 0x09, "rs_down": 0x0A, "rs_left": 0x0B, "rs_right": 0x0C,
    "dpad_up": 0x0D, "dpad_down": 0x0E, "dpad_left": 0x0F, "dpad_right": 0x10,
    "a": 0x12, "b": 0x13, "x": 0x14, "y": 0x15,
    "lb": 0x16, "lt": 0x17, "rb": 0x18, "rt": 0x19,
    "view": 0x23, "menu": 0x24,
}


def remap_command(button, action):
    ctrl, code = REMAP_BUTTONS[button]
    return bytes([0x05, 0x07, 0x6C, 0x02, ctrl, code, REMAP_ACTIONS[action], 0x01])


def touchpad_command(enabled):
    return bytes([0x05, 0x06, 0x6B, 0x02, RIGHT, 0x01 if enabled else 0x00, 0x01])


# ---- transport ---------------------------------------------------------------

def default_opener():
    cands = legion_hid.find_candidates()
    if not cands:
        raise OSError("Legion Go controllers not found")
    return legion_hid.open_candidate(cands[0])


def send(commands, opener=None, gap_s=0.02):
    """Write commands to the controller interface.

    Returns None on success, otherwise a readable error.
    """
    if not commands:
        return None
    try:
        fd = (opener or default_opener)()
    except OSError as e:
        return e.strerror or str(e)
    try:
        for i, cmd in enumerate(commands):
            if i:
                time.sleep(gap_s)
            for attempt in range(3):
                try:
                    os.write(fd, cmd)
                    break
                except BlockingIOError:
                    time.sleep(0.01)
            else:
                return "The controller interface is busy"
        return None
    except OSError as e:
        return e.strerror or str(e)
    finally:
        try:
            os.close(fd)
        except OSError:
            pass
