# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

### Fixed
- TFT live mode keeps its next redraw time across the short slices the service runs it in, and holds back
  system page packets for 5 s after a picture, while the keyboard is still writing it.
- Turning on live mode with a TFT keyboard explains once why the picture changes only every few minutes and that
  the keyboard's system page is the real-time view; the keyboard card says the same.

### Added
- The app has a new layout: widget list, preview and properties side by side, a card with the connected keyboard
  and its state, the log as a panel that can be hidden, and icons that fall back to Qt's own where the desktop's
  icon theme lacks them (GNOME).
- Editor: undo and redo, zoom (fit or 1–4×), a grid, snapping to 4 px steps and the screen centre (Alt: off),
  alignment commands, hiding widgets from the list, Ctrl+D to duplicate; zoom, grid and snap are remembered.
- Keyboard menu with what `epomakerctl` can do: a picture into one bank, switching the screen language, clearing
  pictures, erasing the screen memory (with the model's own notice) and a keyboard info window.
- Widgets: analog clock, ring style for the system monitor, weather symbols, progress of the week, month or year,
  a countdown widget and an optional text shadow.

### Changed
- `reset-screen --erase` (`0xAC`) is verified on the RT100 Pro: it removes every stored picture and the animation
  and keeps the clock and system pages. Its warning is now model-specific; other models keep the full warning.

### Security
- `scripts/diagnose.sh` no longer copies GRUB password lines (including `password_pbkdf2` hashes) into the
  report people attach to issues.
- Install, uninstall, boot-keyboard and diagnose scripts run root commands with the system `PATH` only, so a
  program in a user-writable directory such as `~/.local/bin` cannot stand in for `install` or `udevadm`.
- Opening a layout file that contains "command" widgets asks first in the app, and `epomakerctl --file` needs
  `--allow-commands`: such a layout runs its shell commands every few seconds.
- Without `XDG_RUNTIME_DIR` the lock file goes to `~/.cache/epomaker-screen` instead of `/tmp`, and a symlinked
  lock file is refused.

### Changed
- TFT pixel packets go 1 ms apart instead of the vendor's 2 ms: a 56-frame RT100 Pro animation in 149 s instead
  of 265 s. `--chunk-delay MS` changes it; no delay corrupted pictures in a row and long animations.
- `upload --fit` works without a value (`contain`).
- `lab flow` reads the keyboard's replies during a transfer.

- RT100 Pro animations are limited to 47 frames: `lab frames` measured 6 MiB of screen memory with the picture
  banks right after frame 47 (the vendor app assumes 7 MiB and 56 frames, which overwrites the banks).

### Added
- The RT100 Pro is verified on hardware (firmware 0x0109).
- `lab frames` uploads numbered frames to measure how many fit and where the picture banks are.
- `epomakerctl reset-screen` for TFT keyboards: a black animation and black pictures in every bank, the clock set, the app's live mode and
  system page feed turned off. `--erase` sends the vendor's screen flash erase (`0xAC`) after typing `ERASE`.
- TFT keyboards: a busy keyboard gets about 10 s to accept an upload instead of 2 s, and the error names the
  attempts and the last reply.
- `lab banks` logs timings, carries on past a refused bank and takes `--pause`; `lab partial` also tries a
  full-height strip and a page switch.
- TFT live mode redraws every 15 minutes by default and at least every 5 (it was 1 minute): each update rewrites
  flash, and once a minute would wear it out within months.
- The "send only the changed part" setting is gone from the app: an RT100 Pro draws the part on black instead of
  patching the picture.

### Fixed
- `boot-keyboard.sh` could report a module as missing from a large initrd although it was there
  (`echo | grep -q` under `pipefail`).

## [0.3.0] - 2026-10-30

The project is now **Epomaker Screen**, for Epomaker keyboards in general.

### Added
- Driver architecture: one package per screen family under `drivers/`, a model catalog and device detection
  that picks the driver for each connected keyboard.
- TFT family driver (RT100 Pro, RT75, RT85, Glyph, RT100, HE65 Mag, Dynatab75X): identification by internal id,
  stills into five banks, animations, clock, the keyboard's own system page, language toggle, partial updates
  and refresh-based live mode. Only allow-listed commands can be sent. Not yet verified on hardware.
- `epomakerctl probe` (read-only device report) and `epomakerctl lab` (experiments to verify and speed up the
  TFT protocol on a real keyboard); `sysinfo`, `language`, `upload --bank`, `--device`.
- The editor follows the connected keyboard's screen size, with a layout per size; settings for the preferred
  keyboard, TFT live refresh and the system page.
- Simulator of a TFT keyboard and tests checked against packet vectors recovered from Epomaker's software.
- Docs: `docs/protocols/` (rdm, tft), `docs/hardware-testing.md`.

### Changed
- Package `epomaker_screen`, commands `epomakerctl` and `epomaker-screen`, settings in
  `~/.config/epomaker-screen` (an RT82 layout and settings from 0.2 are carried over), udev rule
  `70-epomaker-screen.rules`. The installer removes the old `rt82-plasma` installation.
- The RDM driver finds the screen controller on its own keyboard's hub, so several keyboards can be connected.
- The HID layer parses collections and feature reports and supports feature-report I/O.

## [0.2.0] - 2026-10-29

### Fixed
- Uploads no longer use the screen controller's download mode, which froze the controller after a few uploads
  (seen on the RT82 at the `16` table commit). Uploads now erase, write and redraw in plain sessions.
- Commands that touch flash wait longer for a reply, and an interrupted sequence is closed before giving up.
- `scripts/boot-keyboard.sh` also loads the keyboard's USB host driver (e.g. `xhci_pci`) early.

### Added
- A frozen screen controller is detected and reported with power-cycle instructions; RT82 Screen leaves it alone
  until the keyboard is re-plugged.
- Setting: keep running in the system tray when the window is closed, or quit.
- Animations are thinned out to 40 frames (Epomaker Hub's limit) by default; `rt82ctl upload --max-frames`.
- Warning before uploads larger than what has been tried on an RT82.
- Installer and boot scripts work without sudo (`su -c`), as on ALT Linux.
- Continuous integration, documentation in `docs/`, contributing guide.

### Changed
- Everything is in English: user interface, messages, scripts and documentation.
- Repository layout: `src/rt82` package, `data/` for system files, `scripts/` for all scripts.
- `boot/rt82-boot-keyboard.sh` is now `scripts/boot-keyboard.sh`, `boot/diagnose.sh` is `scripts/diagnose.sh`.
  Changes made by the old script are still recognised by `--undo`.

## [0.1.0] - 2026-10-29

### Added
- RT82 screen protocol over hidraw: clock sync, picture and GIF upload, QGIF encoder.
- Live mode streaming widget frames into the screen's flash.
- KDE Plasma tray app with a widget editor; `rt82ctl` command-line tool.
- udev rule for non-root access (also fixes Epomaker Hub in Chromium).
- Initramfs keyboard fix and diagnostics scripts.
