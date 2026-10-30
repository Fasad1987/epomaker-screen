"""What every screen driver provides, and the pieces they share."""

from __future__ import annotations

import contextlib
import fcntl
import os
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Protocol

Log = Callable[[str], None]
Progress = Callable[[int, int], None]
Render = Callable[[float], bytes]  # wall-clock time -> RGB888 frame of the driver's frame size

POWER_CYCLE_HINT = (
    "Power-cycle the keyboard: unplug the cable, switch the keyboard off with its power switch, "
    "wait ~10 s, switch it on and plug it back in. Also close any Epomaker Hub browser tabs."
)


class DeviceError(RuntimeError):
    pass


class ScreenFrozen(DeviceError):
    """The screen controller is on USB but does not answer: only a power cycle helps."""


class Unsupported(DeviceError):
    """The connected model or operation is not supported (yet)."""


@dataclass(frozen=True)
class Capabilities:
    width: int  # visible pixels
    height: int
    frame_height: int  # rows of the RGB888 frames passed to upload() and live render functions
    slots: int  # pictures the keyboard stores
    max_frames: int  # per animation
    max_fps: int
    max_animations: int  # how many of the slots may hold animations
    clock: bool  # the keyboard has its own clock page that can be set
    system_info: bool  # the firmware draws CPU/RAM/... itself when the host sends the values
    live: str  # "stream": frames are streamed without wear concerns; "refresh": each update rewrites flash
    verified: bool  # tried on a real keyboard

    @property
    def frame_bytes(self) -> int:
        return self.width * self.frame_height * 3


@dataclass
class Animation:
    """Frames as packed RGB888 of ``width x frame_height``; one frame is a still."""

    frames: list[bytes]
    fps: int = 1


@dataclass
class SystemInfo:
    """Values for keyboards whose firmware draws a system monitor itself (bytes, percent, °C)."""

    disk_free: int = 0
    disk_total: int = 0
    memory_used: int = 0
    memory_total: int = 0
    cpu_percent: int = 0
    cpu_temperature: int | None = None
    net_up: int = 0
    net_down: int = 0


class LiveSession(Protocol):
    def start(self) -> None: ...
    def run_for(self, stop, seconds: float = ...) -> None: ...
    def stop(self) -> None: ...


class Session(Protocol):
    """An open, exclusive connection to one keyboard's screen."""

    def sync_time(self, t=None) -> None: ...
    def upload(self, items: list[Animation], progress: Progress = ...) -> None: ...
    def info(self) -> dict: ...
    def recover(self) -> None: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class Model:
    key: str  # short id, e.g. "rt82"
    name: str  # "Epomaker RT82"
    family: str  # driver package: "rdm" or "tft"
    vid: int
    pid: int
    width: int
    height: int
    internal_id: int | None = None  # TFT: id reported by the 0x8F command
    verified: bool = False
    notes: str = ""
    extra: dict = field(default_factory=dict, compare=False, hash=False)

    @property
    def usb_id(self) -> str:
        return f"{self.vid:04x}:{self.pid:04x}"


# -- one host at a time ---------------------------------------------------


def _lock_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime or not os.path.isdir(runtime):
        # Not /tmp: another user could create the directory there first.
        runtime = os.path.join(os.path.expanduser("~"), ".cache", "epomaker-screen")
        os.makedirs(runtime, mode=0o700, exist_ok=True)
    st = os.lstat(runtime)
    if st.st_uid != os.getuid() or not os.path.isdir(runtime) or os.path.islink(runtime):
        raise DeviceError(f"{runtime} is not a directory owned by this user")
    return os.path.join(runtime, "epomaker-screen.lock")


@contextlib.contextmanager
def exclusive(wait: float = 10.0) -> Iterator[None]:
    """Serialises access to keyboard screens between processes of this user.

    Screen controllers accept one host at a time; two programs talking to one
    at once can freeze it until a power cycle.
    """
    fd = os.open(_lock_path(), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    try:
        end = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise DeviceError("the screen is busy in another epomaker-screen process (live mode?)") from None
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)


def usb_connection_id(usb_path: str, sysfs: str = "/sys/bus/usb/devices") -> str | None:
    """ "bus:devnum" of a USB device; changes on every re-plug or power cycle."""
    try:
        with open(os.path.join(sysfs, usb_path, "busnum")) as f:
            bus = f.read().strip()
        with open(os.path.join(sysfs, usb_path, "devnum")) as f:
            dev = f.read().strip()
    except OSError:
        return None
    return f"{bus}:{dev}"
