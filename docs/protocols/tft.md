# TFT screen protocol (RT100 Pro, RT75, RT85, Glyph, RT100)

In these keyboards the screen is driven by the keyboard's own firmware. The host sends raw pixels through the
vendor command interface; there is no separate screen controller and no image format on the wire.

Implemented in `src/epomaker_screen/drivers/tft/`. The byte layouts were recovered from Epomaker's Windows and
macOS software (version 3.2.22) by [ramarivera/epomaker-driver-linux](https://github.com/ramarivera/epomaker-driver-linux)
(see its `docs/glyph-protocol.md` and `docs/display.md`); this project's codec is checked against the packet
vectors in that project's tests.

**Status:** checked on an **RT100 Pro, firmware 0x0109**: identification, the clock, the system page, the pixel
format (`lab pattern`), pixel packets with no delay (`lab speed`) and animations (`lab animation`) work, and an
animation is still there after unplugging the cable. Black banks and a 47-frame GIF looping cleanly after
`reset-screen` confirmed the memory map below; the RT100 Pro is marked verified. The rest is still open (below);
`epomakerctl lab` runs the experiments that settle it (see [hardware testing](../hardware-testing.md)).

## Devices

| Model              | USB id      | Internal id | Screen  | Pixels | Animation frames |
| ------------------ | ----------- | ----------- | ------- | ------ | ---------------- |
| RT100 Pro          | `3151:5002` | 3152        | 240×240 | RGB565 | 47 (measured; vendor: 56) |
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
| `AC` | erase screen flash | reply `AC AA AA 55 55` when done; removes pictures and animation (RT100 Pro), sent only by `reset-screen --erase` |

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
2 ms between packets; the driver waits 1 ms (`--chunk-delay` changes it).

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

One picture after a pause survives any of these, but the keyboard acknowledges packets it has no time to store:
with no delay, five pictures in a row and a 56-frame animation (6.3 MB in 33 s) came out corrupted. A half-written
picture shows white where its flash was erased and not written. The keyboard draws its own progress bar during an
upload, which fell behind the host. There is no flow control: `lab flow` showed that during a transfer the reply
is just a copy of the last packet received, with no progress or busy signal. So uploads are paced by time. A
56-frame animation came out right at 2 ms (265 s) and at 1 ms (149 s), the driver's default.

**Stills and the animation share memory** (see the map below): an animation longer than 47 frames runs into the
banks. A still written to bank 1 after a 56-frame animation once left the animation showing only its first frame;
that has not been reproduced.

**Screen memory map (RT100 Pro, measured with `lab frames`).** The catalog entry has no memory size, so the vendor
app assumes 7 MiB and 56 frames. A 56-frame numbered animation showed:

- frames 1–53 clean, then parts of other screens (the clock page among them) until the loop restarted;
- picture pages showing frames 48 and 52.

That matches the vendor layout with **6 MiB**: a 4 KiB header, then 118,784-byte blocks (a 115,200-byte frame
rounded up to 4 KiB blocks). Animation frame *n* is block *n − 1*; the five still banks are the blocks right after
the 47 frames the formula allows, so frames 48–52 are banks 1–5. Frame 53 is the last block that fits in 6 MiB;
past it the firmware reads other data. Writes there seem to be ignored: the clock page stayed intact.

So the driver allows 47 frames, and an animation never overwrites the banks. A still never shortens the animation
either: after black stills went into every bank, the numbered animation still played, with black frames 48–52.
`reset-screen` therefore writes a black two-frame animation as well as black banks.

After a picture the keyboard refuses the next prepare for 1–5 s (reply `a5 00 ...`), probably while it finishes
writing flash. The driver waits up to about 10 s.

### Open questions (to settle on hardware)

- Does the keyboard switch to a bank when a picture is written to it, or only store it? (`lab banks`)
- A prepare with a smaller box **replaces** the picture: an RT100 Pro (0x0109) showed the box on black (one earlier
  run showed no change, probably on another screen page). So boxes cannot patch a picture, only make smaller ones.
  `lab partial` also tries a full-height strip and a page switch, to see where boxes land.
- Animations redraw visibly, like a wipe across the screen, on an RT100 Pro: the firmware seems to copy each frame
  from flash to the panel slower than one refresh. Full-screen colour changes show it most.
- Are stills and the animation stored separately, and do they survive unplugging?
- Where are pictures stored (flash wear for frequent live updates)?

### Erasing the screen flash (`AC`)

On an RT100 Pro (firmware 0x0109) `AC` answered `ac aa aa 55 55` after 0.5 s and removed every stored picture
and the animation: only the clock and system pages remained, both intact, as on a new keyboard. So the firmware's
own page graphics are not in the erased area, although frames past the memory limit read them (see the memory
map). epomaker-controller describes the same on the RT100 ("resets the image, does not affect telemetry").

## Screen pages

On the RT100 Pro **Fn + PgUp / PgDn** cycle the screen pages: the clock page, the system page, the animation and
the five pictures. There is no known command to switch pages from the host.

Once an animation is stored, the keyboard goes back to it by itself some time after you switch to another page,
so the clock page shows only until then. This is the firmware's own timer: it happens with no program talking to
the keyboard. The host cannot turn it off or switch pages.

## Live widgets on TFT keyboards

The firmware cannot be streamed to like the RT82's controller, and each update rewrites a picture bank in flash.
NOR flash lasts about 100,000 erases and there is no sign of wear levelling, so live mode redraws the layout every
`interval_s` (15 minutes by default, at least 5, aligned to the interval) and only sends a picture when it changed
(a few seconds on an RT100 Pro). Once a minute would wear a bank out in about 70 days of use; for a clock, use
the keyboard's own clock page. It always sends the whole picture: a smaller box
replaces the picture rather than patching it.

The firmware draws a clock and a system page itself: Epomaker Screen sets the clock on connect and after suspend and
sends system values every 2 s, which costs nothing in flash.
