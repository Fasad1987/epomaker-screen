"""A simulated RT82 screen controller for tests.

Models what matters for correctness: the slot table, sessions, flash that
must be erased before it is written, erase timing, playback that reads frame
records a few frames ahead of the picture on screen, and the two failure
modes seen on real hardware:

* ``stuck``: a sequence was cut off and the controller ignores everything
  until the session is closed (``11``) or the LCD is re-initialised (``1A``);
* ``frozen``: nothing answers any more until a power cycle.
"""

from __future__ import annotations

from rt82.protocol import BLOCK, MAGIC, QGIF_HEIGHT, WIDTH, Cmd

BITMAP = ((WIDTH // 4) * (QGIF_HEIGHT // 4) + 7) // 8
TILES = (WIDTH // 4) * (QGIF_HEIGHT // 4)


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def wall(self) -> float:
        return self.t + 1.7e9


class ProtocolViolation(AssertionError):
    pass


class FakeScreen:
    """Implements the Transport protocol used by rt82.device.Screen."""

    def __init__(self, clock: Clock, blocks: int = 80, readahead: int = 3, slowdown: float = 1.0):
        self.clock = clock
        self.flash = bytearray(b"\xff" * blocks * BLOCK)
        self.written = bytearray(blocks * BLOCK)  # 1 = programmed since the last erase
        self.blocks = blocks
        self.readahead = readahead
        self.slowdown = slowdown
        self.session = False
        self.table_page = bytearray(0x38)
        self.committed: tuple[list[tuple[int, int]], int] = ([], 0)
        self.replies: list[bytes] = []
        self.play: dict | None = None
        self.max_erase_s = 0.0
        self.erases = 0
        self.erase_count_per_block = [0] * blocks
        self.erased_while_playing = 0
        self.sent: list[int] = []
        self.stuck = False
        self.frozen = False
        self.drop_reply: set[int] = set()  # commands whose next reply is lost (then stuck)

    # -- transport ---------------------------------------------------------

    def write(self, pkt: bytes) -> None:
        assert len(pkt) == 64 and pkt[0] == MAGIC, pkt[:8].hex()
        self.clock.t += 0.001
        cmd, addr, length, payload = pkt[1], pkt[2] | pkt[3] << 8 | pkt[4] << 16, pkt[5], pkt[8:]
        self.sent.append(cmd)
        if self.frozen:
            return
        if self.stuck:
            if cmd in (Cmd.END, Cmd.INIT_LCD):
                self.stuck = self.session = False
                self.replies.append(bytes([MAGIC, cmd]) + bytes(62))
            return
        if cmd in self.drop_reply:
            self.drop_reply.discard(cmd)
            self.stuck = True
            return
        if cmd == Cmd.BEGIN:
            self.session = True
        elif cmd == Cmd.END:
            self.session = False
        elif cmd == Cmd.START_DOWNLOAD:
            raise ProtocolViolation("download mode (1B) freezes the controller after repeated use")
        elif cmd == Cmd.READ_TABLE:
            if addr:
                raise ProtocolViolation("14 at a nonzero address freezes the controller")
        elif cmd == Cmd.WRITE_TABLE:
            if addr == 0:
                self.table_page = bytearray(payload[:0x38])
        elif cmd == Cmd.COMMIT_TABLE:
            t = self.table_page
            n, alloc = t[1], t[2]
            slots = []
            for i in range(n):
                e = t[3 + 5 * i : 8 + 5 * i]
                slots.append((e[0], e[2] | e[3] << 8 | e[4] << 16))
            self.committed = (slots, alloc)
        elif cmd == Cmd.ERASE:
            if not self.session:
                raise ProtocolViolation("18 outside a session freezes the controller")
            self._erase(self.committed[1])
        elif cmd == Cmd.WRITE:
            self._program(addr, payload[:length])
        elif cmd == Cmd.INIT_LCD:
            self._restart_playback()
        self.replies.append(bytes([MAGIC, cmd, 0, 0, 0, 0, 0, 0, 0, 2]) + bytes(54))
        self._advance_playback()

    def read_until(self, match, timeout: float) -> bytes:
        while self.replies:
            r = self.replies.pop(0)
            if match(r):
                return r
        from rt82.hidraw import HidTimeout

        raise HidTimeout("fake: no reply")

    def drain(self) -> None:
        self.replies.clear()

    def close(self) -> None:
        pass

    # -- flash ---------------------------------------------------------------

    def _erase(self, alloc: int) -> None:
        if alloc > self.blocks:
            raise ProtocolViolation("erase past the end of flash")
        cost = 0.0
        for b in range(alloc):
            used = any(self.written[b * BLOCK : (b + 1) * BLOCK])
            cost += 0.137 if used else 0.056
            self.flash[b * BLOCK : (b + 1) * BLOCK] = b"\xff" * BLOCK
            self.written[b * BLOCK : (b + 1) * BLOCK] = bytes(BLOCK)
            self.erase_count_per_block[b] += 1
        if cost > 3.5:
            raise ProtocolViolation(f"erase took {cost:.1f}s: the controller drops off USB")
        self.max_erase_s = max(self.max_erase_s, cost)
        self.erases += 1
        # The controller does not play while erasing; pretend time passes.
        self.clock.t += cost
        if self.play and self.play["start"] < alloc:
            # The screen shows white until the next redraw (fine for an upload,
            # a defect in live mode, whose tests check this counter).
            self.erased_while_playing += 1
            self.play = None

    def _program(self, addr: int, data: bytes) -> None:
        end = addr + len(data)
        if any(self.written[addr:end]):
            raise ProtocolViolation(f"write to already-programmed flash at 0x{addr:06X}")
        self.flash[addr:end] = bytes(a & b for a, b in zip(self.flash[addr:end], data))
        self.written[addr:end] = b"\x01" * len(data)

    # -- playback ------------------------------------------------------------

    def _restart_playback(self) -> None:
        slots, _ = self.committed
        if not slots:
            self.play = None
            return
        start, length = slots[0]
        base = start * BLOCK
        hdr = self.flash[base : base + 10]
        if not all(self.written[base : base + 10]) or hdr[:4] != b"QGIF":
            raise ProtocolViolation(f"slot 1 at block {start} has no QGIF header")
        fps = (hdr[5] + 1) / 2
        count = hdr[6] << 8 | hdr[7]
        self.play = {
            "start": start,
            "base": base,
            "t0": self.clock.t,
            "frame_s": self.slowdown / fps,
            "count": count,
            "read": 0,
            "off": base + 10,
            "tiles": [bytes(8)] * TILES,
            "frames": [],
        }

    def _read_record(self) -> None:
        p = self.play
        off = p["off"]
        if not all(self.written[off : off + BITMAP]):
            raise ProtocolViolation(f"screen read an unwritten record (frame {p['read']}) at 0x{off:06X}")
        bitmap = self.flash[off : off + BITMAP]
        n = sum(bin(b).count("1") for b in bitmap)
        end = off + BITMAP + 8 * n
        if not all(self.written[off:end]):
            raise ProtocolViolation(f"screen read a half-written record (frame {p['read']})")
        tiles = list(p["tiles"])
        o = off + BITMAP
        for i in range(TILES):
            if bitmap[i >> 3] & (0x80 >> (i & 7)):
                tiles[i] = bytes(self.flash[o : o + 8])
                o += 8
        p["tiles"] = tiles
        p["frames"].append(tiles)
        p["off"] = end
        p["read"] += 1

    def _advance_playback(self) -> None:
        p = self.play
        if not p:
            return
        showing = int((self.clock.t - p["t0"]) / p["frame_s"])
        if p["count"] and showing >= p["count"]:
            return  # stills loop; not modelled
        while p["read"] <= min(showing + self.readahead, p["count"] - 1):
            self._read_record()

    def showing(self) -> list[bytes] | None:
        p = self.play
        if not p or not p["frames"]:
            return None
        showing = int((self.clock.t - p["t0"]) / p["frame_s"])
        return p["frames"][min(showing, len(p["frames"]) - 1)]

    def tick(self) -> None:
        self._advance_playback()
