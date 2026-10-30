# Epomaker Screen

[![CI](https://github.com/Fasad1987/epomaker-screen/actions/workflows/ci.yml/badge.svg)](https://github.com/Fasad1987/epomaker-screen/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Use the screens of **Epomaker keyboards** on Linux without a browser: a KDE Plasma tray app and a
command-line tool, with one driver per screen family.

- **Keyboard clock** set automatically on connect, after suspend and every few hours.
- **Pictures and GIFs** on the keyboard screen, without Epomaker Hub or the vendor app.
- **Widgets** laid out with the mouse for the connected keyboard's screen size: clock, date,
  CPU/RAM/temperature/network (text, bar or graph), now playing (MPRIS), weather (Open-Meteo, no API key), output
  of any command (a layout from someone else can run commands too: the app asks before opening one), images,
  panels and day progress. **Live mode** keeps them up to date.
- **The keyboard's own system page** (TFT keyboards such as the RT100 Pro) fed with CPU, RAM and disk values.
- **Typing the disk password** on a USB keyboard at boot: a script that puts the keyboard drivers into the
  initramfs (make-initrd on ALT Linux, dracut, initramfs-tools, mkinitcpio), plus advice for the GRUB prompt.
- A **udev rule** that also lets Epomaker Hub in Chromium open the keyboard.

## Supported keyboards

| Keyboard                                       | Screen  | Family | Status                              |
| ---------------------------------------------- | ------- | ------ | ----------------------------------- |
| RT82                                           | 240×135 | RDM    | verified                            |
| QK108, RT98                                    | 240×136 | RDM    | same controller, not yet tried here |
| RT100 Pro                                      | 240×240 | TFT    | verified (firmware 0x0109)          |
| RT75, RT85, Glyph, RT100, HE65 Mag, Dynatab75X | various | TFT    | from the vendor catalog, not tried  |

Two screen families are known (details in [docs/protocols](docs/protocols/)):

- **RDM** ([rdm.md](docs/protocols/rdm.md)): a separate screen controller behind the keyboard's internal hub
  stores QGIF animations in its own flash. Frames can be streamed into it, so live widgets update every second.
- **TFT** ([tft.md](docs/protocols/tft.md)): the keyboard firmware takes raw RGB565 pixels into five picture
  banks and draws its own clock and system pages. Live widgets redraw a bank when they change.

`epomakerctl list` shows what is connected; the editor follows the keyboard's screen size. If yours is not
listed, please open an issue with the output of `epomakerctl probe --identify`.

## Requirements

- Linux with Python 3.9+ and PyQt6 or PySide6
- The keyboard connected **with a USB cable**: the screens are not reachable over Bluetooth or 2.4 GHz.

```bash
# ALT Linux (no sudo by default: run root commands with su -c)
su -c 'apt-get install python3-module-PyQt6'
# Fedora
sudo dnf install python3-pyqt6
# Debian/Ubuntu
sudo apt install python3-pyqt6
```

Optional: `playerctl` for the "Now playing" widget (`gdbus` is used without it).

## Installation

```bash
git clone https://github.com/Fasad1987/epomaker-screen.git
cd epomaker-screen
scripts/install.sh
```

The script installs into `~/.local`, adds **Epomaker Screen** to the menu and to autostart (in the tray), and needs
root once, for the udev rule: through `sudo`, or `su` (root password) when sudo is missing or not allowed. It
also removes an older "RT82 Screen" installation; its settings are carried over.

You can also start it as root, which is handy on ALT Linux:

```bash
su -c 'scripts/install.sh'
```

The udev rule is installed directly and the app is installed for the user who ran `su` or `sudo` (pass
`--user NAME` if it cannot tell). Use `su -c`, not `su -`, so that the relative path still works.

Then **reconnect the keyboard cable** and check:

```bash
epomakerctl list
```

The vendor interface (`FF60` on the RT82 family, `FFFF:0002` on TFT keyboards) must show `access=yes`. The plain
keyboard interfaces stay `access=NO` on purpose: the rule does not let programs read your keystrokes.

Options: `--service` installs only a headless clock-sync service (systemd `--user`) instead of the tray app;
`--no-udev` skips the rule. To remove everything: `scripts/uninstall.sh`.

## Usage

### The app

- **Editor**: a preview of the connected keyboard's screen. Click a widget to select it, drag to move, drag its
  bottom-right corner to resize; arrow keys nudge by 1 px (Shift+arrows resize), Delete removes it. Each screen
  size has its own layout, saved automatically in `~/.config/epomaker-screen/`.
- **Live mode** keeps the widgets on the keyboard up to date. See [docs/live-mode.md](docs/live-mode.md).
- **Send snapshot** renders the layout once and stores it as a picture.
- **Pictures/GIFs…** uploads pictures and animations; long animations are thinned out to what Epomaker's
  software allows for the keyboard (can be turned off in Settings).
- **Set clock** sets the keyboard clock now.
- **Pause** releases the keyboard, e.g. for Epomaker Hub. A screen talks to one program at a time.
- **File → Settings**: preferred keyboard, tray behaviour, clock sync, frame limit, live mode and the TFT system
  page.

### Command line

```bash
epomakerctl list                       # Epomaker keyboards, interfaces and access rights
epomakerctl probe --identify           # detailed report for bug reports
epomakerctl info                       # screen size and capabilities
epomakerctl sync-time                  # set the keyboard clock
epomakerctl upload cat.gif             # pictures and animations
epomakerctl upload photo.jpg --bank 3  # TFT keyboards: one still into bank 3, keeping the others
epomakerctl sysinfo --watch            # TFT keyboards: feed the keyboard's own system page
epomakerctl preview out.png --size 240x240
epomakerctl layout                     # render the layout once and upload it
epomakerctl live                       # live mode in the terminal (Ctrl+C to stop)
epomakerctl watch                      # background clock sync (what the systemd service runs)
epomakerctl reset-screen               # TFT keyboards: clear pictures and animation, set the clock, stop feeds
epomakerctl reset-screen --erase       # TFT keyboards: remove all pictures and the animation (asks first)
epomakerctl lab pattern                # TFT keyboards: protocol experiments, see docs/hardware-testing.md
```

There is no command to read pictures back, so the factory picture cannot be saved first. `reset-screen` writes
a black animation and black pictures into every bank, sets the clock and turns off the app's live mode and system page feed.
`reset-screen --erase` sends Epomaker's "erase screen flash" command after you type `ERASE`. On the RT100 Pro
(firmware 0x0109) it removes every stored picture and the animation, so only the keyboard's own pages (clock,
system) are left, as on a new keyboard; it takes half a second. On other models its effect is not verified yet: it
might also erase the graphics of the clock and system pages, which only Epomaker's own software could put back. Everything on the
computer side is removed by `scripts/uninstall.sh`.

With several keyboards connected, pick one with `--device rt100pro` (model key), `--device 3151:5002` (USB id) or
its USB path.

## Typing the password at boot

There are two separate prompts, fixed in different places. Details: [docs/boot-keyboard.md](docs/boot-keyboard.md).

1. **Disk password (initramfs).** Linux handles the keyboard here, but only with the drivers inside the
   initramfs. On ALT Linux it may lack `usbhid` and the USB host driver. Connect the keyboard with a cable and run:

   ```bash
   su -c 'scripts/boot-keyboard.sh'
   ```

   `--check` only reports, `--undo` reverts.

2. **GRUB menu and password.** GRUB runs before Linux and uses the firmware's keyboard support. Turning off
   **Fast Boot** in the UEFI setup usually fixes it.

## Epomaker Hub in Chromium

Chromium lists the keyboard over udev but cannot open `/dev/hidraw*` without permission, so Hub fails to
connect (`chrome://device-log` shows `FILE_ERROR_ACCESS_DENIED`). The udev rule from `scripts/install.sh` fixes
this. Press **Pause** in Epomaker Screen before using Hub, and close Hub tabs afterwards.

## Troubleshooting

See [docs/troubleshooting.md](docs/troubleshooting.md). In short: if an RT82-family screen stops responding,
unplug the cable, switch the keyboard off with its power switch for ~10 s and plug it back in; the app detects
this and waits for the re-plug. To report a problem, attach `epomakerctl probe --identify` and the output of
`su -c 'scripts/diagnose.sh'`.

## Project layout

```
src/epomaker_screen/
  hid.py            hidraw discovery through sysfs, report descriptors, I/O and feature reports
  models.py         known keyboards: USB ids, internal ids, screen sizes, verification status
  devices.py        finding connected keyboards and picking their drivers
  drivers/base.py   the driver interface: capabilities, sessions, errors, the one-host lock
  drivers/rdm/      RT82 family: protocol, QGIF encoder, flash uploads, live streaming
  drivers/tft/      RT100 Pro family: packet codec, picture banks, refresh-based live mode
  service.py        background worker: hotplug, clock sync, live mode, system page, jobs
  widgets.py        widgets and layout rendering (QPainter), any screen size
  sources.py        widget data: /proc, hwmon, MPRIS, Open-Meteo, commands
  gui.py            KDE Plasma app: editor and tray
  cli.py, lab.py    epomakerctl and its protocol experiments
data/               udev rule, desktop entries, systemd unit
scripts/            install/uninstall, boot keyboard fix, diagnostics
tests/              tests and simulators of both screen families
docs/               protocols, live mode, hardware testing, boot keyboard, troubleshooting
```

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md), including how to add a keyboard.

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[pyqt6,dev]'
pytest
ruff check . && ruff format --check .
```

## Credits

Screen protocols were reverse-engineered by [guysoft/rt82display](https://github.com/guysoft/rt82display) and
[wyattades/keyscreen](https://github.com/wyattades/keyscreen) (RDM family) and by
[ramarivera/epomaker-driver-linux](https://github.com/ramarivera/epomaker-driver-linux) (TFT family, from
Epomaker's own software, MIT). This project is an independent implementation; the TFT model catalog values come
from the last project.

## License

[MIT](LICENSE)
