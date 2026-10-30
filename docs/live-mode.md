# Live mode

This page describes live mode on the RT82 family. TFT keyboards (RT100 Pro, ...) redraw a picture bank
instead; see [the TFT protocol](protocols/tft.md#live-widgets-on-tft-keyboards).

## RT82 family

The screen has no "draw this frame" command: it only plays animations from its flash. Live mode therefore
writes an endless animation into flash and keeps appending frames a few seconds ahead of what the screen shows
(`src/epomaker_screen/drivers/rdm/live.py`). Frames are rendered for the moment they will be on screen, so a clock shows the right time
even though it is written ahead.

### Flash layout

```
blocks [0 .. area)   stream area: a sequence of segments, each a growing QGIF
block  [area]        fallback still, shown only while the stream area is erased
```

- A **segment** starts with a QGIF header declaring 65535 frames, a keyframe and a few lead frames, and the
  slot table is flipped to it (`10`, table, `11`, `1A`, about 0.15 s, no "Downloading").
- Frames are appended while it plays. An unchanged frame costs 255 bytes (the change bitmap); a changed one adds
  8 bytes per changed 4×4 tile.
- Every `segment_seconds` (default 300) the next segment starts right after the previous one. The screen plays
  slightly slower than declared, and a fresh segment resets that drift.
- When the area is full, the screen is flipped to the fallback still, the area is erased (in steps of at most
  ~3.4 s) and streaming restarts at block 0. The fallback sits after the area because an erase always starts at
  block 0. The fallback is the layout's background without live data, so for a few seconds the screen shows no
  values.
- If the stream falls behind the screen (system suspend, slow USB), a new segment starts at once instead of
  letting the screen read unwritten flash.
- When live mode stops, the current picture is stored as a looping still, so the screen does not run into
  erased flash.

### Flash wear

With the defaults (1 frame per second, 24 blocks) the area is erased about once an hour: roughly 9,000 erase
cycles a year, well below the ~100,000 that SPI NOR flash is usually rated for. Two frames per second doubles
that. Turn live mode off when you do not need it.

### Settings

File → Settings in the app, or `live_config` in `~/.config/epomaker-screen/settings.json`:

| Key               | Default | Meaning                                                    |
| ----------------- | ------- | ---------------------------------------------------------- |
| `fps`             | 1       | frames per second (1–10)                                   |
| `area_blocks`     | 24      | flash blocks for the stream (4–28)                         |
| `segment_seconds` | 300     | how often a new segment starts                             |
| `lead_frames`     | 5       | frames written ahead of the screen (at least 4)            |
| `slowdown`        | 1.0     | declared/real frame time ratio; keep at or below the real one |

### Testing

`tests/fake_screen.py` simulates the controller: flash that must be erased before it is written, erase timing,
the slot table, and playback that reads records ahead of the picture. `tests/test_live.py` streams for three
simulated hours and checks that the screen never reads an unwritten or half-written record, that the picture on
screen is never erased, that every erase stays short, and that the clock on screen matches the time it was
rendered for, including with a screen that plays 3% slow and after a simulated suspend.
