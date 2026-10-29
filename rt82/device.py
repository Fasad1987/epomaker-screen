"""High-level access to the RT82 keyboard and its screen controller."""

from __future__ import annotations

import contextlib
import datetime as dt
import fcntl
import os
import time
from typing import Callable, Iterator, Optional, Protocol

from . import hidraw
from .protocol import (
    BLOCK,
    KEYBOARD_INTERFACE,
    KEYBOARD_PID,
    KEYBOARD_REPORT,
    KEYBOARD_USAGE_PAGE,
    KEYBOARD_VID,
    LINKS,
    MAGIC,
    MAX_SLOTS,
    PAYLOAD,
    REJECTED,
    SCREEN_INTERFACE,
    SCREEN_PID,
    SCREEN_REPORT,
    SCREEN_USAGE_PAGE,
    SCREEN_VID,
    Cmd,
    Kb,
    Slot,
    blocks_for,
    keyboard_packet,
    packet,
    table_pages,
    time_digits,
)

Log = Callable[[str], None]


class DeviceError(RuntimeError):
    pass


class Transport(Protocol):
    def write(self, data: bytes) -> None: ...
    def read_until(self, match: Callable[[bytes], bool], timeout: float) -> bytes: ...
    def drain(self) -> None: ...
    def close(self) -> None: ...


def keyboard_present() -> bool:
    return hidraw.present(KEYBOARD_VID, KEYBOARD_PID)


def _echo(cmd: int) -> Callable[[bytes], bool]:
    return lambda r: len(r) > 1 and r[0] == MAGIC and r[1] == cmd


class Keyboard:
    """Vendor interface of the keyboard (36b0:30a3, usage page 0xFF60)."""

    def __init__(self, transport: Transport, report_size: int = KEYBOARD_REPORT):
        self.t = transport
        self.report_size = report_size

    @classmethod
    def open(cls) -> "Keyboard":
        info = hidraw.find(KEYBOARD_VID, KEYBOARD_PID, KEYBOARD_USAGE_PAGE, KEYBOARD_INTERFACE)
        if info is None:
            raise DeviceError("клавиатура RT82 (36b0:30a3) не найдена. Подключена ли она кабелем USB?")
        size = info.out_size or KEYBOARD_REPORT
        return cls(hidraw.HidDevice(info, size), size)

    def close(self) -> None:
        self.t.close()

    def send(self, cmd: int, payload: bytes | list[int] = b"", timeout: float = 1.0) -> Optional[bytes]:
        self.t.drain()
        self.t.write(keyboard_packet(cmd, payload, self.report_size))
        try:
            return self.t.read_until(_echo(cmd), timeout)
        except hidraw.HidTimeout:
            return None  # some firmware versions do not answer E0

    def open_screen(self) -> None:
        self.send(Kb.OPEN_SCREEN, timeout=0.3)

    def set_screen_count(self, n: int) -> None:
        self.send(Kb.SCREEN_COUNT, [n])


class Screen:
    """The screen controller (1919:1919, usage page 0x00FF)."""

    def __init__(self, transport: Transport, log: Log = lambda m: None):
        self.t = transport
        self.log = log
        self.write_window = 2  # FLASH_WRITE packets in flight

    # -- connection ------------------------------------------------------

    @classmethod
    def connect(cls, timeout: float = 15.0, log: Log = lambda m: None) -> "Screen":
        """Opens the controller, asking the keyboard to enumerate it first."""
        end = time.monotonic() + timeout
        last_error = None
        while time.monotonic() < end:
            info = hidraw.find(SCREEN_VID, SCREEN_PID, SCREEN_USAGE_PAGE, SCREEN_INTERFACE)
            if info is not None:
                try:
                    dev = hidraw.HidDevice(info, info.out_size or SCREEN_REPORT)
                    dev.drain()
                    log(f"экран открыт: {info.node}")
                    return cls(dev, log)
                except PermissionError:
                    raise
                except OSError as e:  # still settling after enumeration
                    last_error = e
            else:
                kb = Keyboard.open()
                try:
                    kb.open_screen()
                finally:
                    kb.close()
                log("отправлен AA E0, жду появления контроллера экрана…")
            time.sleep(0.4)
        raise DeviceError(
            "контроллер экрана (1919:1919) не появился. Клавиатура должна быть подключена кабелем; "
            f"закройте вкладки Epomaker Hub. {last_error or ''}".strip()
        )

    def close(self) -> None:
        self.t.close()

    # -- commands --------------------------------------------------------

    def send(self, pkt: bytes, timeout: float = 1.0) -> bytes:
        cmd = pkt[1]
        self.t.write(pkt)
        try:
            r = self.t.read_until(_echo(cmd), timeout)
        except hidraw.HidTimeout as e:
            raise DeviceError(f"команда экрана 0x{cmd:02X}: нет ответа") from e
        if len(r) > 7 and r[7] == REJECTED:
            raise DeviceError(f"экран отклонил команду 0x{cmd:02X}")
        return r

    def cmd(self, cmd: int, payload: bytes | list[int] = b"", length: int = 0, addr: int = 0, timeout: float = 1.0) -> bytes:
        return self.send(packet(cmd, payload, length, addr), timeout)

    def status(self) -> str:
        r = self.cmd(Cmd.STATUS)
        return LINKS.get(r[9], f"unknown({r[9]})")

    def sync_time(self, t: Optional[dt.datetime] = None) -> None:
        t = t or dt.datetime.now()
        self.cmd(Cmd.BEGIN)
        self.cmd(Cmd.SET_TIME, time_digits(t), 0x38)
        self.cmd(Cmd.END)

    def info(self) -> dict:
        self.cmd(Cmd.BEGIN)
        r = self.cmd(Cmd.INFO, length=0x38)
        self.cmd(Cmd.END)
        u16 = lambda i: r[i] | (r[i + 1] << 8)  # noqa: E731
        return {"width": u16(20), "height": u16(22), "rotation": r[26]}

    def write_table(self, slots: list[Slot], allocation: int) -> None:
        for p in table_pages(slots, allocation):
            self.send(p)

    def flip(self, slots: list[Slot], allocation: int) -> None:
        """Points the slots at stored images and redraws, without "Downloading"."""
        self.cmd(Cmd.BEGIN)
        self.write_table(slots, allocation)
        self.cmd(Cmd.END)
        self.cmd(Cmd.INIT_LCD)

    def erase_first(self, count: int, restore: list[Slot], allocation: int) -> float:
        """Erases blocks 0..count-1 in a plain session (no "Downloading").

        The erase follows the table's allocation, so the table is written with
        ``count`` first and ``restore`` is put back afterwards without a redraw.
        Returns the erase time in seconds. Keep erases under ~3.5 s: the
        controller does not answer USB while erasing and a long erase can make
        it drop off the bus.
        """
        t0 = time.monotonic()
        self.cmd(Cmd.BEGIN)
        self.write_table([Slot(0, restore[0].length if restore else 0)], count)
        self.cmd(Cmd.ERASE, [count], 1, timeout=4.0 + 0.5 * count)
        took = time.monotonic() - t0
        self.cmd(Cmd.END)
        self.cmd(Cmd.BEGIN)
        self.write_table(restore, allocation)
        self.cmd(Cmd.END)
        return took

    def write_at(self, addr: int, data: bytes) -> None:
        """FLASH_WRITE into already-erased flash, pipelined."""
        offsets = list(range(0, len(data), PAYLOAD))
        sent = acked = 0
        while acked < len(offsets):
            while sent < len(offsets) and sent - acked < self.write_window:
                off = offsets[sent]
                chunk = data[off : off + PAYLOAD]
                self.t.write(packet(Cmd.WRITE, chunk, len(chunk), addr + off))
                sent += 1
            try:
                r = self.t.read_until(_echo(Cmd.WRITE), 1.0)
            except hidraw.HidTimeout as e:
                raise DeviceError(f"запись во flash по адресу 0x{addr:06X}: нет ответа") from e
            if len(r) > 7 and r[7] == REJECTED:
                raise DeviceError(f"экран отклонил запись по адресу 0x{addr:06X}")
            acked += 1

    def upload(
        self,
        images: list[bytes],
        keyboard: Optional[Keyboard] = None,
        leading_blocks: int = 0,
        progress: Callable[[int, int], None] = lambda done, total: None,
    ) -> list[Slot]:
        """Replaces all stored screens, the way Epomaker's web tool does it.

        Shows "Downloading" on the keyboard while it runs. Each image starts
        on a 64 KiB block. ``leading_blocks`` leaves erased blocks in front of
        the images (used by live mode).
        """
        if not 1 <= len(images) <= MAX_SLOTS:
            raise ValueError(f"нужно от 1 до {MAX_SLOTS} изображений")
        slots, block = [], leading_blocks
        for img in images:
            slots.append(Slot(block, len(img)))
            block += blocks_for(len(img))
        allocation = block + 1  # one spare block, like the official tool
        total = sum(len(i) for i in images)

        own_kb = keyboard is None
        kb = keyboard or Keyboard.open()
        try:
            kb.set_screen_count(len(images))
        finally:
            if own_kb:
                kb.close()

        self.cmd(Cmd.START_DOWNLOAD, length=0x38)
        self.cmd(Cmd.READ_TABLE, length=0x38)
        self.write_table(slots, allocation)
        # The reply comes after the erase; both known tools also wait afterwards.
        self.cmd(Cmd.ERASE, [allocation], 1, timeout=2.0 + 1.0 * allocation)
        time.sleep(0.5 * allocation)

        done = 0
        for img, slot in zip(images, slots):
            base = slot.start * BLOCK
            for off in range(0, len(img), PAYLOAD * 32):
                part = img[off : off + PAYLOAD * 32]
                self.write_at(base + off, part)
                done += len(part)
                progress(done, total)
        time.sleep(0.5)
        self.cmd(Cmd.INIT_LCD)
        self.sync_time()
        self.cmd(Cmd.END)
        return slots


# -- one host at a time ---------------------------------------------------


def _lock_path() -> str:
    runtime = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/rt82-{os.getuid()}"
    os.makedirs(runtime, mode=0o700, exist_ok=True)
    return os.path.join(runtime, "rt82-screen.lock")


@contextlib.contextmanager
def exclusive(wait: float = 10.0) -> Iterator[None]:
    """Serialises access to the screen between rt82 processes of this user.

    The controller accepts only one host; two programs talking to it at once
    can freeze it until a power cycle.
    """
    fd = os.open(_lock_path(), os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    try:
        end = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= end:
                    raise DeviceError("экран занят другим процессом rt82 (например, живым режимом)")
                time.sleep(0.1)
        yield
    finally:
        os.close(fd)


@contextlib.contextmanager
def screen_session(log: Log = lambda m: None, wait: float = 10.0) -> Iterator[Screen]:
    with exclusive(wait):
        screen = Screen.connect(log=log)
        try:
            yield screen
        finally:
            screen.close()
