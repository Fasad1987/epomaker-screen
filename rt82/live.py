"""Live mode: keep computer-generated frames on the screen without "Downloading".

The screen plays slot 1 from flash and reads frame records progressively, so
records appended to the playing QGIF are shown in turn. This module keeps a
growing QGIF a few frames ahead of the playhead.

Flash layout (64 KiB blocks)::

    [0 .. area)  stream area: a sequence of segments, each a growing QGIF
    [area]       fallback still, shown only while the stream area is erased

* Each segment lives ``segment_seconds`` (the screen plays slightly slower
  than declared, so a fresh segment resets the drift), then the next one is
  prepared right after it and the table is flipped to it (~0.15 s, no
  "Downloading").
* When the area is full, the screen shows the fallback while the area is
  erased (a few seconds), and streaming restarts at block 0. Flash can only
  erase blocks ``0..N-1``, which is why the fallback sits after the area.
* With the defaults (1 fps, 24 blocks) an unchanged frame costs 255 bytes,
  so the area is erased roughly once an hour: ~9000 erase cycles a year,
  far below what SPI NOR flash is rated for (~100k).

Frames are rendered for the moment they will be on screen, so a clock is on
time even though records are written ``lead_frames`` ahead.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from . import qgif
from .device import DeviceError, Keyboard, Screen
from .protocol import BLOCK, QGIF_HEIGHT, WIDTH, Slot, blocks_for

Render = Callable[[float], bytes]  # wall-clock time -> 240x136 RGB888

FULL_FRAME = qgif.TileEncoder(WIDTH, QGIF_HEIGHT).bitmap_len + 8 * (WIDTH // 4) * (QGIF_HEIGHT // 4)
# Measured on the same controller (keyscreen): ~137 ms per block holding data, ~56 ms per blank one.
# Erases over ~5 s made it drop off USB; ones up to ~4 s never did.
ERASE_S_DATA, ERASE_S_BLANK, MAX_ERASE_S = 0.14, 0.06, 3.4


@dataclass
class LiveConfig:
    fps: int = 1
    area_blocks: int = 24
    segment_seconds: float = 300.0
    lead_frames: int = 5  # the screen reads ~3 frames ahead of what it shows
    slowdown: float = 1.0  # declared-to-real frame time ratio; keep <= the real one

    def check(self) -> None:
        if not 1 <= self.fps <= 10:
            raise ValueError("fps: 1..10")
        if not 4 <= self.area_blocks <= 28:
            raise ValueError("area_blocks: 4..28")
        if self.lead_frames < 4:
            raise ValueError("lead_frames >= 4")


@dataclass
class _Segment:
    start: int  # block
    capacity: int  # bytes
    stream: qgif.Stream = field(default_factory=lambda: qgif.Stream(WIDTH, QGIF_HEIGHT))
    offset: int = 0
    written: int = 0  # frames
    t0: float = 0.0  # monotonic time frame 0 went on screen
    wall0: float = 0.0  # wall-clock time of frame 0

    @property
    def addr(self) -> int:
        return self.start * BLOCK + self.offset

    @property
    def end_block(self) -> int:
        return self.start + blocks_for(self.offset)

    def slot(self) -> Slot:
        return Slot(self.start, self.capacity)


def erase_steps(used: int, budget: float = MAX_ERASE_S) -> list[int]:
    """Erase counts that clear blocks 0..used-1 with each erase under ``budget``."""
    steps, done = [], 0
    while done < used:
        n = done + max(1, int((budget - ERASE_S_BLANK * done) / ERASE_S_DATA))
        done = min(used, n)
        steps.append(done)
    return steps


class LiveStreamer:
    def __init__(
        self,
        screen: Screen,
        render: Render,
        fallback: bytes,
        config: Optional[LiveConfig] = None,
        log: Callable[[str], None] = lambda m: None,
        monotonic: Callable[[], float] = time.monotonic,
        wall: Callable[[], float] = time.time,
    ):
        self.s = screen
        self.render = render
        self.fallback_rgb = fallback
        self.cfg = config or LiveConfig()
        self.cfg.check()
        self.log = log
        self.now = monotonic
        self.wall = wall
        self.seg: Optional[_Segment] = None
        self.used_blocks = 0  # blocks of the area that hold data
        self.allocation = 0
        self.fallback_slot: Optional[Slot] = None
        self.stats = {"frames": 0, "segments": 0, "erases": 0, "underruns": 0}

    @property
    def frame_s(self) -> float:
        return self.cfg.slowdown / self.cfg.fps

    # -- setup -----------------------------------------------------------

    def start(self, keyboard: Optional[Keyboard] = None) -> None:
        """Wipes the screen's flash and starts streaming (shows "Downloading" once)."""
        area = self.cfg.area_blocks
        fallback = qgif.encode([self.fallback_rgb], WIDTH, QGIF_HEIGHT, fps=1)
        (self.fallback_slot,) = self.s.upload([fallback], keyboard, leading_blocks=area)
        self.allocation = area + blocks_for(len(fallback)) + 1
        self.used_blocks = 0
        self._begin_segment(0)

    def _begin_segment(self, block: int) -> None:
        seg = _Segment(block, (self.cfg.area_blocks - block) * BLOCK)
        show_at = self.wall() + 0.5  # rough: preparing takes about this long
        data = bytearray(seg.stream.header(0xFFFF, self.cfg.fps))
        for k in range(self.cfg.lead_frames + 1):
            data += seg.stream.next(self.render(show_at + k * self.frame_s))
        self.s.write_at(seg.addr, bytes(data))
        seg.offset, seg.written = len(data), self.cfg.lead_frames + 1
        self.s.flip([seg.slot()], self.allocation)
        seg.t0, seg.wall0 = self.now(), show_at
        self.seg = seg
        self.used_blocks = max(self.used_blocks, seg.end_block)
        self.stats["segments"] += 1
        self.log(f"live: сегмент с блока {block}")

    # -- streaming -------------------------------------------------------

    def _playhead(self) -> float:
        assert self.seg
        return (self.now() - self.seg.t0) / self.frame_s

    def _feed(self, ahead: int) -> bool:
        """Writes frames until ``ahead`` frames are buffered. False when the segment is full."""
        seg = self.seg
        assert seg
        while seg.written < self._playhead() + ahead:
            if seg.capacity - seg.offset < FULL_FRAME:
                return False
            rec = seg.stream.next(self.render(seg.wall0 + seg.written * self.frame_s))
            self.s.write_at(seg.addr, rec)
            seg.offset += len(rec)
            seg.written += 1
            self.stats["frames"] += 1
        self.used_blocks = max(self.used_blocks, seg.end_block)
        return True

    def tick(self) -> None:
        seg = self.seg
        if seg is None:
            raise RuntimeError("start() first")
        if seg.written - self._playhead() < 2:
            # Fell behind (system suspend, slow USB): the screen may be reading
            # unwritten flash. A fresh segment puts a clean picture back.
            self.stats["underruns"] += 1
            self.log("live: буфер опустел, начинаю новый сегмент")
            self._rotate(feed_current=False)
            return
        full = not self._feed(self.cfg.lead_frames)
        old = (self.now() - seg.t0) >= self.cfg.segment_seconds
        low = seg.capacity - seg.offset < 3 * FULL_FRAME
        if full or old or low:
            self._rotate(feed_current=not full)

    def _rotate(self, feed_current: bool) -> None:
        seg = self.seg
        assert seg
        nxt = seg.end_block
        if self.cfg.area_blocks - nxt >= 2:
            if feed_current:
                # Buffer enough for the time preparing the next segment takes.
                self._feed(self.cfg.lead_frames + 3 * self.cfg.fps + 2)
            self._begin_segment(nxt)
            return
        assert self.fallback_slot
        self.s.flip([self.fallback_slot], self.allocation)
        for n in erase_steps(self.used_blocks):
            took = self.s.erase_first(n, [self.fallback_slot], self.allocation)
            self.log(f"live: стёрты блоки 0..{n - 1} за {took:.1f} с")
        self.stats["erases"] += 1
        self.used_blocks = 0
        self._begin_segment(0)

    def run_for(self, stop: threading.Event, seconds: float = math.inf) -> None:
        """Streams until ``stop`` is set or ``seconds`` pass. Raises DeviceError if the screen goes away."""
        end = self.now() + seconds
        while not stop.is_set() and self.now() < end:
            self.tick()
            seg = self.seg
            assert seg
            # Wake up shortly after the next frame boundary.
            nxt = seg.t0 + (math.floor(self._playhead()) + 1) * self.frame_s
            stop.wait(max(0.02, min(1.0, nxt - self.now())))

    def stop(self) -> None:
        """Leaves a still of the current picture on the screen.

        A stream left alone would play into erased flash (white), so the
        current frame is stored as a looping still in the next free block.
        """
        seg = self.seg
        if seg is None:
            return
        self.seg = None
        try:
            block = seg.end_block
            if block < self.cfg.area_blocks:
                still = qgif.encode([self.render(self.wall())], WIDTH, QGIF_HEIGHT, fps=1)
                self.s.write_at(block * BLOCK, still)
                self.s.flip([Slot(block, len(still))], self.allocation)
            elif self.fallback_slot:
                self.s.flip([self.fallback_slot], self.allocation)
        except DeviceError:
            pass
