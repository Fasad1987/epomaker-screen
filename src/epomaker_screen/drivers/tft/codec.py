"""TFT family packets: 64-byte feature reports on the vendor collection.

Layout of every command::

    byte 0      command
    bytes 1..6  parameters
    byte 7      checksum: 255 - (sum(bytes 0..6) & 255)
    bytes 8..63 payload

Byte layouts were recovered from Epomaker's installers by
ramarivera/epomaker-driver-linux (docs/glyph-protocol.md, docs/display.md
there). See docs/protocols/tft.md in this repository.
"""

from __future__ import annotations

import datetime as dt
import struct

REPORT = 64
PAYLOAD = 56  # pixel bytes per data packet

IDENTIFY = 0x8F
SYSTEM_INFO = 0x22
PREPARE_565, DATA_565 = 0xA5, 0x25
PREPARE_888, DATA_888 = 0xA9, 0x29
LANGUAGE = 0x27
CLOCK = 0x28

# Commands this project sends. Anything else (factory reset 0x01, screen flash
# erase 0xAC, firmware update 0xBA/0x30-0x32, key maps ...) is refused, so a bug
# cannot reconfigure or brick a keyboard.
ALLOWED = frozenset({IDENTIFY, SYSTEM_INFO, PREPARE_565, DATA_565, PREPARE_888, DATA_888, LANGUAGE, CLOCK})

GIB = 1024**3


def packet(header: bytes | list[int], payload: bytes = b"", checksum_at: int = 7) -> bytes:
    p = bytearray(REPORT)
    h = bytes(header)
    if not 1 <= len(h) <= checksum_at:
        raise ValueError("header must be 1..7 bytes")
    if h[0] not in ALLOWED:
        raise ValueError(f"command 0x{h[0]:02X} is not allowed by this driver")
    p[: len(h)] = h
    p[checksum_at] = 255 - (sum(p[:checksum_at]) & 255)
    if len(payload) > REPORT - 8:
        raise ValueError("payload too long")
    p[8 : 8 + len(payload)] = payload
    return bytes(p)


def identify() -> bytes:
    return packet([IDENTIFY])


def parse_identity(reply: bytes) -> dict:
    if len(reply) < 12 or reply[0] != IDENTIFY:
        raise ValueError(f"unexpected identity reply: {bytes(reply[:12]).hex()}")
    return {
        "internal_id": struct.unpack_from("<I", reply, 1)[0],
        "firmware": struct.unpack_from("<H", reply, 7)[0],
        "boot_mode": reply[9] == 1,
    }


def clock(t: dt.datetime) -> bytes:
    return packet([CLOCK], struct.pack(">H5B", t.year, t.month, t.day, t.hour, t.minute, t.second))


def system_info(
    disk_free: int,
    disk_total: int,
    memory_used: int,
    memory_total: int,
    cpu_percent: int,
    cpu_temperature: int | None,
    net_up: int,
    net_down: int,
) -> bytes:
    """Values the firmware draws on its own system page. Sizes in bytes, sent as GiB."""

    def gib(v: int) -> int:
        return max(0, min(0xFFFF, (int(v) + GIB // 2) // GIB))

    cpu = max(0, min(100, int(cpu_percent)))
    temp = 0 if cpu_temperature is None else max(0, min(255, int(cpu_temperature)))
    return packet(
        [SYSTEM_INFO],
        struct.pack("<4HBB2H", gib(disk_free), gib(disk_total), gib(memory_used), gib(memory_total), cpu, temp, gib(net_up), gib(net_down)),
    )  # fmt: skip


def rgb565_column_major(rgb: bytes, width: int, height: int, box: tuple[int, int, int, int] | None = None) -> bytes:
    """RGB888 row-major frame -> RGB565 big-endian, column by column (x outer, y inner).

    ``box`` (left, top, right, bottom; right/bottom exclusive) selects a region.
    """
    left, top, right, bottom = box or (0, 0, width, height)
    out = bytearray((right - left) * (bottom - top) * 2)
    stride = width * 3
    i = 0
    for x in range(left, right):
        o = (top * width + x) * 3
        for _ in range(top, bottom):
            r, g, b = rgb[o], rgb[o + 1], rgb[o + 2]
            v = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
            out[i] = v >> 8
            out[i + 1] = v & 0xFF
            i += 2
            o += stride
    return bytes(out)


def rgb888_column_major(rgb: bytes, width: int, height: int, box: tuple[int, int, int, int] | None = None) -> bytes:
    left, top, right, bottom = box or (0, 0, width, height)
    return b"".join(
        rgb[(y * width + x) * 3 : (y * width + x) * 3 + 3] for x in range(left, right) for y in range(top, bottom)
    )


def prepare(
    data_length: int,
    box: tuple[int, int, int, int],
    frame: int = 0,
    frames: int = 1,
    delay_ms: int = 0,
    rgb24: bool = False,
) -> bytes:
    """Announces a picture upload. ``frame`` is the bank (0..4) for a still, the index for an animation."""
    left, top, right, bottom = box
    if not (0 <= left < right and 0 <= top < bottom):
        raise ValueError(f"bad box {box}")
    for v in (frame, frames, delay_ms):
        if not 0 <= v <= 255:
            raise ValueError("frame metadata must fit in a byte")
    header = [
        PREPARE_888 if rgb24 else PREPARE_565,
        frame,
        frames,
        delay_ms,
        data_length & 0xFF,
        (data_length >> 8) & 0xFF,
        0,
    ]
    payload = bytes(
        [left & 0xFF, top & 0xFF, right & 0xFF, bottom & 0xFF, left >> 8, top >> 8, right >> 8, bottom >> 8]
    )
    payload += bytes([(data_length >> 16) & 0xFF, (data_length >> 24) & 0xFF, 0])
    return packet(header, payload)


def chunks(data: bytes, frame: int = 0, frames: int = 1, delay_ms: int = 0, rgb24: bool = False) -> list[bytes]:
    if len(data) > PAYLOAD * 0x10000:
        raise ValueError("picture too large")
    cmd = DATA_888 if rgb24 else DATA_565
    return [
        packet([cmd, frame, frames, delay_ms, i & 0xFF, i >> 8, len(data[o : o + PAYLOAD])], data[o : o + PAYLOAD])
        for i, o in enumerate(range(0, len(data), PAYLOAD))
    ]


def changed_box(old: bytes | None, new: bytes, width: int, height: int) -> tuple[int, int, int, int] | None:
    """Bounding box (left, top, right, bottom) of the pixels that differ, or None if none do."""
    if old is None or len(old) != len(new):
        return (0, 0, width, height)
    stride = width * 3
    rows = [y for y in range(height) if old[y * stride : (y + 1) * stride] != new[y * stride : (y + 1) * stride]]
    if not rows:
        return None
    top, bottom = rows[0], rows[-1] + 1
    left, right = width, 0
    for y in rows:
        a, b = old[y * stride : (y + 1) * stride], new[y * stride : (y + 1) * stride]
        x0 = next(x for x in range(width) if a[x * 3 : x * 3 + 3] != b[x * 3 : x * 3 + 3])
        x1 = next(x for x in range(width - 1, -1, -1) if a[x * 3 : x * 3 + 3] != b[x * 3 : x * 3 + 3])
        left, right = min(left, x0), max(right, x1 + 1)
    return (left, top, right, bottom)
