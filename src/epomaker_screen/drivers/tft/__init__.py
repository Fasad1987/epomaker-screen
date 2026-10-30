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
# reply, 100 ms before reading a prepare reply, 2 ms between pixel packets.
SEND_DELAY, READ_DELAY, PREPARE_READ_DELAY = 0.01, 0.01, 0.1
CHUNK_DELAY = 0.002
PREPARE_RETRIES = 10


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


@dataclass
class RefreshConfig:
    """Live mode for TFT keyboards: each update rewrites a picture bank."""

    interval_s: float = 60.0  # how often to re-render and send changes
    partial: bool = False  # send only the changed rectangle (experimental: firmware support unverified)
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
        self.caps = capabilities(model)
        self._lock = threading.RLock()

    # -- raw exchange ------------------------------------------------------

    def send(self, pkt: bytes, delay: float = SEND_DELAY) -> None:
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

    def _transfer(self, prepare: bytes, packets: list[bytes], progress: Progress) -> None:
        with self._lock:
            for _attempt in range(PREPARE_RETRIES + 1):
                reply = self.exchange(prepare, PREPARE_READ_DELAY)
                if len(reply) >= 2 and reply[0] == prepare[0] and reply[1] == 1:
                    break
                self.sleep(0.1)
            else:
                raise DeviceError("the keyboard did not accept the picture upload")
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

    def upload_animation(self, frames: list[bytes], fps: int, progress: Progress = lambda d, t: None) -> None:
        maximum = self.model.extra["max_frames"]
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

    def live(self, render, fallback: bytes, config: RefreshConfig) -> _Refresh:
        return _Refresh(self, render, config, self.log)

    def recover(self) -> None:
        pass  # no session state to clean up in this family

    def close(self) -> None:
        self.t.close()


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
