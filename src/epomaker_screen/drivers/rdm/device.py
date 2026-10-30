"""RDM family: the keyboard (e.g. RT82, 36b0:30a3) and its screen controller (1919:1919)."""

from __future__ import annotations

import contextlib
import datetime as dt
import time
from collections.abc import Iterator
from typing import Callable, Protocol

from ... import hid as hidraw
from ..base import POWER_CYCLE_HINT, DeviceError, Log, ScreenFrozen
from .protocol import (
    BLOCK,
    KEYBOARD_INTERFACE,
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

# Erase timing measured on this controller (keyscreen): ~137 ms per 64 KiB block
# holding data, ~56 ms per blank one. Erases over ~5 s made it drop off USB;
# ones up to ~4 s never did.
ERASE_S_DATA, ERASE_S_BLANK, MAX_ERASE_S = 0.14, 0.06, 3.4

# Reply timeouts. Table commits and LCD re-inits touch flash and can be slow.
DEFAULT_TIMEOUT = 2.0
SLOW_COMMANDS = {Cmd.COMMIT_TABLE: 5.0, Cmd.INIT_LCD: 5.0, Cmd.WRITE_TABLE: 3.0, Cmd.SET_TIME: 3.0}

DEFAULT_PID = 0x30A3  # RT82


class Transport(Protocol):
    def write(self, data: bytes) -> None: ...
    def read_until(self, match: Callable[[bytes], bool], timeout: float) -> bytes: ...
    def drain(self) -> None: ...
    def close(self) -> None: ...


def _same_hub(screen_usb: str, keyboard_usb: str) -> bool:
    """The screen controller sits on the keyboard's internal hub: 3-9.1 next to 3-9.3."""
    if not keyboard_usb or not screen_usb:
        return True
    hub = keyboard_usb.rsplit(".", 1)[0] if "." in keyboard_usb else keyboard_usb
    return screen_usb.startswith(hub + ".") and screen_usb != keyboard_usb


def _echo(cmd: int) -> Callable[[bytes], bool]:
    return lambda r: len(r) > 1 and r[0] == MAGIC and r[1] == cmd


def erase_steps(used: int, budget: float = MAX_ERASE_S) -> list[int]:
    """Erase counts that clear blocks 0..used-1 with each erase under ``budget`` seconds.

    Assumes every block not yet erased holds data (the slow case).
    """
    steps, done = [], 0
    while done < used:
        n = done + max(1, int((budget - ERASE_S_BLANK * done) / ERASE_S_DATA))
        done = min(used, n)
        steps.append(done)
    return steps


class Keyboard:
    """Vendor interface of the keyboard (e.g. 36b0:30a3, usage page 0xFF60)."""

    def __init__(self, transport: Transport, report_size: int = KEYBOARD_REPORT):
        self.t = transport
        self.report_size = report_size

    @classmethod
    def open(cls, pid: int = DEFAULT_PID, usb_path: str = "") -> Keyboard:
        devices = [d for d in hidraw.enumerate_hidraw() if not usb_path or d.usb_path == usb_path]
        info = hidraw.find(KEYBOARD_VID, pid, KEYBOARD_USAGE_PAGE, KEYBOARD_INTERFACE, devices=devices)
        if info is None:
            raise DeviceError(f"keyboard {KEYBOARD_VID:04x}:{pid:04x} not found. Is it connected with a USB cable?")
        size = info.out_size or KEYBOARD_REPORT
        return cls(hidraw.HidDevice(info, size), size)

    def close(self) -> None:
        self.t.close()

    def send(self, cmd: int, payload: bytes | list[int] = b"", timeout: float = 1.0) -> bytes | None:
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
    """The screen controller (1919:1919, usage page 0x00FF).

    Never use download mode (``1B``) here: entering it repeatedly freezes the
    controller on the next table commit until the keyboard is power-cycled
    (seen on the RT82 after three uploads in four minutes, and reproduced by
    keyscreen on the QK108). Everything below uses plain ``10``/``11``
    sessions, which keyscreen ran 150 times in a row without a failure.
    """

    def __init__(self, transport: Transport, log: Log = lambda m: None):
        self.t = transport
        self.log = log
        self.write_window = 2  # FLASH_WRITE packets in flight

    # -- connection ------------------------------------------------------

    @classmethod
    def connect(
        cls,
        timeout: float = 15.0,
        log: Log = lambda m: None,
        probe: bool = True,
        pid: int = DEFAULT_PID,
        usb_path: str = "",
    ) -> Screen:
        """Opens the controller, asking the keyboard to enumerate it first.

        ``usb_path`` is the keyboard's USB device; the controller is looked for
        on the same internal hub, so two keyboards of this family can coexist.
        """
        end = time.monotonic() + timeout
        last_error: Exception | None = None
        while time.monotonic() < end:
            devices = [d for d in hidraw.enumerate_hidraw() if _same_hub(d.usb_path, usb_path)]
            info = hidraw.find(SCREEN_VID, SCREEN_PID, SCREEN_USAGE_PAGE, SCREEN_INTERFACE, devices=devices)
            if info is not None:
                try:
                    dev = hidraw.HidDevice(info, info.out_size or SCREEN_REPORT)
                except PermissionError:
                    raise
                except OSError as e:  # still settling after enumeration
                    last_error = e
                else:
                    dev.drain()
                    log(f"screen opened: {info.node}")
                    screen = cls(dev, log)
                    if probe:
                        try:
                            screen.probe()
                        except DeviceError:
                            screen.close()
                            raise
                    return screen
            else:
                kb = Keyboard.open(pid, usb_path)
                try:
                    kb.open_screen()
                finally:
                    kb.close()
                log("sent AA E0, waiting for the screen controller…")
            time.sleep(0.4)
        raise DeviceError(
            "the screen controller (1919:1919) did not appear. The keyboard must be connected with a cable; "
            f"close Epomaker Hub tabs. {last_error or ''}".strip()
        )

    def probe(self) -> None:
        """Checks that the controller answers, recovering an interrupted session if needed."""
        try:
            self.cmd(Cmd.STATUS)
            return
        except DeviceError:
            self.log("the screen did not answer; trying to close an interrupted session")
        self.recover()
        try:
            self.cmd(Cmd.STATUS, timeout=3.0)
        except DeviceError as e:
            raise ScreenFrozen(f"the screen controller does not answer. {POWER_CYCLE_HINT}") from e

    def recover(self) -> None:
        """Best effort: closes whatever session a failed sequence left open."""
        for cmd in (Cmd.END, Cmd.INIT_LCD, Cmd.END):
            try:
                self.t.drain()
                self.cmd(cmd, timeout=1.5)
            except DeviceError:
                pass

    def close(self) -> None:
        self.t.close()

    # -- commands --------------------------------------------------------

    def send(self, pkt: bytes, timeout: float | None = None) -> bytes:
        cmd = pkt[1]
        if timeout is None:
            timeout = SLOW_COMMANDS.get(cmd, DEFAULT_TIMEOUT)
        self.t.write(pkt)
        try:
            r = self.t.read_until(_echo(cmd), timeout)
        except hidraw.HidTimeout as e:
            raise DeviceError(f"screen command 0x{cmd:02X}: no reply") from e
        if len(r) > 7 and r[7] == REJECTED:
            raise DeviceError(f"the screen rejected command 0x{cmd:02X}")
        return r

    def cmd(
        self, cmd: int, payload: bytes | list[int] = b"", length: int = 0, addr: int = 0, timeout: float | None = None
    ) -> bytes:
        return self.send(packet(cmd, payload, length, addr), timeout)

    @contextlib.contextmanager
    def session(self) -> Iterator[None]:
        """A plain ``10`` … ``11`` session; closed even when a command inside fails."""
        self.cmd(Cmd.BEGIN)
        ok = False
        try:
            yield
            ok = True
        finally:
            if ok:
                self.cmd(Cmd.END)
            else:
                try:
                    self.cmd(Cmd.END, timeout=1.5)
                except DeviceError:
                    pass

    def status(self) -> str:
        r = self.cmd(Cmd.STATUS)
        return LINKS.get(r[9], f"unknown({r[9]})")

    def sync_time(self, t: dt.datetime | None = None) -> None:
        t = t or dt.datetime.now()
        with self.session():
            self.cmd(Cmd.SET_TIME, time_digits(t), 0x38)

    def info(self) -> dict:
        with self.session():
            r = self.cmd(Cmd.INFO, length=0x38)
        u16 = lambda i: r[i] | (r[i + 1] << 8)  # noqa: E731
        return {"width": u16(20), "height": u16(22), "rotation": r[26]}

    def write_table(self, slots: list[Slot], allocation: int) -> None:
        for p in table_pages(slots, allocation):
            self.send(p)

    def flip(self, slots: list[Slot], allocation: int) -> None:
        """Points the slots at stored images and redraws, without "Downloading"."""
        with self.session():
            self.write_table(slots, allocation)
        self.cmd(Cmd.INIT_LCD)

    def erase_first(self, count: int, restore: list[Slot], allocation: int) -> float:
        """Erases blocks 0..count-1 in a plain session (no "Downloading").

        The erase follows the table's allocation, not its own argument, so the
        table is written with ``count`` first and ``restore`` is put back
        afterwards without a redraw. Returns the erase time in seconds. Keep
        erases under ~3.5 s: the controller does not answer USB while erasing
        and a long erase can make it drop off the bus.
        """
        t0 = time.monotonic()
        with self.session():
            self.write_table([Slot(0, restore[0].length if restore else 0)], count)
            self.cmd(Cmd.ERASE, [count], 1, timeout=4.0 + 0.5 * count)
        took = time.monotonic() - t0
        with self.session():
            self.write_table(restore, allocation)
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
                r = self.t.read_until(_echo(Cmd.WRITE), DEFAULT_TIMEOUT)
            except hidraw.HidTimeout as e:
                raise DeviceError(f"flash write at 0x{addr:06X}: no reply") from e
            if len(r) > 7 and r[7] == REJECTED:
                raise DeviceError(f"the screen rejected a flash write at 0x{addr:06X}")
            acked += 1

    def upload(
        self,
        images: list[bytes],
        keyboard: Keyboard | None = None,
        leading_blocks: int = 0,
        progress: Callable[[int, int], None] = lambda done, total: None,
    ) -> list[Slot]:
        """Replaces all stored screens.

        Each image starts on a 64 KiB block. ``leading_blocks`` leaves erased
        blocks in front of the images (used by live mode). The screen shows
        white while its flash is erased, then the first image.
        """
        if not 1 <= len(images) <= MAX_SLOTS:
            raise ValueError(f"1 to {MAX_SLOTS} images are supported")
        slots, block = [], leading_blocks
        for img in images:
            slots.append(Slot(block, len(img)))
            block += blocks_for(len(img))
        allocation = block
        total = sum(len(i) for i in images)

        own_kb = keyboard is None
        kb = keyboard or Keyboard.open()
        try:
            kb.set_screen_count(len(images))
        finally:
            if own_kb:
                kb.close()

        try:
            for n in erase_steps(allocation):
                took = self.erase_first(n, slots, allocation)
                self.log(f"erased blocks 0..{n - 1} in {took:.1f} s")
            done = 0
            for img, slot in zip(images, slots):
                base = slot.start * BLOCK
                for off in range(0, len(img), PAYLOAD * 32):
                    part = img[off : off + PAYLOAD * 32]
                    self.write_at(base + off, part)
                    done += len(part)
                    progress(done, total)
            self.flip(slots, allocation)
            self.sync_time()
        except DeviceError:
            self.recover()
            raise
        return slots
