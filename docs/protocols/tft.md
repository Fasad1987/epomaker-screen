# TFT screen protocol (RT100 Pro, RT75, RT85, Glyph, RT100)

In these keyboards the screen is driven by the keyboard's own firmware. The host sends raw pixels through the
vendor command interface; there is no separate screen controller and no image format on the wire.

Implemented in `src/epomaker_screen/drivers/tft/`. The byte layouts were recovered from Epomaker's Windows and
macOS software (version 3.2.22) by [ramarivera/epomaker-driver-linux](https://github.com/ramarivera/epomaker-driver-linux)
(see its `docs/glyph-protocol.md` and `docs/display.md`); this project's codec is checked against the packet
vectors in that project's tests.

**Status:** checked on an **RT100 Pro, firmware 0x0109**: identification, the clock, the system page, the pixel
format (`lab pattern`), pixel packets with no delay (`lab speed`) and animations (`lab animation`) work, and an
animation is still there after unplugging the cable. The rest is still open (below);
`epomakerctl lab` runs the experiments that settle it (see [hardware testing](../hardware-testing.md)).

## Devices

| Model              | USB id      | Internal id | Screen  | Pixels | Animation frames |
| ------------------ | ----------- | ----------- | ------- | ------ | ---------------- |
| RT100 Pro          | `3151:5002` | 3152        | 240×240 | RGB565 | 56               |
| RT75               | `3151:5002` | 3223        | 240×240 | RGB565 | 47               |
| RT85               | `3151:5002` | 2895        | 320×172 | RGB565 | 51               |
| Glyph              | `3151:5002` | 3059        | 428×142 | RGB565 | 46               |
| RT100              | `3151:4015` | 1379        | 162×173 | RGB565 | catalog          |
| Dynatab75X-UK      | `3151:4015` | 1723        | 60×9    | RGB888 | catalog          |
| HE65 Mag           | `3151:502f` | 2376        | 128×128 | RGB565 | catalog          |

Several models share one USB id, so the model is identified with command `8F` before anything is drawn.

The command interface is interface 2: a HID collection with usage page `0xFFFF`, usage `2`, and one unnumbered
64-byte **feature** report. Commands are sent with `HIDIOCSFEATURE` and replies read with `HIDIOCGFEATURE`.

## Framing

```
byte 0      command
bytes 1..6  parameters
byte 7      checksum = 255 - (sum(bytes 0..6) & 255)
bytes 8..63 payload
```

The checksum covers the header only. The driver refuses to build any command outside a small allow-list
(identify, clock, system info, language, picture upload), so a bug cannot send factory reset (`01`), screen
flash erase (`AC`), key-map or firmware-update commands.

## Commands used

| Cmd  | Name           | Payload                                                                                                  |
| ---- | -------------- | -------------------------------------------------------------------------------------------------------- |
| `8F` | identify       | reply: `[1:5]` internal id (u32 LE), `[7:9]` firmware version (u16 LE), `[9]` 1 in boot mode             |
| `28` | clock          | `[8:10]` year **big-endian**, then month, day, hour, minute, second                                       |
| `22` | system info    | u16 LE GiB: disk free, disk total, RAM used, RAM total; u8 CPU %, u8 CPU °C; u16 LE GiB net up, down     |
| `27` | language       | `[1] = 1` toggles the screen language (no way to read the current one)                                    |
| `A5` | prepare RGB565 | see below; a write that authorises an upload, not a query                                                 |
| `25` | pixels RGB565  | see below                                                                                                 |
| `A9`/`29` | RGB888 variants | same layout, three bytes per pixel (Dynatab75X)                                                   |

### Pictures

**Prepare** (`A5`), then read the reply; the keyboard is ready when reply byte 1 is `1`. The vendor software waits
100 ms before reading and retries up to ten times (about 2 s). An RT100 Pro refused a third prepare for that long
right after two full pictures in a row, so the driver keeps asking for about 10 s and reports the attempts and the
last reply.

| Offset | Meaning                                                                    |
| ------ | -------------------------------------------------------------------------- |
| 1      | still: bank 0..4; animation: 0                                             |
| 2      | frames: 1 for a still, the frame count for an animation                    |
| 3      | frame delay in ms (one byte: animations play at 4 fps or faster)           |
| 4, 5   | pixel byte count, bits 0–15                                                |
| 8..11  | low bytes of left, top, right, bottom (right/bottom exclusive)             |
| 12..15 | high bytes of left, top, right, bottom                                     |
| 16, 17 | pixel byte count, bits 16–31                                               |

**Pixels** (`25`), 56 bytes per packet: `[1]` bank or frame index, `[2]` frames, `[3]` delay, `[4:6]` packet index
(u16 LE, restarting at 0 for each animation frame), `[6]` bytes in this packet. The vendor desktop app waits
2 ms between packets; the driver sends them back to back (each is an acknowledged USB control transfer).

Pixels are RGB565 **big-endian**, ordered **column by column** (x outer, y inner): for a 2×2 image red, green /
blue, white the bytes are `F8 00 00 1F 07 E0 FF FF`.

A full RT100 Pro picture is 115,200 bytes in 2,058 packets. Measured on an RT100 Pro (firmware 0x0109), all
pictures complete:

| Delay between packets | Full picture | Throughput |
| --------------------- | ------------ | ---------- |
| 5 ms                  | 12.29 s      | 9 KiB/s    |
| 2 ms (vendor)         | 5.28 s       | 21 KiB/s   |
| 1 ms                  | 2.84 s       | 40 KiB/s   |
| none                  | 0.77 s       | 146 KiB/s  |

### Open questions (to settle on hardware)

- Why did the third still in a row get refused (`lab banks`, now with timings and `--pause`)? Likely the keyboard
  is still writing the previous picture to flash.
- Does the keyboard switch to a bank when a picture is written to it, or only store it? (`lab banks`)
- A prepare with a smaller box **replaces** the picture: an RT100 Pro (0x0109) showed the box on black (one earlier
  run showed no change, probably on another screen page). So boxes cannot patch a picture, only make smaller ones.
  `lab partial` also tries a full-height strip and a page switch, to see where boxes land.
- Animations redraw visibly, like a wipe across the screen, on an RT100 Pro: the firmware seems to copy each frame
  from flash to the panel slower than one refresh. Full-screen colour changes show it most.
- Are stills and the animation stored separately, and do they survive unplugging?
- Where are pictures stored (flash wear for frequent live updates)?

## Live widgets on TFT keyboards

The firmware cannot be streamed to like the RT82's controller, and each update rewrites a picture bank. Live
mode therefore redraws the layout every `interval_s` (60 s by default, aligned to the minute) and only sends a
picture when it changed (under a second on an RT100 Pro). It always sends the whole picture: a smaller box
replaces the picture rather than patching it.

The firmware draws a clock and a system page itself: Epomaker Screen sets the clock on connect and after suspend and
sends system values every 2 s, which costs nothing in flash.
