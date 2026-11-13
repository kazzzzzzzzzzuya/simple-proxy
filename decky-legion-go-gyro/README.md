# Legion Go Gyro (Decky plugin)

Per-controller motion for the **Lenovo Legion Go** on SteamOS.

Each detachable controller has its own gyro and accelerometer, but SteamOS
never switches them on. This plugin does, and publishes the data through a
**DSU ("CemuHook") motion server** – the motion standard emulators use. That
gives you three independent motion devices:

| DSU slot | Device | Motion comes from |
|---|---|---|
| **0** | Whole handheld | Right controller, else left, else the tablet's own sensor (selectable) |
| **1** | Left controller | Left controller's sensor |
| **2** | Right controller | Right controller's sensor |

So a detached controller can be swung like a Wii Remote (Wii Sports in
Dolphin), the pair can act as two Joy-Cons with separate motion (Switch
Sports), and slot 0 covers ordinary handheld gyro aiming.

Every slot also carries the buttons and sticks that sit on that controller,
so an emulator can use a slot as a complete controller if you want.

## Install

1. Install [Decky Loader](https://decky.xyz) if you have not already.
2. Decky → settings (gear) → **General → Developer mode** on.
3. **Developer → Install plugin from ZIP file** and pick
   `release/decky-legion-go-gyro.zip` from this folder (copy it to the Legion
   Go first), or use **Install plugin from URL** with the zip's download link.
4. Open the Quick Access menu → Decky → **Legion Go Gyro**.

It needs no further setup: once loaded it finds the controllers, switches
their sensors on and starts the server on `127.0.0.1:26760`. Move a
controller and its bar in the panel moves with it.

Press **Calibrate** once and leave the controllers alone for a few
seconds – on a table is best. It finishes as soon as it sees a calm second;
if the sensors never get perfectly quiet it uses the calmest second it saw,
so it only fails if a controller was being swung the whole time. After
that, drift is removed automatically whenever a controller rests.

## Simple and advanced view

The panel opens in the **simple view**: one on/off switch, a status line
and motion bar per controller, one Calibrate button and the address to
type into your emulator. Everything runs on the default settings.

Turn on **Advanced options** (bottom of the panel) for per-slot tuning,
the handheld motion source and the server options described below. The
choice is remembered.

Switching back to the simple view only hides those options – anything you
changed stays in effect. When that is the case the simple view says so and
offers **Use default settings**, which puts every option back to default
in one tap (your calibration is kept).

## Emulator setup

Server address everywhere: **`127.0.0.1`**, port **`26760`**.
Menu names differ slightly between emulator versions.

### Dolphin (Wii Sports and other Wii games)

1. *Controllers → Alternate Input Sources →* enable **DSU Client**, add a
   server `127.0.0.1` / `26760`.
2. *Wii Remote 1 → Emulated Wii Remote → Configure.*
3. Open **Motion Input**, choose the device `DSUClient/2/…` (right
   controller; `/1/` is the left, `/0/` the handheld) and press **Default**.
   That maps accelerometer and gyro; enable *Point → Enable* there if you
   want the pointer driven by motion as well.
4. Map the buttons on **General and Options** – either from the same DSU
   device (A/B/X/Y arrive as Cross/Circle/Square/Triangle) or from your
   normal gamepad.
5. For a Nunchuk, configure the extension's motion with `DSUClient/1/…`
   (left controller). For two players, use slot 1 for one Wii Remote and
   slot 2 for the other.

Hold the controller like a remote: buttons facing up, top edge (bumper)
pointing at the screen.

### Switch emulators (Switch Sports and other motion games)

- **Ryujinx / Ryubing:** *Settings → Input → Configure* the player (Joy-Con
  pair) → **Motion** → enable motion, tick **Use CemuHook compatible
  motion**, host `127.0.0.1`, port `26760`, **Controller Slot 1**. Leave
  **Mirror Input** unticked and set **Right JoyCon Slot 2** so each Joy-Con gets
  its own controller's motion.
- **Eden / Citron / other yuzu forks:** *Configure → Controls → Advanced →
  Motion/Touch → Configure*, add the server `127.0.0.1:26760`. Back on the
  player page click **Motion 1**, then shake the left controller; click
  **Motion 2** and shake the right one.

### Cemu, RPCS3, PCSX2, …

Any emulator with a DSU / CemuHook client works the same way: add the
server and pick the slot.

## Settings (advanced view)

Per slot:

- **Enabled** – switch a slot off if you do not need it. A controller's
  sensor is only powered while something uses it.
- **Calibrate** – measures the resting offset of that sensor.
- **Sensitivity**, **Deadzone** – scale the rotation / ignore tiny motion.
- **How it is held** – tells the plugin if you hold the controller turned
  (for example sideways) instead of upright as it sits on the tablet.
- **Invert pitch / yaw / roll** – flip a single axis.
- **Motion source** (handheld slot) – Automatic, right, left, or tablet.

Global: auto-calibration, server port, and whether other devices on your
network may connect (off by default; the server is local-only).

## Troubleshooting

- **"Legion Go controllers not found"** – the controller receiver is not
  visible. Wake the controllers (press a button) and check they work in
  Steam.
- **"waiting for sensor…"** – the controller is connected but not sending
  motion yet; the plugin retries every 2 seconds. Detach/re-attach the
  controller or toggle the slot if it stays that way.
- **"Port 26760 is already used"** – another motion server is running
  (for example one bundled with EmuDeck). Stop it or choose another port
  here and in the emulator.
- **Motion feels too strong or too weak in a game** – Advanced options →
  that controller → Tuning → Sensitivity.
- **An axis moves the wrong way** in one game – use the invert switches.
- **Check without an emulator:** run `python3 tools/dsu_monitor.py` in a
  terminal on the device; it prints the live data of all slots.
- The backend log is in `~/homebrew/logs/Legion Go Gyro/`.

## Scope and limits

- Built for the **Legion Go (1st gen, 83E1)**. The Legion Go 2 controllers
  speak the same protocol and should work, but that is untested.
- Developed against the documented controller protocol and verified with
  simulated controllers (see `tests/`); it has not been run on real
  hardware by the author. Axis directions follow the mappings used by
  Handheld Daemon – if one is off on your unit, the invert switches fix it.
- This plugin feeds **emulators (DSU)**. It does not add gyro to Steam
  Input for native Steam games.
- The tablet sensor is polled at a lower rate than the controller sensors
  and is only used when selected or when no controller is connected.
- If Handheld Daemon (Bazzite) is managing the controllers at the same
  time, both will switch the controller sensors on and off; use one of them
  for gyro.

## How it works

The controller receiver exposes a vendor HID interface that streams a
60-byte state report with both controllers' IMU data. The Python backend
(runs as root, standard library only) opens it next to SteamOS's own input
stack, sends the two commands that enable each controller's IMU, converts
the samples to the DualShock axis convention and serves them as DSU slots.
On unload it switches the controller IMUs off again.

```
py_modules/lgo_gyro/
  legion_hid.py   controller protocol + device discovery
  motion.py       calibration, tuning, axis conversion
  dsu.py          DSU / CemuHook protocol
  tablet.py       tablet sensor (IIO)
  service.py      the service loop
  settings.py     settings schema + storage
src/              Decky UI (React)
```

## Development

```bash
pnpm install
python3 -m unittest discover -s tests -v   # backend tests
pnpm run build                             # frontend -> dist/
bash scripts/package.sh                    # -> release/decky-legion-go-gyro.zip
```

## Credits

Protocol knowledge comes from the reverse-engineering work in
[Handheld Daemon](https://github.com/hhd-dev/hhd) and
[InputPlumber](https://github.com/ShadowBlip/InputPlumber). DSU protocol
documentation: <https://v1993.github.io/cemuhook-protocol/>.

MIT licensed.
