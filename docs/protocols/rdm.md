# RDM screen protocol (RT82, QK108, RT98)

What this project relies on, as implemented in `src/epomaker_screen/drivers/rdm/`. The protocol was
reverse-engineered by [guysoft/rt82display](https://github.com/guysoft/rt82display/blob/main/PROTOCOL.md) (RT82)
and [wyattades/keyscreen](https://github.com/wyattades/keyscreen/blob/main/PROTOCOL.md) (QK108, same screen
controller); their documents have the full history of each finding.

## Devices

| Device            | USB ID      | Interface used                                                     |
| ----------------- | ----------- | ------------------------------------------------------------------ |
| Keyboard          | `36b0:30a3` | if 1, usage page `0xFF60` (QMK/VIA raw HID), 32-byte reports       |
| Screen controller | `1919:1919` | if 1, usage page `0x00FF`, 64-byte reports, no report id           |

- The screen controller sits behind a USB hub inside the keyboard. It appears about 1–1.5 s after the keyboard
  receives `AA E0`, and leaves the bus about 4 s after the last command.
- It accepts one host at a time. Two programs talking to it (for example Epomaker Hub in a browser tab and this
  app) can freeze it.
- Interfaces are found through sysfs by USB ids and the usage page in the report descriptor; the report size
  comes from the descriptor too.

## Keyboard commands (32-byte reports)

`AA cmd 00 00 00 len 00 00 payload…`; the reply echoes `AA cmd`.

| Cmd  | Effect                                                         |
| ---- | -------------------------------------------------------------- |
| `E0` | enumerate the screen controller                                |
| `E2` | screen mode status (reply byte 8 = 1: on)                      |
| `E3` | number of stored screens (`len 1`, byte 8 = n), before uploads |

## Screen controller packets (64 bytes)

```
byte  0    1    2      3      4      5    6  7   8 .. 63
      AA   cmd  addrL  addrM  addrH  len  00 00  payload (up to 56 bytes)
```

Every command gets one reply echoing `AA cmd`; reply byte 7 = `0x0F` means rejected.

| Cmd  | Name          | Used for                                                                 |
| ---- | ------------- | ------------------------------------------------------------------------ |
| `10` | FUNC_START    | opens a plain session                                                    |
| `11` | FUNC_STOP     | closes it                                                                |
| `12` | READ_INFO     | width/height/rotation (`epomakerctl info`)                                   |
| `15` | WRITE_FUNC    | writes a slot table page                                                 |
| `16` | WRITE_FUNC_OK | commits the slot table                                                   |
| `17` | WRITE_TIME    | sets the clock                                                           |
| `18` | FLASH_ERASE   | erases blocks `0 .. allocation-1` (inside a session only)                |
| `19` | FLASH_WRITE   | programs up to 56 bytes into erased flash (no session needed)            |
| `1A` | INIT_LCD      | re-reads the slot table and restarts playback                            |
| `1B` | WRITE_START   | download mode, **not used** (see below)                                  |
| `1C` | GET_MODE      | link type; also a cheap "are you alive" probe                            |

### Clock

`10`, `17 len 0x38` with one decimal digit per byte `Y Y Y Y M M W D D h h m m s s` (W = weekday, Sunday = 0),
`11`.

### Flash and the slot table

- Flash is erased in 64 KiB blocks and can only be programmed after an erase (bits go from 1 to 0).
- Images are QGIF files, each starting on a block boundary.
- The slot table is `00 count allocation` followed by `startBlock 00 lenL lenM lenH` per slot. It is written as
  three `15` pages (`addr 0 len 0x38`, `addr 0x38 len 0x38`, `addr 0x70 len 0x10`) and committed with `16`.
- **The erase clears exactly the blocks the table allocates, starting at block 0**, whatever its own argument
  says. To erase, write a table with the wanted allocation first.
- The screen plays slot 1 on its GIF page; a new table shows after `1A`.

### Upload sequence used here

1. Keyboard `E3` with the number of images.
2. For each erase step (each kept under ~3.4 s, ~140 ms per block holding data): `10`, table with the step's
   allocation, `18`, `11`, then `10`, the real table, `11`.
3. `19` writes for each image (two packets in flight).
4. `10`, table, `11`, `1A` (redraw), then the clock.

On any error the open session is closed (`11`, `1A`) before giving up, and the next connection probes the
controller with `1C` before using it.

### Why not download mode

Epomaker's web tool wraps uploads in download mode (`1B` … `1A`, the "Downloading" screen). Entering download
mode repeatedly freezes the controller: on an RT82 it stopped answering at the `16` commit of the third upload
within four minutes, and keyscreen saw the same on the QK108 after 8–14 switches. A frozen controller stays on
USB but answers nothing until the keyboard is power-cycled. The plain-session sequence above ran 150 times in a
row without a failure in keyscreen's tests.

## QGIF

Header (10 bytes): `QGIF`, `0`, `2*fps-1`, frame count (u16 big-endian), `width/4-1`, `height/4-1`. The 135-pixel
panel uses a height of 136.

Each frame record is a change bitmap (one bit per 4×4 tile, left to right, top to bottom, MSB first; 255 bytes
for 240×136) followed by 8 bytes for every changed tile: colour A and colour B (RGB565 little-endian), then 4
rows of four 2-bit palette indices (leftmost pixel in the high bits). Palette: A, B, (2A+B)/3, (A+2B)/3, with
A ≥ B. The first record contains every tile; a looping file ends with one extra record that restores frame 0.

The screen reads records progressively while it plays, about 3 frames ahead of the picture, which is what
[live mode](../live-mode.md) builds on.
