# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[Semantic Versioning](https://semver.org/).

## [Unreleased]

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
