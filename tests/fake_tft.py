"""A simulated TFT-family keyboard (RT100 Pro etc.) behind the vendor feature report."""

from __future__ import annotations

import struct

from epomaker_screen.drivers.tft import codec


class FakeTft:
    """Implements the TFT Transport: set_feature / get_feature / close.

    Checks every packet's framing and checksum, keeps picture banks and the
    last animation, and composites partial updates into a bank (what the
    real firmware does with them is not known yet).
    """

    def __init__(self, internal_id: int = 3152, width: int = 240, height: int = 240, firmware: int = 0x0105):
        self.internal_id, self.w, self.h, self.firmware = internal_id, width, height, firmware
        self.sent: list[bytes] = []
        self.reply = bytes(64)
        self.banks: dict[int, bytearray] = {}
        self.animation: list[bytearray] | None = None
        self.delay = None
        self.pending: dict | None = None
        self.clock = None
        self.system_info = None
        self.refuse_prepare = 0  # answer this many prepares with "not ready"
        self.closed = False
        self.erased = 0

    # -- transport -----------------------------------------------------------

    def set_feature(self, data: bytes, report_id: int = 0) -> None:
        assert report_id == 0 and len(data) == 64, (report_id, len(data))
        assert data[7] == 255 - (sum(data[:7]) & 255), f"bad checksum in {data[:8].hex()}"
        assert data[0] in codec.ALLOWED or data[0] == codec.ERASE_SCREEN, f"command 0x{data[0]:02X} must never be sent"
        self.sent.append(bytes(data))
        cmd = data[0]
        if cmd == codec.IDENTIFY:
            self.reply = (
                bytes([0x8F])
                + struct.pack("<I", self.internal_id)
                + bytes(2)
                + struct.pack("<H", self.firmware)
                + bytes(55)
            )
        elif cmd in (codec.PREPARE_565, codec.PREPARE_888):
            self._prepare(data)
        elif cmd in (codec.DATA_565, codec.DATA_888):
            self._data(data)
        elif cmd == codec.ERASE_SCREEN:
            self.banks.clear()
            self.animation = None
            self.erased += 1
            self.reply = codec.ERASED + bytes(59)
        elif cmd == codec.CLOCK:
            self.clock = struct.unpack(">H5B", data[8:15])
        elif cmd == codec.SYSTEM_INFO:
            self.system_info = struct.unpack("<4HBB2H", data[8:22])

    def get_feature(self, report_id: int = 0, size: int | None = None) -> bytes:
        return self.reply

    def close(self) -> None:
        self.closed = True

    # -- pictures --------------------------------------------------------------

    def _prepare(self, p: bytes) -> None:
        frame, frames, delay = p[1], p[2], p[3]
        length = p[4] | p[5] << 8 | p[16] << 16 | p[17] << 24
        box = tuple(p[8 + i] | p[12 + i] << 8 for i in range(4))
        left, top, right, bottom = box
        assert 0 <= left < right <= self.w and 0 <= top < bottom <= self.h, box
        assert length == (right - left) * (bottom - top) * 2, (length, box)
        if self.refuse_prepare:
            self.refuse_prepare -= 1
            self.reply = bytes([p[0], 0]) + bytes(62)
            return
        self.pending = {
            "frame": frame,
            "frames": frames,
            "delay": delay,
            "length": length,
            "box": box,
            "data": {},
            "next": {},
        }
        self.reply = bytes([p[0], 1]) + bytes(62)

    def _data(self, p: bytes) -> None:
        st = self.pending
        assert st is not None, "pixel data without a prepare"
        frame, frames, delay, index, n = p[1], p[2], p[3], p[4] | p[5] << 8, p[6]
        assert (frames, delay) == (st["frames"], st["delay"])
        if frames == 1:
            assert frame == st["frame"]
        assert index == st["next"].get(frame, 0), f"chunk {index} out of order"
        st["next"][frame] = index + 1
        buf = st["data"].setdefault(frame, bytearray())
        buf += p[8 : 8 + n]
        assert len(buf) <= st["length"]
        if len(buf) == st["length"]:
            self._store(frame, bytes(buf))

    def _store(self, frame: int, data: bytes) -> None:
        st = self.pending
        left, top, right, bottom = st["box"]
        if st["frames"] == 1:
            img = self.banks.setdefault(frame, bytearray(self.w * self.h * 2))
        else:
            if self.animation is None or len(self.animation) != st["frames"]:
                self.animation = [bytearray(self.w * self.h * 2) for _ in range(st["frames"])]
            self.delay = st["delay"]
            img = self.animation[frame]
        i = 0
        for x in range(left, right):  # column-major
            for y in range(top, bottom):
                o = (y * self.w + x) * 2
                img[o : o + 2] = data[i : i + 2]
                i += 2

    def pixel(self, bank: int, x: int, y: int) -> tuple[int, int, int]:
        """Colour of a stored pixel, back in 8-bit RGB (low bits lost to RGB565)."""
        o = (y * self.w + x) * 2
        v = self.banks[bank][o] << 8 | self.banks[bank][o + 1]
        return ((v >> 11) << 3, ((v >> 5) & 63) << 2, (v & 31) << 3)
