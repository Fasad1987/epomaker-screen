# Contributing

Thanks for helping. Bug reports from real hardware are the most valuable contribution: please include the
output of `su -c 'scripts/diagnose.sh'` and the app log.

## Development setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[pyqt6,dev]'      # or '.[pyside6,dev]'
```

Run the app from the checkout with `rt82-screen`, or `python -m rt82.gui`; the CLI is `rt82ctl`.

## Checks

```bash
pytest                         # unit tests and the simulated screen controller
ruff check . && ruff format --check .
shellcheck -x scripts/*.sh
udevadm verify data/udev/70-epomaker-rt82.rules   # systemd 254+
```

The GUI and rendering tests need Qt; they run headless with `QT_QPA_PLATFORM=offscreen` and are skipped when
no Qt binding is installed. Continuous integration runs everything on PyQt6 and PySide6.

## Working on the protocol

- Read [docs/protocol.md](docs/protocol.md) first. Some command sequences freeze the screen controller until the
  keyboard is power-cycled; the simulator in `tests/fake_screen.py` rejects the known ones (download mode,
  erasing outside a session, erases that take too long, writing over programmed flash).
- When you change how the device is driven, extend the simulator and add a test before trying it on hardware.
- Keep every erase under ~3.4 s and never use download mode (`1B`).

## Style

- Python 3.9+, formatted with `ruff format`, line length 120.
- Comments explain why, not what. Hardware facts get a note on where they come from.
- Shell scripts are bash, checked with shellcheck, and must work when started with `su -c` (no sudo).

## Pull requests

- One topic per pull request, with tests.
- Add an entry under "Unreleased" in [CHANGELOG.md](CHANGELOG.md).
- Say whether you tried the change on a keyboard, and which one.
