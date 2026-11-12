<div align="center">

# Legion Go Toolbox

**The Legion Go's hardware controls, together in the Steam Quick Access menu.**

Power and automatic TDP, fan curve, stick and power lights, charge limit,
button remapping, swapped menu buttons and per-controller gyro for emulators.

**[User manual (PDF)](docs/Legion-Go-Toolbox-Manual.pdf)** · [Features](#features) · [Requirements](#requirements) · [Installation](#installation) · [Using the tabs](#using-the-tabs) · [Troubleshooting](#troubleshooting) · [How it works](#how-it-works) · [Credits](#credits)

</div>

---

## Features

One Decky plugin, six tabs (switch with **LB / RB**). Nothing is changed on
your device until you switch a feature on, and every feature that your system
cannot do says so, with the reason, instead of pretending.

| Tab | What you can do |
|---|---|
| **Home** | See every module at a glance, limit charging to 80 %, reset |
| **Power** | Pick a power mode or set the TDP in watts, per game if you like. **Auto TDP** finds the lowest power that still holds a frame rate you choose |
| **Fan** | Draw a ten-point fan curve, presets, full speed, live temperature |
| **Lights** | Stick lights: effect, colour, brightness, speed. Power-button light on/off, and off while sleeping |
| **Controls** | Swap View/Menu with the Legion buttons, remap Y1 Y2 Y3 M2 M3, switch the touchpad off |
| **Gyro** | Gyro in ordinary Steam games (handheld), and each detached controller as its own motion device for Dolphin, Switch emulators and others (DSU / CemuHook) |

Saved settings are applied when SteamOS starts, after sleep, and when a
controller reconnects, also while the menu is closed.

## Requirements

| | |
|---|---|
| Device | Lenovo **Legion Go** (83E1). Built and tested for this model |
| System | SteamOS with [Decky Loader](https://decky.xyz) |

### What needs what

| Feature | Works when |
|---|---|
| Gyro, stick lights, remapping, touchpad | The controllers are connected |
| Power light | Always on a Legion Go (three ways to reach it are tried) |
| Charge limit | SteamOS exposes the battery's *Long Life* mode (current versions do) |
| TDP and power modes | SteamOS has the Lenovo power-limit driver (current versions do) |
| Auto TDP | The above, in Gaming Mode (the frame rate comes from gamescope) |
| Swapped menu buttons, gyro in Steam games | InputPlumber is running (it is on SteamOS) |
| **Fan curve** | **The `acpi_call` kernel module is installed. SteamOS does not include it** |

**About the fan curve.** The Legion Go only accepts a fan table through a
Lenovo firmware call, and Linux can only make that call with the `acpi_call`
module. Installing a kernel module on SteamOS means changing the read-only
system and repeating that after every system update, so this plugin
deliberately does not do it for you. If the module is present (for example
installed with Legion Center's *Install Fan Support*, or on Bazzite), the
curve editor appears by itself. Without it the fan follows the firmware's own
curve, which is safe.

## Installation

1. Install [Decky Loader](https://decky.xyz).
2. If you have **Legion Go Gyro** installed, uninstall it first (this plugin
   contains it; its tuning and calibration are carried over automatically).
   Do the same for other plugins that control the same things (SimpleDeckyTDP,
   Legion Center, HueSync, DeckyPlumber): two plugins steering the same
   hardware will fight.
3. Decky → settings → **General → Developer mode** on.
4. **Developer → Install plugin from ZIP file** → `release/legion-go-toolbox.zip`.
5. Open the Quick Access menu → Decky → **Legion Go Toolbox**.

## Using the tabs

### Power

- **Manage power** hands TDP control to the plugin. Turn off Steam's own
  *TDP limit* in the Performance menu, otherwise both keep overwriting each
  other.
- **Power mode**: *Custom* gives you a watt slider; the other entries are the
  firmware's own modes (Quiet, Balanced, Performance).
- **Auto TDP for a frame rate**: choose a target (for example 60 fps). The
  plugin measures the game's real frame rate and
  - adds power immediately when the frame rate drops below the target,
  - tries one watt less whenever the target has been held for a few seconds,
  - remembers settings that turned out too low, so it settles instead of
    bouncing.

  *Maximum TDP* and *Minimum TDP* are the limits it stays within. It works
  best with a frame limit (in the game or Steam) equal to the target. In
  menus and loading screens it simply keeps the last setting.
- **Separate settings per game** stores a profile for the running game.
- **CPU boost** off runs cooler for a small performance cost.

### Fan

Needs `acpi_call` (see above). The firmware keeps control of the fan and of
overheating protection; the plugin only changes the table it follows.

- The curve always rises with temperature and never goes below Lenovo's own
  minimum for the Legion Go (44 % at 10 °C rising to 100 % at 90 °C, the same
  limit Handheld Daemon enforces). *Silent* is exactly that minimum.
- The firmware throws away *full speed* and the custom curve whenever the
  power mode changes - whether Steam, another tool or this plugin changes it.
  The plugin reads both back every 5 seconds (and one second after each of
  its own power changes) and puts them back. The Fan tab shows how often it
  had to.
- The firmware uses a custom table only in the *Custom* power mode.

### Lights

Stick lights are stored in the controllers. *Off while sleeping* switches the
power light off when the device suspends and back on when it wakes.

### Controls

- **Swap menu buttons** with two layouts:

  | Layout | Press | Gets |
  |---|---|---|
  | **Steam ↔ Menu, QAM ↔ View** (default) | Menu / View | Steam / Quick Access |
  | | Legion L / Legion R | Menu / View |
  | Steam ↔ View, QAM ↔ Menu | View / Menu | Steam / Quick Access |
  | | Legion L / Legion R | View / Menu |

  Done with an InputPlumber profile on top of the active one, and restored
  when you switch it off.
- **Remap back buttons** uses Lenovo's own remapping, stored in the
  controllers. Steam still sees the back buttons as extra buttons too, so
  leave them unassigned in Steam Input for games where you use this.

### Gyro

**In Steam games.** Steam only offers gyro for controllers that have one,
and InputPlumber on its own presents the Legion Go as an Xbox controller.
This plugin presents it as a **Steam Deck controller** by default (also
selectable: DualSense, DualSense Edge, or back to Xbox), so Steam shows its
normal gyro options for every game (Steam button → Controller settings →
Gyro).

Choose a type for **all games**, and optionally a different one for the
game that is running: it is remembered, switched to when that game starts
and back when it closes. *Games with their own type* lists them; select one
to remove it.

**Button symbols**: most games show Xbox symbols whatever type is picked,
because Steam Input hands them an Xbox-style controller. Games with their own
PlayStation support may show PlayStation symbols with a DualSense (most offer
a button-prompt setting); Steam's own menus always match the type. The Steam
Deck type keeps A/B/X/Y lettering.

This uses the motion sensor inside the tablet,
so it works with the controllers attached; it cannot use a detached
controller's own sensor. Switching the type reconnects the controller once,
so do it before starting a game. Controller layouts are stored per
controller type, so a game may start with that type's default layout.

**In emulators.** Add a DSU / CemuHook motion server in your emulator: `127.0.0.1`, port
`26760`. Slot 0 = whole handheld, slot 1 = left controller, slot 2 = right
controller.

- **Dolphin**: *Controllers → Alternate Input Sources → DSU Client*, then in
  the Wii Remote's *Motion Input* tab choose `DSUClient/2/…` and press
  *Default*.
- **Ryujinx**: *Input → Motion → Use CemuHook compatible motion*, slot 1 and
  *Right JoyCon Slot* 2.
- **Eden / Citron / yuzu forks**: *Controls → Advanced → Motion/Touch →
  Configure*, add the server, then map *Motion 1* / *Motion 2* by shaking a
  controller.

Press **Calibrate** once and leave the controllers alone for a few seconds.

## Troubleshooting

- **A feature says "not available"** – the text under it gives the reason.
- **TDP jumps back** – Steam's own TDP limit is still on, or another TDP
  plugin is installed.
- **Auto TDP says "waiting for the game to draw frames"** – no game is in
  focus, or the game is paused.
- **Auto TDP stays at the maximum** – the target frame rate is not reachable
  within your maximum TDP; raise the maximum or lower the target.
- **Stick lights or remapping report "controllers not found"** – wake the
  controllers by pressing a button.
- **Menu buttons swap back** – InputPlumber restarted; the plugin re-applies
  the swap within a few seconds.
- Logs: `~/homebrew/logs/Legion Go Toolbox/`.

## Scope and limits

- Used on a Legion Go (83E1), and verified with simulated hardware (127
  automated tests). Every module reads the device state back after a change
  and reports it when a change did not take.
- Legion Go 2 and Legion Go S are recognised for the parts that are known to
  be identical (power limits, charge limit, power light); the rest stays off.
- Gyro in Steam games comes from the tablet's sensor through SteamOS's own
  input layer; per-controller gyro is for emulators only.

## How it works

```
main.py                     Decky entry point
py_modules/lgo_gyro/        gyro: controller HID reader + DSU server
py_modules/lgo_toolbox/
  hub.py                    applies saved settings, re-applies after sleep
  power.py                  firmware power limits, Auto TDP
  fps_probe.py              game frame rate from gamescope
  fan.py  acpi.py           fan table through Lenovo's firmware call
  powerled.py               power-button light (EC)
  battery.py                charge limit
  controller.py             stick lights, remapping, touchpad
  plumber.py                swapped menu buttons (InputPlumber)
src/                        the tabs (React)
tests/                      backend tests with simulated hardware
```

The backend is plain Python (standard library only) and runs as root, as
Decky plugins with hardware access do.

## Development

```bash
pnpm install
python3 -m unittest discover -s tests     # backend tests
pnpm run build                            # UI -> dist/
bash scripts/package.sh                   # -> release/legion-go-toolbox.zip
```

The PDF manual and README live in `docs/`. To rebuild them after editing
`docs/src/manual.html` or this README (needs Node with `marked` and
Playwright):

```bash
cd docs/src
node readme.cjs ../../README.md readme.html print.css
node render.cjs && mv *.pdf ..
```

## Credits

This plugin is its own code, but it stands on what these projects worked out
about the hardware:

- [Handheld Daemon](https://github.com/hhd-dev/hhd) and
  [hwinfo](https://github.com/hhd-dev/hwinfo) – controller protocol, Lenovo
  firmware methods
- [Legion Center](https://github.com/InnoVision-Games/legion-center) /
  LegionGoRemapper – remapping, fan table, charge limit
- [HueSync](https://github.com/honjow/HueSync) – power light locations
- [SimpleDeckyTDP](https://github.com/aarron-lee/SimpleDeckyTDP) – Lenovo
  power-limit interface
- [DeckyPlumber](https://github.com/aarron-lee/DeckyPlumber) and
  [InputPlumber](https://github.com/ShadowBlip/InputPlumber) – menu-button
  swap
- [Panel de Control](https://github.com/Hooandee/panel-de-control) – the idea
  of reading frame times from gamescope's control interface
- [decky-lsfg-vk](https://github.com/xXJSONDeruloXx/decky-lsfg-vk) – tabs in
  the Quick Access menu
- [Legion Go 2 Companion](https://github.com/Rayekkk/LegionGo2Companion) –
  the module-overview layout

MIT licensed.
