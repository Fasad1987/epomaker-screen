"""Experiments for learning a keyboard's screen protocol on real hardware.

Each experiment draws something recognisable, measures how long the keyboard
takes, and says what to look for on the screen. Results go into
docs/protocols/tft.md once confirmed.

Only TFT-family keyboards are supported: the RT82 family's protocol is
already mapped, and its controller is easy to freeze with experiments.
"""

from __future__ import annotations

import argparse
import sys
import time

from .drivers.base import DeviceError
from .drivers.tft import codec

EXPERIMENTS = ("pattern", "speed", "partial", "animation", "banks", "flow", "frames")


def add_parser(sub) -> argparse.ArgumentParser:
    p = sub.add_parser("lab", help="TFT keyboards: protocol experiments (draw test pictures, measure speed)")
    p.add_argument("experiment", choices=EXPERIMENTS)
    p.add_argument("--bank", type=int, default=1, help="bank 1..5 to draw into (default 1)")
    p.add_argument("--delays", default="5,2,1,0", help="speed: milliseconds between pixel packets to try")
    p.add_argument("--pause", type=float, default=0.0, help="banks: seconds to wait between pictures")
    p.add_argument("--every", type=int, default=32, help="flow: read the keyboard's reply every N packets")
    p.add_argument("--frames", type=int, default=56, help="frames: how many numbered frames to upload (2..255)")
    return p


def _frame(width: int, height: int, paint) -> bytes:
    out = bytearray(width * height * 3)
    for y in range(height):
        for x in range(width):
            o = (y * width + x) * 3
            out[o : o + 3] = bytes(paint(x, y))
    return bytes(out)


def pattern(width: int, height: int, mark: int = 0) -> bytes:
    """Colour bars on top, a gradient below, white top-left corner, ``mark`` dots on the left.

    It shows at a glance whether colours, orientation and byte order are right.
    """
    bars = [
        (255, 0, 0),
        (0, 255, 0),
        (0, 0, 255),
        (255, 255, 0),
        (0, 255, 255),
        (255, 0, 255),
        (255, 255, 255),
        (0, 0, 0),
    ]

    def paint(x, y):
        if x < width // 8 and y < height // 8:
            return (255, 255, 255)  # top-left marker
        if y < height // 2:
            return bars[x * len(bars) // width]
        if x < 10 and mark and (y - height // 2) % 20 < 10 and (y - height // 2) // 20 < mark:
            return (255, 128, 0)  # one orange dot per run
        return (x * 255 // max(1, width - 1), y * 255 // max(1, height - 1), 128)

    return _frame(width, height, paint)


# 3x5 digits, one string of rows per digit.
DIGITS = (
    "111101101101111", "010110010010111", "111001111100111", "111001111001111", "101101111001001",
    "111100111001111", "111100111101111", "111001010010010", "111101111101111", "111101111001111",
)  # fmt: skip


def numbered(width: int, height: int, n: int) -> bytes:
    """A frame showing ``n`` in big digits on a background whose colour changes with ``n``."""
    text = str(n)
    scale = max(1, min(width // (4 * len(text) + 1), height // 7))
    left = (width - (4 * len(text) - 1) * scale) // 2
    top = (height - 5 * scale) // 2
    background = ((n * 53) % 160 + 40, (n * 97) % 160 + 40, (n * 31) % 160 + 40)

    def paint(x, y):
        col, row = (x - left) // scale, (y - top) // scale
        if 0 <= row < 5 and 0 <= col < 4 * len(text) and col % 4 < 3 and x >= left and y >= top:
            digit = DIGITS[int(text[col // 4])]
            if digit[row * 3 + col % 4] == "1":
                return (255, 255, 255)
        return background

    return _frame(width, height, paint)


def solid(width: int, height: int, rgb) -> bytes:
    return bytes(rgb) * (width * height)


def _ask(question: str) -> str:
    if not sys.stdin.isatty():
        return ""
    try:
        return input(f"  {question} ").strip()
    except EOFError:
        return ""


def main(a, session, log) -> int:
    with session(a) as (driver, s):
        if driver.family != "tft":
            raise DeviceError("lab experiments are for TFT-family keyboards (RT100 Pro, RT75, ...)")
        w, h = driver.caps.width, driver.caps.height
        bank = a.bank - 1
        log(f"{driver.name}: {w}x{h}, firmware {s.identify()['firmware']:#06x}")
        log(
            "Switch the keyboard screen to the picture page first (RT100 Pro: Fn + PgUp / PgDn; other models: see the manual)."
        )
        log("Experiments overwrite the stored pictures and the animation (they share the screen memory).")

        if a.experiment == "pattern":
            t0 = time.monotonic()
            sent = s.upload_still(pattern(w, h), bank)
            log(f"bank {a.bank}: {sent} bytes in {time.monotonic() - t0:.2f} s")
            log("Expect: colour bars red, green, blue, yellow, cyan, magenta, white, black from left to right on the")
            log("top half, a white square top-left, a red-to-green gradient below.")
            answer = _ask("What do you see (ok / colours wrong / rotated / mirrored / garbage / nothing)?")
            if answer:
                log(f"reported: {answer}")

        elif a.experiment == "speed":
            results = []
            for run, ms in enumerate(float(v) for v in a.delays.split(",")):
                s.chunk_delay = ms / 1000
                t0 = time.monotonic()
                sent = s.upload_still(pattern(w, h, run + 1), bank)
                took = time.monotonic() - t0
                results.append((ms, took, sent / took / 1024))
                log(
                    f"delay {ms} ms: {took:.2f} s, {sent / took / 1024:.1f} KiB/s ({run + 1} orange dot(s) on the left)"
                )
                answer = _ask("Is this picture complete and correct (y/n)?")
                if answer:
                    log(f"reported for {ms} ms: {answer}")
                if answer.lower().startswith("n"):
                    break
            print("\ndelay_ms  seconds  KiB/s")
            for ms, took, rate in results:
                print(f"{ms:8g}  {took:7.2f}  {rate:5.1f}")

        elif a.experiment == "partial":
            s.upload_still(pattern(w, h), bank)
            log("full pattern sent")
            # Pixels go column by column, so a full-height strip is one contiguous run of
            # bytes: if the firmware writes from the strip's first column, this one works.
            steps = (
                ("a green full-height strip in the middle", (0, 255, 0), (w * 3 // 8, 0, w * 5 // 8, h),
                 "strip in the middle / strip on the left edge / whole screen green / nothing changed"),
                ("a red 1/4-size box in the middle", (255, 0, 0), (w * 3 // 8, h * 3 // 8, w * 5 // 8, h * 5 // 8),
                 "box on pattern / box on black / red stripes / nothing changed"),
            )  # fmt: skip
            for what, colour, box, choices in steps:
                t0 = time.monotonic()
                sent = s.upload_still(solid(w, h, colour), bank, box)
                log(f"{what}: box {box}, {sent} bytes in {time.monotonic() - t0:.2f} s")
                answer = _ask(f"What do you see ({choices})?")
                if answer:
                    log(f"reported: {answer}")
            log("Now switch the screen to another page and back to the picture (the firmware may redraw only then).")
            answer = _ask("What do you see now (pattern / pattern with strip and/or box / something else)?")
            if answer:
                log(f"reported after switching: {answer}")

        elif a.experiment == "animation":
            frames = [solid(w, h, c) for c in ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255))]
            t0 = time.monotonic()
            # The frame delay is one byte of milliseconds, so 4 fps (250 ms) is the slowest.
            s.upload_animation(frames, 4)
            log(f"4-frame animation at 4 fps in {time.monotonic() - t0:.1f} s")
            log("Expect red, green, blue, white, four changes a second, looping.")
            answer = _ask("What do you see?")
            if answer:
                log(f"reported: {answer}")

        elif a.experiment == "banks":
            colours = (None, (255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 0))
            failed = []
            for b, c in enumerate(colours):
                if b and a.pause:
                    time.sleep(a.pause)
                t0 = time.monotonic()
                try:
                    s.upload_still(pattern(w, h) if c is None else solid(w, h, c), b)
                except DeviceError as e:
                    failed.append(b + 1)
                    log(f"bank {b + 1}: FAILED after {time.monotonic() - t0:.1f} s: {e}")
                    continue
                p = s.last_prepare
                log(
                    f"bank {b + 1}: {'pattern' if c is None else c}, {time.monotonic() - t0:.2f} s, "
                    f"accepted on attempt {p.get('attempts')} after {p.get('seconds', 0):.2f} s (reply {p.get('reply')})"
                )
            if failed:
                log(f"banks {failed} failed; try again with a pause, e.g. `epomakerctl lab banks --pause 3`")
            log("Banks 1-5: pattern, red, green, blue, yellow. Cycle the pictures on the keyboard")
            log("and note which order they come in and whether the keyboard switched by itself.")
            answer = _ask("What did you observe?")
            if answer:
                log(f"reported: {answer}")
        elif a.experiment == "frames":
            count = max(2, min(255, a.frames))
            log(f"uploading {count} numbered frames (1..{count}) at 4 fps; this takes a few minutes")
            t0 = time.monotonic()
            s.upload_animation([numbered(w, h, i + 1) for i in range(count)], 4, maximum=255)
            log(f"done in {time.monotonic() - t0:.0f} s")
            log("Watch a whole loop: note the last number that shows cleanly before anything else appears.")
            log("Then look through the picture pages: which numbers (if any) does each show?")
            answer = _ask("Last clean number, and what the picture pages show:")
            if answer:
                log(f"reported: {answer}")

        elif a.experiment == "flow":
            flow(s, w, h, bank, a, log)
    return 0


def flow(s, w, h, bank: int, a, log) -> None:
    """Streams one picture with no delay and reads the reply every few packets.

    The keyboard acknowledges every packet even when it falls behind, so this looks
    for a reply that shows its progress or a busy state, to pace uploads by it.
    """
    data = s._pixels(pattern(w, h, 3), None)
    packets = codec.chunks(data, bank, 1, 0)
    every = max(1, a.every)
    with s._lock:
        s._prepare(codec.prepare(len(data), (0, 0, w, h), bank, 1, 0))
        s.sleep(0.1)
        t0, last = time.monotonic(), None
        for i, p in enumerate(packets):
            s.send(p, 0.0)
            if i % every == 0 or i + 1 == len(packets):
                reply = s.t.get_feature(0, codec.REPORT)[:8].hex(" ")
                if reply != last:
                    log(f"packet {i + 1}/{len(packets)} at {time.monotonic() - t0:.2f} s: reply {reply}")
                    last = reply
        sent = time.monotonic() - t0
        log(f"all {len(packets)} packets sent in {sent:.2f} s; now polling for 5 s")
        for _ in range(20):
            s.sleep(0.25)
            reply = s.t.get_feature(0, codec.REPORT)[:8].hex(" ")
            if reply != last:
                log(f"after {time.monotonic() - t0:.2f} s: reply {reply}")
                last = reply
    log("Expect the test pattern with three orange dots on the left.")
    answer = _ask("Is the picture complete and correct (y/n), and did the keyboard's progress bar keep up?")
    if answer:
        log(f"reported: {answer}")
