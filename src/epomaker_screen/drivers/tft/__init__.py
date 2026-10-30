"""TFT screen family (RT100 Pro, RT75, RT85, Glyph, RT100): raw pixels through the keyboard firmware.

Protocol notes: docs/protocols/tft.md.
"""

from __future__ import annotations

import datetime as dt
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from ... import hid
from ...models import by_internal_id, by_usb_id
from ..base import (
    Animation,
    Capabilities,
    DeviceError,
    Log,
    Model,
    Progress,
    SystemInfo,
    Unsupported,
    usb_connection_id,
)
from . import codec

COMMAND_PAGE, COMMAND_USAGE = 0xFFFF, 0x0002  # vendor command collection (feature report 0, 64 bytes)

# Vendor timings (desktop app): 10 ms before a command, 10 ms before reading its
# reply, 100 ms before reading a prepare reply.
SEND_DELAY, READ_DELAY, PREPARE_READ_DELAY = 0.01, 0.01, 0.1
# The vendor app waits 2 ms between pixel packets. An RT100 Pro (firmware 0x0109)
# takes one picture with no delay, but acknowledges packets it cannot keep up with
# (it writes them to flash): back-to-back pictures and a 56-frame animation came out
# corrupted. There is no flow control, so uploads are paced by time: a 56-frame
# animation came out right at both 2 ms and 1 ms.
CHUNK_DELAY = 0.001
# The vendor app gives up on a prepare after ten retries (about 2 s). An RT100 Pro
# refused that long right after two full pictures in a row, so keep asking longer.
PREPARE_RETRY_DELAY = 0.1
PREPARE_ATTEMPTS = 48  # about 10 s at 210 ms per attempt


class Transport(Protocol):
    def set_feature(self, data: bytes, report_id: int = 0) -> None: ...
    def get_feature(self, report_id: int = 0, size: int | None = None) -> bytes: ...
    def close(self) -> None: ...


def command_node(usb_path: str, vid: int, pid: int, devices: list[hid.HidInfo] | None = None) -> hid.HidInfo | None:
    pool = [
        d
        for d in (devices if devices is not None else hid.enumerate_hidraw())
        if not usb_path or d.usb_path == usb_path
    ]
    for d in pool:
        if (d.vendor_id, d.product_id) == (vid, pid) and d.descriptor.has(COMMAND_PAGE, COMMAND_USAGE):
            return d
    return None


# Each live update rewrites a whole picture in the keyboard's flash (29 erase blocks on
# an RT100 Pro). NOR flash lasts about 100,000 erases, and there is no sign of wear
# levelling: once a minute would wear a bank out in about 70 days of use, every
# 15 minutes in about 3 years.
MIN_REFRESH_S = 300.0


@dataclass
class RefreshConfig:
    """Live mode for TFT keyboards: each update rewrites a picture bank."""

    interval_s: float = 900.0  # how often to re-render and send changes (at least MIN_REFRESH_S)
    # Send only the changed rectangle. An RT100 Pro (firmware 0x0109) draws the box on black,
    # dropping the rest of the picture, so this stays off.
    partial: bool = False
    bank: int = 0


class _Refresh:
    """Re-renders the layout every ``interval_s`` and sends it when it changed."""

    def __init__(self, session: TftSession, render, config: RefreshConfig, log: Log):
        self.s, self.render, self.cfg, self.log = session, render, config, log
        self.last: bytes | None = None
        self.stats = {"updates": 0, "bytes": 0, "skipped": 0}

    def _update(self) -> None:
        frame = self.render(time.time())
        box = codec.changed_box(self.last, frame, self.s.caps.width, self.s.caps.height)
        if box is None:
            self.stats["skipped"] += 1
            return
        if not self.cfg.partial:
            box = (0, 0, self.s.caps.width, self.s.caps.height)
        sent = self.s.upload_still(frame, self.cfg.bank, box)
        self.last = frame
        self.stats["updates"] += 1
        self.stats["bytes"] += sent

    def start(self) -> None:
        self._update()

    def run_for(self, stop: threading.Event, seconds: float = math.inf) -> None:
        end = time.monotonic() + seconds
        while not stop.is_set() and time.monotonic() < end:
            # Align updates to the interval, e.g. on the minute for a clock.
            wait = self.cfg.interval_s - (time.time() % self.cfg.interval_s)
            if stop.wait(min(wait, max(0.0, end - time.monotonic()))):
                return
            if time.monotonic() < end:
                self._update()

    def stop(self) -> None:
        pass  # the last picture stays in its bank


class TftSession:
    def __init__(self, model: Model, transport: Transport, log: Log = lambda m: None, sleep=time.sleep):
        self.model = model
        self.t = transport
        self.log = log
        self.sleep = sleep
        self.chunk_delay = CHUNK_DELAY
        self.last_prepare: dict = {}
        self.caps = capabilities(model)
        self._lock = threading.RLock()

    # -- raw exchange ------------------------------------------------------

    def send(self, pkt: bytes, delay: float = SEND_DELAY, allow_erase: bool = False) -> None:
        if pkt[0] not in codec.ALLOWED and not (allow_erase and pkt[0] == codec.ERASE_SCREEN):
            raise ValueError(f"command 0x{pkt[0]:02X} is not allowed by this driver")
        self.sleep(delay)
        try:
            self.t.set_feature(pkt)
        except OSError as e:
            raise DeviceError(f"command 0x{pkt[0]:02X}: {e}") from e

    def exchange(self, pkt: bytes, read_delay: float = READ_DELAY) -> bytes:
        with self._lock:
            self.send(pkt)
            self.sleep(read_delay)
            try:
                return self.t.get_feature(0, codec.REPORT)
            except OSError as e:
                raise DeviceError(f"reply to 0x{pkt[0]:02X}: {e}") from e

    def identify(self) -> dict:
        reply = self.exchange(codec.identify())
        try:
            return codec.parse_identity(reply)
        except ValueError as e:
            raise DeviceError(str(e)) from e

    # -- Session API -------------------------------------------------------

    def sync_time(self, t: dt.datetime | None = None) -> None:
        self.send(codec.clock(t or dt.datetime.now()))
        self.sleep(0.1)

    def system_info(self, info: SystemInfo) -> None:
        self.send(
            codec.system_info(
                info.disk_free, info.disk_total, info.memory_used, info.memory_total,
                info.cpu_percent, info.cpu_temperature, info.net_up, info.net_down,
            )
        )  # fmt: skip

    def toggle_language(self) -> None:
        self.send(codec.packet([codec.LANGUAGE, 1]))
        self.sleep(0.1)

    def info(self) -> dict:
        return {**self.identify(), "width": self.caps.width, "height": self.caps.height}

    def _prepare(self, prepare: bytes) -> None:
        """Sends a prepare until the keyboard accepts it; records how long that took."""
        start, reply = time.monotonic(), b""
        for attempts in range(1, PREPARE_ATTEMPTS + 1):
            if attempts > 1:
                self.sleep(PREPARE_RETRY_DELAY)
            reply = self.exchange(prepare, PREPARE_READ_DELAY)
            if len(reply) >= 2 and reply[0] == prepare[0] and reply[1] == 1:
                break
        else:
            attempts = PREPARE_ATTEMPTS + 1
        self.last_prepare = {"attempts": attempts, "seconds": time.monotonic() - start, "reply": reply[:8].hex(" ")}
        if attempts > PREPARE_ATTEMPTS:
            raise DeviceError(
                f"the keyboard did not accept the picture upload ({PREPARE_ATTEMPTS} attempts in "
                f"{self.last_prepare['seconds']:.1f} s, last reply {self.last_prepare['reply'] or 'empty'})"
            )
        if attempts > 1:
            self.log(f"keyboard accepted the upload after {attempts} attempts ({self.last_prepare['seconds']:.1f} s)")

    def _transfer(self, prepare: bytes, packets: list[bytes], progress: Progress) -> None:
        with self._lock:
            self._prepare(prepare)
            self.sleep(0.1)
            total = len(packets)
            for i, p in enumerate(packets):
                self.send(p, self.chunk_delay)
                if i % 64 == 0 or i + 1 == total:
                    progress(i + 1, total)

    def _pixels(self, frame: bytes, box=None) -> bytes:
        w, h = self.caps.width, self.caps.height
        rgb24 = self.model.extra["pixel_bytes"] == 3
        return (codec.rgb888_column_major if rgb24 else codec.rgb565_column_major)(frame, w, h, box)

    def upload_still(self, frame: bytes, bank: int = 0, box=None, progress: Progress = lambda d, t: None) -> int:
        """Writes one picture into ``bank`` (0-based). Returns the pixel bytes sent."""
        if not 0 <= bank < self.model.extra["banks"]:
            raise ValueError(f"bank must be 0..{self.model.extra['banks'] - 1}")
        box = box or (0, 0, self.caps.width, self.caps.height)
        rgb24 = self.model.extra["pixel_bytes"] == 3
        data = self._pixels(frame, box)
        self._transfer(
            codec.prepare(len(data), box, bank, 1, 0, rgb24), codec.chunks(data, bank, 1, 0, rgb24), progress
        )
        return len(data)

    def upload_animation(
        self, frames: list[bytes], fps: int, progress: Progress = lambda d, t: None, maximum: int | None = None
    ) -> None:
        maximum = maximum or self.model.extra["max_frames"]
        if not 2 <= len(frames) <= maximum:
            raise ValueError(f"{self.model.name} animations take 2..{maximum} frames")
        rgb24 = self.model.extra["pixel_bytes"] == 3
        # The frame delay is one byte of milliseconds: animations play at 4 fps or faster.
        delay = max(1, min(255, round(1000 / max(1, fps))))
        data = [self._pixels(f) for f in frames]
        box = (0, 0, self.caps.width, self.caps.height)
        packets = [p for i, d in enumerate(data) for p in codec.chunks(d, i, len(data), delay, rgb24)]
        self._transfer(codec.prepare(len(data[0]), box, 0, len(data), delay, rgb24), packets, progress)

    def upload(self, items: list[Animation], progress: Progress = lambda done, total: None) -> None:
        animated = [a for a in items if len(a.frames) > 1]
        if animated:
            if len(items) != 1:
                raise ValueError(f"{self.model.name}: upload either one animation or up to 5 still pictures")
            self.upload_animation(animated[0].frames, animated[0].fps, progress)
            return
        if not 1 <= len(items) <= self.model.extra["banks"]:
            raise ValueError(f"{self.model.name} stores up to {self.model.extra['banks']} still pictures")
        for bank, item in enumerate(items):
            self.upload_still(item.frames[0], bank, progress=progress)

    def reset_screen(self, progress: Progress = lambda d, t: None) -> None:
        """The safe reset: a black two-frame animation (the shortest there is), black stills in every
        bank, and the clock set. Stills do not replace the animation, so both are written.

        There is no readback, so the factory pictures cannot be saved or restored from the host.
        """
        black = bytes(self.caps.width * self.caps.height * 3)
        banks = self.model.extra["banks"]
        steps = banks + 2  # the animation counts as two pictures
        self.upload_animation([black, black], 4, progress=lambda d, t: progress(2 * d, steps * t))
        for bank in range(banks):
            self.upload_still(black, bank, progress=lambda d, t, b=bank: progress((b + 2) * t + d, steps * t))
        self.sync_time()

    def erase_screen_flash(self, timeout: float = 120.0) -> bytes:
        """Sends the vendor's "erase screen flash" command (0xAC) and waits for its reply.

        Effect unverified: it may bring back the factory picture, or also erase graphics the
        firmware draws its own pages with. Only call this after the user confirmed.
        """
        with self._lock:
            self.send(codec.erase_screen(), SEND_DELAY, allow_erase=True)
            start, reply = time.monotonic(), b""
            polls = max(1, int(timeout / 0.5))
            for _ in range(polls):
                self.sleep(0.5)
                try:
                    reply = self.t.get_feature(0, codec.REPORT)
                except OSError as e:
                    raise DeviceError(f"reply to 0xAC: {e}") from e
                if reply.startswith(codec.ERASED):
                    self.log(f"screen flash erased in {time.monotonic() - start:.1f} s")
                    break
            else:
                raise DeviceError(
                    f"no 'erased' reply within {timeout:.0f} s (last reply {reply[:8].hex(' ') or 'empty'})"
                )
        self.sleep(1.0)
        self.sync_time()
        return reply

    def live(self, render, fallback: bytes, config: RefreshConfig) -> _Refresh:
        return _Refresh(self, render, config, self.log)

    def recover(self) -> None:
        pass  # no session state to clean up in this family

    def close(self) -> None:
        self.t.close()


ERASE_WARNING = """\
This sends Epomaker's "erase screen flash" command (0xAC). What it does on this keyboard
is not known yet:
  - it may bring back the factory picture, or leave the picture page blank;
  - it may also erase graphics the firmware draws its clock and system pages with,
    which only Epomaker's own software (Windows/macOS) could put back. On an RT100 Pro
    those graphics seem to sit in the same memory as the pictures (an over-long
    animation showed parts of the clock page), so this risk is real.
Keys, lighting and the rest of the keyboard are not touched. Try the safe reset
(without --erase) first."""

ERASE_VERIFIED = """\
This sends Epomaker's "erase screen flash" command (0xAC). On the {name} (firmware 0x0109)
it removed every stored picture and the animation, and kept the clock and system pages.
Keys, lighting and the rest of the keyboard are not touched."""


def erase_notice(model: Model) -> str:
    """What erasing the screen flash (0xAC) is known to do on ``model``."""
    return ERASE_VERIFIED.format(name=model.name) if model.extra.get("erase_verified") else ERASE_WARNING


def capabilities(model: Model) -> Capabilities:
    return Capabilities(
        width=model.width,
        height=model.height,
        frame_height=model.height,
        slots=model.extra["banks"],
        max_frames=model.extra["max_frames"],
        max_fps=250,
        max_animations=1,
        clock=True,
        system_info=True,
        live="refresh",
        verified=model.verified,
    )


class TftDriver:
    """A keyboard of the TFT family; the exact model is known after the first identification."""

    family = "tft"

    def __init__(self, vid: int, pid: int, usb_path: str, model: Model | None = None):
        self.vid, self.pid, self.usb_path = vid, pid, usb_path
        candidates = by_usb_id(vid, pid)
        self.model: Model | None = model or (candidates[0] if len(candidates) == 1 else None)
        self._identified_for: str | None = None

    @property
    def name(self) -> str:
        return (
            self.model.name if self.model else f"Epomaker keyboard {self.vid:04x}:{self.pid:04x} (not identified yet)"
        )

    @property
    def caps(self) -> Capabilities | None:
        return capabilities(self.model) if self.model else None

    def connection_id(self) -> str | None:
        return usb_connection_id(self.usb_path) if self.usb_path else None

    def open(self, log: Log = lambda m: None, transport_factory: Callable | None = None) -> TftSession:
        if transport_factory is None:
            info = command_node(self.usb_path, self.vid, self.pid)
            if info is None:
                raise DeviceError(f"{self.name}: vendor command interface not found (is it connected with a cable?)")
            transport = hid.HidDevice(info, codec.REPORT)
        else:
            transport = transport_factory()
        # Identify once per connection: models share USB ids.
        probe = TftSession(self.model or by_usb_id(self.vid, self.pid)[0], transport, log)
        conn = self.connection_id()
        if self._identified_for is None or conn != self._identified_for:
            try:
                ident = probe.identify()
            except DeviceError:
                transport.close()
                raise
            model = by_internal_id(ident["internal_id"])
            if model is None or (model.vid, model.pid) != (self.vid, self.pid):
                transport.close()
                raise Unsupported(
                    f"keyboard {self.vid:04x}:{self.pid:04x} reports internal id {ident['internal_id']}, "
                    "which is not in the model list. Please report it (epomakerctl probe)."
                )
            self.model, self._identified_for = model, conn
            log(f"identified {model.name} (id {ident['internal_id']}, firmware {ident['firmware']:#06x})")
        return TftSession(self.model, transport, log)
