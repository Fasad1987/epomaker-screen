# RT82 Screen

[![CI](https://github.com/Fasad1987/epomaker-rt82-plasma/actions/workflows/ci.yml/badge.svg)](https://github.com/Fasad1987/epomaker-rt82-plasma/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

Use the screen of the **Epomaker RT82** keyboard on Linux without a browser: a KDE Plasma tray app and a
command-line tool.

- **Keyboard clock** set automatically on connect, after suspend and every few hours.
- **Pictures and GIFs** on the keyboard screen (up to 3 screens), without Epomaker Hub.
- **Widgets** laid out with the mouse in an editor: clock, date, CPU/RAM/temperature/network (text, bar or
  graph), now playing (MPRIS), weather (Open-Meteo, no API key), output of any command, images, panels and day
  progress. **Live mode** keeps them up to date on the keyboard.
- **Typing the disk password on the RT82** at boot: a script that puts the keyboard drivers into the initramfs
  (make-initrd on ALT Linux, dracut, initramfs-tools, mkinitcpio), plus advice for the GRUB prompt.
- A **udev rule** that also lets Epomaker Hub in Chromium open the keyboard.

> **Status: alpha.** Version 0.1 ran on a real RT82 (ALT Linux 11, KDE Plasma 6): clock sync, picture upload
> and live mode worked, but repeated uploads froze the screen controller. Version 0.2 uploads without the
> controller's download mode to avoid that; this new path is so far verified only against the simulator in
> `tests/`. It talks to undocumented hardware, so read [Troubleshooting](docs/troubleshooting.md) first.

## Requirements

- Linux with a Python 3.9+ and PyQt6 or PySide6
- The keyboard connected **with a USB cable**: the screen controller is not reachable over Bluetooth or 2.4 GHz,
  with the official software too.

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
git clone https://github.com/Fasad1987/epomaker-rt82-plasma.git
cd epomaker-rt82-plasma
scripts/install.sh
```

The script installs into `~/.local`, adds **RT82 Screen** to the menu and to autostart (in the tray), and needs
root once, for the udev rule: through `sudo`, or `su` (root password) when sudo is missing or not allowed.

You can also start it as root, which is handy on ALT Linux:

```bash
su -c 'scripts/install.sh'
```

The udev rule is installed directly and the app is installed for the user who ran `su` or `sudo` (pass
`--user NAME` if it cannot tell). Use `su -c`, not `su -`, so that the relative path still works.

Then **reconnect the keyboard cable** and check:

```bash
rt82ctl list
```

The interface with `usage_page=0xFF60` must show `access=yes`. The other two are the plain keyboard
interfaces and stay `access=NO` on purpose: the rule does not let programs read your keystrokes.

Options: `--service` installs only a headless clock-sync service (systemd `--user`) instead of the tray app;
`--no-udev` skips the rule. To remove everything: `scripts/uninstall.sh`.

## Usage

### The app

- **Editor**: the left side is a 3× preview of the 240×135 screen. Click a widget to select it, drag to move,
  drag its bottom-right corner to resize; arrow keys nudge by 1 px (Shift+arrows resize), Delete removes it.
  The layout is saved automatically to `~/.config/rt82-plasma/layout.json`.
- **Live mode** keeps the widgets on the keyboard up to date; edits show up on the keyboard right away. See
  [how it works](docs/live-mode.md).
- **Send snapshot** renders the layout once and stores it as a picture.
- **Pictures/GIFs…** uploads up to 3 files (GIF, PNG, JPEG, WebP, `.qgif`). Animations are thinned out to 40
  frames, Epomaker Hub's limit (can be turned off in Settings).
- **Set clock** sets the keyboard clock now.
- **Pause** releases the keyboard, e.g. for Epomaker Hub. The screen controller talks to one program at a time.
- **File → Settings**: whether closing the window keeps the app in the tray, clock sync, frame limit and live
  mode parameters.

On the keyboard: **Fn + Right Shift** shows the GIF page, where pictures and widgets appear; **Fn + Enter**
cycles the screen pages.

### Command line

```bash
rt82ctl list                       # RT82 interfaces and access rights
rt82ctl sync-time                  # set the keyboard clock
rt82ctl info                       # screen size and link type
rt82ctl upload cat.gif             # up to 3 files: GIF/PNG/JPEG/WebP/.qgif
rt82ctl upload a.png b.gif --fit cover --max-frames 0
rt82ctl preview out.png            # render the layout to a PNG
rt82ctl layout                     # render the layout once and upload it
rt82ctl live                       # live mode in the terminal (Ctrl+C to stop)
rt82ctl watch                      # background clock sync (what the rt82-watch service runs)
```

`--file my-layout.json` uses another layout file.

## Typing the password at boot

There are two separate prompts, fixed in different places. Details: [docs/boot-keyboard.md](docs/boot-keyboard.md).

1. **Disk password (initramfs).** Linux handles the keyboard here, but only with the drivers inside the
   initramfs. On ALT Linux it may lack `usbhid` and the USB host driver. Connect the RT82 with a cable and run:

   ```bash
   su -c 'scripts/boot-keyboard.sh'
   ```

   `--check` only reports, `--undo` reverts.

2. **GRUB menu and password.** GRUB runs before Linux and uses the firmware's keyboard support. Turning off
   **Fast Boot** in the UEFI setup usually fixes it.

## Epomaker Hub in Chromium

Chromium lists the keyboard over udev but cannot open `/dev/hidraw*` without permission, so Hub fails to
connect (`chrome://device-log` shows `FILE_ERROR_ACCESS_DENIED`). The udev rule from `scripts/install.sh` fixes
this. Press **Pause** in RT82 Screen before using Hub, and close Hub tabs afterwards.

## Troubleshooting

See [docs/troubleshooting.md](docs/troubleshooting.md). In short: if the screen stops responding, unplug the
cable, switch the keyboard off with its power switch for ~10 s and plug it back in. RT82 Screen detects this
state and waits for the re-plug. To report a problem, attach the output of `su -c 'scripts/diagnose.sh'`.

## How it works

The RT82 is two USB devices: the keyboard (`36b0:30a3`) and, behind its internal hub, a screen controller
(`1919:1919`) that appears on request. The screen plays animations stored in its flash in a tile-compressed
format (QGIF). This project implements the protocol from scratch in Python, including the QGIF encoder. See
[docs/protocol.md](docs/protocol.md).

```
src/rt82/        Python package
  hidraw.py      finding /dev/hidraw* through sysfs, HID descriptors, I/O
  protocol.py    ids, commands, packets, slot table, clock format
  device.py      keyboard and screen controller: clock, uploads, erase, recovery
  qgif.py        QGIF encoder/decoder
  live.py        live mode: streaming frames into flash
  service.py     background worker: hotplug, clock sync, live mode, jobs
  widgets.py     widgets and layout rendering (QPainter)
  sources.py     widget data: /proc, hwmon, MPRIS, Open-Meteo, commands
  gui.py         KDE Plasma app: editor and tray
  cli.py         rt82ctl
data/            udev rule, desktop entries, systemd unit
scripts/         install/uninstall, boot keyboard fix, diagnostics
tests/           tests and a simulated screen controller
docs/            protocol, live mode, boot keyboard, troubleshooting
```

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md).

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[pyqt6,dev]'
pytest
ruff check . && ruff format --check .
```

## Credits

The screen protocol was reverse-engineered by [guysoft/rt82display](https://github.com/guysoft/rt82display) and
[wyattades/keyscreen](https://github.com/wyattades/keyscreen); this project is an independent implementation.

## License

[MIT](LICENSE)
