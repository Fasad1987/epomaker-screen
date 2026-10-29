import datetime as dt

import pytest

from fake_screen import Clock, FakeScreen
from rt82 import device, qgif
from rt82.device import Screen
from rt82.hidraw import parse_descriptor
from rt82.protocol import BLOCK, Cmd, Slot, packet, table_pages, time_digits
from test_live import FakeKeyboard, read_stamp, stamp


def test_packet_layout():
    p = packet(Cmd.WRITE, b"\x01\x02", 2, 0x123456)
    assert p[:8] == bytes([0xAA, 0x19, 0x56, 0x34, 0x12, 2, 0, 0])
    assert p[8:10] == b"\x01\x02" and len(p) == 64


def test_time_digits_match_web_tool_capture():
    # The web tool sent 02 00 02 06 | 00 02 | 03 | 00 04 for Wednesday, 4 Feb 2026.
    d = time_digits(dt.datetime(2026, 2, 4, 9, 17, 5))
    assert d == [2, 0, 2, 6, 0, 2, 3, 0, 4, 0, 9, 1, 7, 0, 5]
    assert time_digits(dt.datetime(2026, 11, 1, 0, 0, 0))[6] == 0  # Sunday


def test_table_pages():
    pages = table_pages([Slot(0, 0x012345), Slot(2, 10)], 4)
    assert pages[0][:8] == bytes([0xAA, 0x15, 0, 0, 0, 0x38, 0, 0])
    assert pages[0][8:21] == bytes([0, 2, 4, 0, 0, 0x45, 0x23, 0x01, 2, 0, 10, 0, 0])
    assert pages[1][2] == 0x38 and pages[2][2] == 0x70 and pages[2][5] == 0x10
    assert pages[3][1] == 0x16
    with pytest.raises(ValueError):
        table_pages([], 0)


def test_descriptor_sizes():
    # QMK/VIA raw HID: usage page 0xFF60, 32-byte input and output reports.
    via = bytes.fromhex("0660ff0961a1010962150026ff007508952081020963150026ff00750895209102c0")
    assert parse_descriptor(via) == (0xFF60, 32, 32)
    lcd = bytes.fromhex("06ff000901a1010901150026ff007508954081020902150026ff00750895409102c0")
    assert parse_descriptor(lcd) == (0x00FF, 64, 64)


def test_upload_writes_images_and_shows_slot_one(monkeypatch):
    monkeypatch.setattr(device.time, "sleep", lambda s: None)
    clock = Clock()
    fake = FakeScreen(clock)
    s = Screen(fake)
    a = qgif.encode([stamp(5)], 240, 136, fps=1)
    b = qgif.encode([stamp(7), stamp(8)], 240, 136, fps=2)
    kb = FakeKeyboard()
    slots = s.upload([a, b], kb)
    assert slots == [Slot(0, len(a)), Slot(1, len(b))]
    assert kb.sent == [(0xE3, b"\x02")]
    assert fake.flash[: len(a)] == a and fake.flash[BLOCK : BLOCK + len(b)] == b
    assert fake.committed == ([(0, len(a)), (1, len(b))], 2)
    assert read_stamp(fake.showing()) == 5


def test_sync_time_sequence():
    fake = FakeScreen(Clock())
    sent = []
    orig = fake.write
    fake.write = lambda p: (sent.append(p[1]), orig(p))
    Screen(fake).sync_time(dt.datetime(2026, 1, 1))
    assert sent == [Cmd.BEGIN, Cmd.SET_TIME, Cmd.END]


def test_qgif_roundtrip_and_header():
    frames = [stamp(1), stamp(2), stamp(1)]
    data = qgif.encode(frames, 240, 136, fps=8)
    assert data[:10] == b"QGIF" + bytes([0, 15, 0, 3, 59, 33])  # matches captured web-tool headers
    w, h, fps, out = qgif.decode(data)
    assert (w, h, fps) == (240, 136, 8)
    assert [read_stamp(qgif.TileEncoder(240, 136).split(f)) for f in out] == [1, 2, 1]


def test_tile_colours_are_close():
    import random

    rnd = random.Random(1)
    for _ in range(200):
        c1 = [rnd.randrange(256) for _ in range(3)]
        c2 = [rnd.randrange(256) for _ in range(3)]
        tile = bytes(sum(([*c1] if rnd.random() < 0.5 else [*c2] for _ in range(16)), []))
        enc = qgif.encode_tile(tile)
        a, b = enc[0] | enc[1] << 8, enc[2] | enc[3] << 8
        assert a >= b
        pal = qgif.palette(a, b)
        for i in range(16):
            got = pal[(enc[4 + i // 4] >> (6 - 2 * (i % 4))) & 3]
            want = tile[i * 3 : i * 3 + 3]
            assert all(abs(g - w) <= 8 for g, w in zip(got, want))


def test_upload_never_uses_download_mode_and_replaces_what_is_playing(monkeypatch):
    monkeypatch.setattr(device.time, "sleep", lambda s: None)
    fake = FakeScreen(Clock())
    s = Screen(fake)
    big = qgif.encode([stamp(i) for i in range(30)], 240, 136, fps=20)
    s.upload([big], FakeKeyboard())
    s.upload([qgif.encode([stamp(9)], 240, 136, fps=1)], FakeKeyboard())
    assert Cmd.START_DOWNLOAD not in fake.sent
    assert read_stamp(fake.showing()) == 9
    assert fake.max_erase_s <= 3.5


def test_recovers_from_an_interrupted_sequence(monkeypatch):
    monkeypatch.setattr(device.time, "sleep", lambda s: None)
    fake = FakeScreen(Clock())
    s = Screen(fake)
    fake.drop_reply = {Cmd.COMMIT_TABLE}  # what the RT82 did: no reply to 16, then silence
    with pytest.raises(device.DeviceError):
        s.upload([qgif.encode([stamp(1)], 240, 136, fps=1)], FakeKeyboard())
    assert not fake.stuck  # upload() closed the session on its way out
    fake.stuck = True  # left like that by a crashed program
    s.probe()  # the next connection notices and recovers
    assert not fake.stuck
    s.upload([qgif.encode([stamp(2)], 240, 136, fps=1)], FakeKeyboard())
    assert read_stamp(fake.showing()) == 2


def test_probe_reports_a_frozen_controller():
    fake = FakeScreen(Clock())
    fake.frozen = True
    with pytest.raises(device.ScreenFrozen, match="Power-cycle"):
        Screen(fake).probe()
