"""Epomaker RT82 screen protocol: ids, commands and packet builders.

The RT82 is two USB devices behind an internal hub:

* the keyboard, 36b0:30a3; its vendor interface (usage page 0xFF60, if 1)
  takes short ``AA cmd`` commands;
* the screen controller, 1919:1919 (usage page 0x00FF, if 1), 64-byte
  reports. It only appears on USB after the keyboard gets ``AA E0`` and
  leaves again ~4 s after the last command.

Screen packets::

    AA cmd addrL addrM addrH len 00 00  payload[56]

Reverse-engineered by guysoft/rt82display (RT82) and wyattades/keyscreen
(QK108, same controller); this is an independent implementation.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass

KEYBOARD_VID, KEYBOARD_PID = 0x36B0, 0x30A3
KEYBOARD_USAGE_PAGE, KEYBOARD_INTERFACE = 0xFF60, 1
SCREEN_VID, SCREEN_PID = 0x1919, 0x1919
SCREEN_USAGE_PAGE, SCREEN_INTERFACE = 0x00FF, 1

SCREEN_REPORT = 64
KEYBOARD_REPORT = 32  # used when the descriptor cannot be read
PAYLOAD = 56
BLOCK = 0x10000  # flash erase block
MAGIC = 0xAA
REJECTED = 0x0F  # reply byte 7

WIDTH, HEIGHT = 240, 135
QGIF_HEIGHT = 136  # QGIF works on 4x4 tiles; the last row is cut off by the panel
MAX_SLOTS = 3  # the RT82 firmware shows up to 3 stored screens


class Kb:
    """Keyboard (36b0) commands."""

    OPEN_SCREEN = 0xE0  # enumerate the screen controller
    SCREEN_MODE = 0xE2  # reply byte 8 == 1: screen on
    SCREEN_COUNT = 0xE3  # number of stored screens, sent before an upload


class Cmd:
    """Screen controller (1919) commands."""

    BEGIN = 0x10
    END = 0x11
    INFO = 0x12
    READ_TABLE = 0x14
    WRITE_TABLE = 0x15
    COMMIT_TABLE = 0x16
    SET_TIME = 0x17
    ERASE = 0x18
    WRITE = 0x19
    INIT_LCD = 0x1A
    START_DOWNLOAD = 0x1B
    STATUS = 0x1C


LINKS = {0: "bluetooth", 1: "2.4g", 2: "usb"}


def packet(cmd: int, payload: bytes | list[int] = b"", length: int = 0, addr: int = 0, size: int = SCREEN_REPORT) -> bytes:
    p = bytearray(size)
    p[0:6] = bytes([MAGIC, cmd, addr & 0xFF, (addr >> 8) & 0xFF, (addr >> 16) & 0xFF, length & 0xFF])
    data = bytes(payload)[: size - 8]
    p[8 : 8 + len(data)] = data
    return bytes(p)


def keyboard_packet(cmd: int, payload: bytes | list[int] = b"", size: int = KEYBOARD_REPORT) -> bytes:
    payload = bytes(payload)
    return packet(cmd, payload, len(payload), size=size)


def time_digits(t: _dt.datetime) -> list[int]:
    """``YYYY MM W DD hh mm ss``, one decimal digit per byte, Sunday = 0."""

    def two(n: int) -> list[int]:
        return [n // 10 % 10, n % 10]

    weekday = (t.weekday() + 1) % 7  # Python: Monday = 0
    y = t.year
    return [y // 1000 % 10, y // 100 % 10, y // 10 % 10, y % 10, *two(t.month), weekday, *two(t.day), *two(t.hour), *two(t.minute), *two(t.second)]


@dataclass(frozen=True)
class Slot:
    """Slot table entry: first 64 KiB block of an image and its length in bytes."""

    start: int
    length: int


def table_pages(slots: list[Slot], allocation: int) -> list[bytes]:
    """The four packets that write and commit a slot table.

    ``allocation`` is the number of blocks in use; the erase command clears
    exactly blocks ``0 .. allocation-1``.
    """
    if not 0 < allocation < 256:
        raise ValueError("allocation must be 1..255 blocks")
    if len(slots) > 10:
        raise ValueError("at most 10 slots")
    table = [0, len(slots), allocation]
    for s in slots:
        if not 0 <= s.start < 256 or not 0 <= s.length < 1 << 24:
            raise ValueError(f"bad slot {s}")
        table += [s.start, 0, s.length & 0xFF, (s.length >> 8) & 0xFF, (s.length >> 16) & 0xFF]
    return [
        packet(Cmd.WRITE_TABLE, table, 0x38),
        packet(Cmd.WRITE_TABLE, b"", 0x38, 0x38),
        packet(Cmd.WRITE_TABLE, b"", 0x10, 0x70),
        packet(Cmd.COMMIT_TABLE, b"", 0x38),
    ]


def blocks_for(nbytes: int) -> int:
    return (nbytes + BLOCK - 1) // BLOCK
