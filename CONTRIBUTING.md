# Contributing

Thanks for helping. Bug reports from real hardware are the most valuable contribution: please include the
output of `su -c 'scripts/diagnose.sh'` and the app log.

## Development setup

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[pyqt6,dev]'      # or '.[pyside6,dev]'
```

Run the app from the checkout with `epomaker-screen`, or `python -m epomaker_screen.gui`; the CLI is `epomakerctl`.

## Checks

```bash
pytest                         # unit tests and the simulated screen controller
ruff check . && ruff format --check .
shellcheck -x scripts/*.sh
udevadm verify data/udev/70-epomaker-screen.rules   # systemd 254+
```

The GUI and rendering tests need Qt; they run headless with `QT_QPA_PLATFORM=offscreen` and are skipped when
no Qt binding is installed. Continuous integration runs everything on PyQt6 and PySide6.

## Adding a keyboard

1. Run `epomakerctl probe --identify` with the keyboard connected by cable and keep the output.
2. If it belongs to a known family (same vendor collection, same commands), add a `Model` to
   `src/epomaker_screen/models.py` with its USB id, internal id (TFT) and screen size, unverified.
3. Run the checklist in [docs/hardware-testing.md](docs/hardware-testing.md) and set `verified=True` once a
   picture, the clock and (where it applies) live mode work.
4. A new screen family gets its own package under `src/epomaker_screen/drivers/` with the same session API
   (`sync_time`, `upload`, `info`, `live`, `recover`, `close`), a simulator in `tests/`, and a protocol page in
   `docs/protocols/`.

## Working on the protocol

- Read the family's page in [docs/protocols/](docs/protocols/) first. Some command sequences freeze the screen controller until the
  keyboard is power-cycled; the simulators in `tests/fake_screen.py` (RT82 family) and `tests/fake_tft.py` reject the known ones (download mode,
  erasing outside a session, erases that take too long, writing over programmed flash).
- When you change how the device is driven, extend the simulator and add a test before trying it on hardware.
- RT82 family: keep every erase under ~3.4 s and never use download mode (`1B`). TFT family: only send commands
  in the codec's allow-list.

## Style

- Python 3.9+, formatted with `ruff format`, line length 120.
- Comments explain why, not what. Hardware facts get a note on where they come from.
- Shell scripts are bash, checked with shellcheck, and must work when started with `su -c` (no sudo).

## Pull requests

- One topic per pull request, with tests.
- Add an entry under "Unreleased" in [CHANGELOG.md](CHANGELOG.md).
- Say whether you tried the change on a keyboard, and which one.
