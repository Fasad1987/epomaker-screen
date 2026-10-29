"""QGIF, the screen's flash image format.

Header (10 bytes): ``QGIF``, 0, 2*fps-1, frame count (u16 BE), width/4-1,
height/4-1. Then one record per frame: a change bitmap (one bit per 4x4
tile, MSB first) followed by 8 bytes per changed tile. A looping file ends
with one extra record that restores frame 0.

Tile: colour A and colour B (RGB565 LE), then 4 rows of four 2-bit indices,
leftmost pixel in the high bits. Palette: A, B, (2A+B)/3, (A+2B)/3, A >= B.

Frames are passed as packed RGB888 bytes (``width*height*3``).
"""

from __future__ import annotations

from collections.abc import Iterable

MAGIC = b"QGIF"


def rgb565(r: int, g: int, b: int) -> int:
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def expand565(c: int) -> tuple[int, int, int]:
    r, g, b = (c >> 11) & 31, (c >> 5) & 63, c & 31
    return (r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)


def palette(a: int, b: int) -> list[tuple[int, int, int]]:
    A, B = expand565(a), expand565(b)
    mix = lambda x, y: tuple(round((2 * p + q) / 3) for p, q in zip(x, y))  # noqa: E731
    return [A, B, mix(A, B), mix(B, A)]


def _d2(p, q) -> int:
    return (p[0] - q[0]) ** 2 + (p[1] - q[1]) ** 2 + (p[2] - q[2]) ** 2


def encode_tile(tile: bytes) -> bytes:
    """Encodes 16 RGB pixels (48 bytes, row-major) into 8 bytes."""
    px = [tuple(tile[i : i + 3]) for i in range(0, 48, 3)]
    colors = list(dict.fromkeys(px))
    if len(colors) == 1:
        c = rgb565(*colors[0])
        return bytes((c & 0xFF, c >> 8, c & 0xFF, c >> 8, 0, 0, 0, 0))
    if len(colors) == 2:
        pa, pb = colors
    else:
        best, pa, pb = -1, colors[0], colors[1]
        for i, p in enumerate(colors):
            for q in colors[i + 1 :]:
                d = _d2(p, q)
                if d > best:
                    best, pa, pb = d, p, q
    a, b = rgb565(*pa), rgb565(*pb)
    if a < b:
        a, b = b, a
    out = bytearray((a & 0xFF, a >> 8, b & 0xFF, b >> 8, 0, 0, 0, 0))
    if a != b:
        pal = palette(a, b)
        cache: dict = {}
        for row in range(4):
            byte = 0
            for col in range(4):
                p = px[row * 4 + col]
                idx = cache.get(p)
                if idx is None:
                    idx = min(range(4), key=lambda k: _d2(p, pal[k]))
                    cache[p] = idx
                byte |= idx << (6 - col * 2)
            out[4 + row] = byte
    return bytes(out)


class TileEncoder:
    """Splits frames into tiles and encodes them, caching repeated tiles."""

    def __init__(self, width: int, height: int):
        if width % 4 or height % 4:
            raise ValueError("QGIF dimensions must be multiples of 4")
        self.width, self.height = width, height
        self.tiles_x, self.tiles_y = width // 4, height // 4
        self.count = self.tiles_x * self.tiles_y
        self.bitmap_len = (self.count + 7) // 8
        self._cache: dict[bytes, bytes] = {}

    def split(self, rgb: bytes) -> list[bytes]:
        if len(rgb) != self.width * self.height * 3:
            raise ValueError(f"frame must be {self.width}x{self.height} RGB888")
        stride, out = self.width * 3, []
        for ty in range(self.tiles_y):
            rows = [rgb[(ty * 4 + y) * stride : (ty * 4 + y + 1) * stride] for y in range(4)]
            for tx in range(self.tiles_x):
                s = tx * 12
                out.append(rows[0][s : s + 12] + rows[1][s : s + 12] + rows[2][s : s + 12] + rows[3][s : s + 12])
        return out

    def encode(self, tile: bytes) -> bytes:
        enc = self._cache.get(tile)
        if enc is None:
            if len(self._cache) > 50000:
                self._cache.clear()
            enc = self._cache[tile] = encode_tile(tile)
        return enc

    def record(self, cur: list[bytes], prev: list[bytes] | None) -> bytes:
        """One frame record: the tiles that differ from ``prev`` (all if None)."""
        bitmap = bytearray(self.bitmap_len)
        body = bytearray()
        for i, tile in enumerate(cur):
            if prev is None or prev[i] != tile:
                bitmap[i >> 3] |= 0x80 >> (i & 7)
                body += self.encode(tile)
        return bytes(bitmap) + bytes(body)


def header(width: int, height: int, frames: int, fps: int) -> bytes:
    if not 1 <= fps <= 128:
        raise ValueError("fps must be 1..128")
    if not 1 <= frames <= 0xFFFF:
        raise ValueError("frame count must be 1..65535")
    return MAGIC + bytes((0, 2 * fps - 1, frames >> 8, frames & 0xFF, width // 4 - 1, height // 4 - 1))


def encode(frames: Iterable[bytes], width: int, height: int, fps: int = 10) -> bytes:
    """Encodes a looping animation (or a still with one frame)."""
    enc = TileEncoder(width, height)
    records, first, prev = [], None, None
    for rgb in frames:
        cur = enc.split(rgb)
        records.append(enc.record(cur, prev))
        if first is None:
            first = cur
        prev = cur
    if first is None:
        raise ValueError("no frames")
    records.append(enc.record(first, prev))  # restore frame 0 for looping
    return header(width, height, len(records) - 1, fps) + b"".join(records)


class Stream:
    """Incremental QGIF for live mode: header, keyframe, then delta records."""

    def __init__(self, width: int, height: int):
        self.enc = TileEncoder(width, height)
        self.prev: list[bytes] | None = None

    def header(self, frames: int, fps: int) -> bytes:
        return header(self.enc.width, self.enc.height, frames, fps)

    def next(self, rgb: bytes) -> bytes:
        cur = self.enc.split(rgb)
        rec = self.enc.record(cur, self.prev)
        self.prev = cur
        return rec

    def empty(self) -> bytes:
        """A record that repeats the previous frame."""
        return bytes(self.enc.bitmap_len)


def decode(data: bytes) -> tuple[int, int, int, list[bytes]]:
    """Decodes a QGIF into (width, height, fps, RGB888 frames). For tests and previews."""
    if data[:4] != MAGIC:
        raise ValueError("not a QGIF")
    fps = (data[5] + 1) // 2
    count = (data[6] << 8) | data[7]
    w, h = (data[8] + 1) * 4, (data[9] + 1) * 4
    tw = w // 4
    n = tw * (h // 4)
    blen = (n + 7) // 8
    tiles = [bytes(8)] * n
    frames, off = [], 10
    for _ in range(count):
        bitmap = data[off : off + blen]
        off += blen
        for i in range(n):
            if bitmap[i >> 3] & (0x80 >> (i & 7)):
                tiles[i] = data[off : off + 8]
                off += 8
        img = bytearray(w * h * 3)
        for i, t in enumerate(tiles):
            pal = palette(t[0] | t[1] << 8, t[2] | t[3] << 8)
            bx, by = (i % tw) * 4, (i // tw) * 4
            for y in range(4):
                for x in range(4):
                    c = pal[(t[4 + y] >> (6 - 2 * x)) & 3]
                    o = ((by + y) * w + bx + x) * 3
                    img[o : o + 3] = bytes(c)
        frames.append(bytes(img))
    return w, h, fps, frames
