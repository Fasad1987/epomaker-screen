"""RDM screen family (RT82, QK108, RT98): QGIF animations in the screen controller's flash.

Protocol notes: docs/protocols/rdm.md.
"""

from __future__ import annotations

from ..base import Animation, Capabilities, Log, Model, Progress, usb_connection_id
from . import qgif
from .device import Keyboard, Screen
from .live import LiveConfig, LiveStreamer
from .protocol import MAX_FRAMES, MAX_SLOTS, QGIF_HEIGHT, WIDTH


class _Live:
    """Live mode for one open session: streams frames into the controller's flash."""

    def __init__(self, session: RdmSession, render, fallback: bytes, config: LiveConfig, log: Log):
        self.session = session
        self.streamer = LiveStreamer(session.screen, render, fallback, config, log=log)

    def start(self) -> None:
        kb = Keyboard.open(self.session.model.pid, self.session.usb_path)
        try:
            self.streamer.start(kb)
        finally:
            kb.close()

    def run_for(self, stop, seconds: float = float("inf")) -> None:
        self.streamer.run_for(stop, seconds)

    def stop(self) -> None:
        self.streamer.stop()


class RdmSession:
    def __init__(self, model: Model, usb_path: str, log: Log, screen: Screen | None = None):
        self.model = model
        self.usb_path = usb_path
        self.log = log
        self.screen = screen or Screen.connect(log=log, pid=model.pid, usb_path=usb_path)

    def sync_time(self, t=None) -> None:
        self.screen.sync_time(t)

    def info(self) -> dict:
        return {**self.screen.info(), "link": self.screen.status()}

    def upload(self, items: list[Animation], progress: Progress = lambda done, total: None) -> None:
        if not 1 <= len(items) <= MAX_SLOTS:
            raise ValueError(f"{self.model.name} stores 1 to {MAX_SLOTS} pictures")
        images = [qgif.encode(a.frames, WIDTH, QGIF_HEIGHT, max(1, min(128, a.fps))) for a in items]
        kb = Keyboard.open(self.model.pid, self.usb_path)
        try:
            self.screen.upload(images, kb, progress=progress)
        finally:
            kb.close()

    def live(self, render, fallback: bytes, config: LiveConfig) -> _Live:
        return _Live(self, render, fallback, config, self.log)

    def recover(self) -> None:
        self.screen.recover()

    def close(self) -> None:
        self.screen.close()


class RdmDriver:
    family = "rdm"

    def __init__(self, model: Model, usb_path: str):
        self.model = model
        self.usb_path = usb_path
        self.caps = Capabilities(
            width=WIDTH,
            height=model.height,
            frame_height=QGIF_HEIGHT,
            slots=MAX_SLOTS,
            max_frames=MAX_FRAMES,
            max_fps=30,
            max_animations=MAX_SLOTS,
            clock=True,
            system_info=False,
            live="stream",
            verified=model.verified,
        )

    @property
    def name(self) -> str:
        return self.model.name

    def connection_id(self) -> str | None:
        return usb_connection_id(self.usb_path) if self.usb_path else None

    def open(self, log: Log = lambda m: None) -> RdmSession:
        return RdmSession(self.model, self.usb_path, log)
