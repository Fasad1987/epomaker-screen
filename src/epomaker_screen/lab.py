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

EXPERIMENTS = ("pattern", "speed", "partial", "animation", "banks")


def add_parser(sub) -> argparse.ArgumentParser:
    p = sub.add_parser("lab", help="TFT keyboards: protocol experiments (draw test pictures, measure speed)")
    p.add_argument("experiment", choices=EXPERIMENTS)
    p.add_argument("--bank", type=int, default=1, help="bank 1..5 to draw into (default 1)")
    p.add_argument("--delays", default="5,2,1,0", help="speed: milliseconds between pixel packets to try")
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
            "Switch the keyboard screen to the picture page first (see its manual; often Fn + a key next to the screen)."
        )

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
            log("full pattern sent; now a 1/4-size red box into the middle only")
            box = (w * 3 // 8, h * 3 // 8, w * 5 // 8, h * 5 // 8)
            t0 = time.monotonic()
            sent = s.upload_still(solid(w, h, (255, 0, 0)), bank, box)
            log(f"partial: {sent} bytes in {time.monotonic() - t0:.2f} s")
            log("If partial updates work, the pattern stays and only a red box appears in the middle.")
            answer = _ask("What do you see (box on pattern / box on black / red box stretched / nothing changed)?")
            if answer:
                log(f"reported: {answer}")

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
            for b, c in enumerate(colours):
                s.upload_still(pattern(w, h) if c is None else solid(w, h, c), b)
                log(f"bank {b + 1}: {'pattern' if c is None else c}")
            log("Banks 1-5: pattern, red, green, blue, yellow. Cycle the pictures on the keyboard")
            log("and note which order they come in and whether the keyboard switched by itself.")
            answer = _ask("What did you observe?")
            if answer:
                log(f"reported: {answer}")
    return 0
