import datetime as dt
import threading
import time

import pytest

from epomaker_screen import devices, hid
from epomaker_screen.drivers.base import Animation, DeviceError, SystemInfo, Unsupported
from epomaker_screen.drivers.tft import PREPARE_ATTEMPTS, RefreshConfig, TftDriver, TftSession, codec
from epomaker_screen.models import by_key, by_usb_id
from fake_tft import FakeTft

W = H = 240


def frame(paint, w=W, h=H) -> bytes:
    return b"".join(bytes(paint(x, y)) for y in range(h) for x in range(w))


def session(fake: FakeTft, key: str = "rt100pro") -> TftSession:
    s = TftSession(by_key(key), fake, sleep=lambda s: None)
    s.chunk_delay = 0
    return s


# -- packets -----------------------------------------------------------------


def test_packets_match_vectors_recovered_from_the_vendor_software():
    # From ramarivera/epomaker-driver-linux tests/fixtures/glyph-packets.json (MIT).
    assert list(codec.identify()) == [143, 0, 0, 0, 0, 0, 0, 112] + [0] * 56
    glyph = codec.prepare(428 * 142 * 2, (0, 0, 428, 142), 0, 1, 10)
    assert list(glyph[:19]) == [165, 0, 1, 10, 208, 218, 0, 165, 0, 0, 172, 142, 0, 0, 1, 0, 1, 0, 0]
    chunks = codec.chunks(bytes(range(58)), 0, 1, 10)
    assert list(chunks[0][:8]) == [37, 0, 1, 10, 0, 0, 56, 151] and chunks[0][8:] == bytes(range(56))
    assert list(chunks[1][:10]) == [37, 0, 1, 10, 1, 0, 2, 204, 56, 57]


def test_rgb565_is_big_endian_column_major():
    # 2x2: red green / blue white -> F800 001F 07E0 FFFF (x outer, y inner)
    rgb = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255])
    assert codec.rgb565_column_major(rgb, 2, 2).hex() == "f800001f07e0ffff"


def test_clock_and_system_info_layout():
    p = codec.clock(dt.datetime(2026, 10, 30, 9, 5, 7))
    assert p[0] == 0x28 and p[8:15] == bytes([0x07, 0xEA, 10, 30, 9, 5, 7])  # big-endian year
    gib = 1024**3
    p = codec.system_info(100 * gib, 512 * gib, int(7.6 * gib), 32 * gib, 37, 61, 3 * gib, 20 * gib)
    assert p[0] == 0x22 and p[8:22] == bytes([100, 0, 0, 2, 8, 0, 32, 0, 37, 61, 3, 0, 20, 0])


def test_dangerous_commands_cannot_be_built():
    for cmd in (0x01, 0xAC, 0xBA, 0x30, 0x0A):  # factory reset, screen erase, firmware, key map
        with pytest.raises(ValueError):
            codec.packet([cmd])


def test_the_erase_command_needs_an_explicit_opt_in():
    fake = FakeTft()
    s = session(fake)
    with pytest.raises(ValueError):
        s.send(codec.erase_screen())
    assert fake.sent == []
    s.erase_screen_flash()
    assert fake.erased == 1 and fake.sent[0][:8].hex() == "ac00000000000053"
    assert fake.clock is not None


def test_safe_reset_blacks_out_every_bank_and_sets_the_clock():
    fake = FakeTft()
    s = session(fake)
    s.upload_still(frame(lambda x, y: (255, 255, 255)), 2)
    s.reset_screen()
    assert set(fake.banks) == {0, 1, 2, 3, 4}
    assert all(fake.pixel(b, 120, 120) == (0, 0, 0) for b in range(5))
    assert len(fake.animation) == 2 and not any(fake.animation[0])
    assert fake.clock is not None and fake.erased == 0


def test_changed_box():
    a = frame(lambda x, y: (0, 0, 0), 10, 8)
    b = bytearray(a)
    for x, y in ((2, 3), (6, 5)):
        b[(y * 10 + x) * 3] = 255
    assert codec.changed_box(a, bytes(b), 10, 8) == (2, 3, 7, 6)
    assert codec.changed_box(a, a, 10, 8) is None
    assert codec.changed_box(None, a, 10, 8) == (0, 0, 10, 8)


# -- model catalogue -----------------------------------------------------------------


def test_frame_limits_follow_the_vendor_memory_formula():
    # Values documented by ramarivera/epomaker-driver-linux docs/display.md.
    assert by_key("rt100pro").extra["max_frames"] == 47
    assert by_key("rt75").extra["max_frames"] == 47
    assert by_key("rt85").extra["max_frames"] == 51
    assert by_key("glyph").extra["max_frames"] == 46
    assert {m.key for m in by_usb_id(0x3151, 0x5002)} == {"rt100pro", "rt75", "rt85", "glyph"}


# -- session ------------------------------------------------------------------------


def test_still_lands_in_its_bank_with_the_right_colours():
    fake = FakeTft()
    s = session(fake)
    img = frame(lambda x, y: (255, 0, 0) if x < 120 else (0, 0, 255) if y < 120 else (0, 255, 0))
    sent = s.upload_still(img, bank=2)
    assert sent == W * H * 2 and set(fake.banks) == {2}
    assert fake.pixel(2, 10, 10) == (248, 0, 0)
    assert fake.pixel(2, 200, 10) == (0, 0, 248)
    assert fake.pixel(2, 200, 200) == (0, 252, 0)
    assert len([p for p in fake.sent if p[0] == codec.DATA_565]) == -(-W * H * 2 // 56)  # 2058 packets


def test_prepare_is_retried_until_the_keyboard_is_ready():
    fake = FakeTft()
    fake.refuse_prepare = 3
    session(fake).upload_still(frame(lambda x, y: (1, 2, 3)))
    assert sum(p[0] == codec.PREPARE_565 for p in fake.sent) == 4


def test_a_busy_keyboard_gets_about_ten_seconds_then_a_clear_error():
    fake = FakeTft()
    fake.refuse_prepare = PREPARE_ATTEMPTS - 1
    s = session(fake)
    s.upload_still(frame(lambda x, y: (1, 2, 3)))
    assert s.last_prepare["attempts"] == PREPARE_ATTEMPTS
    fake.refuse_prepare = PREPARE_ATTEMPTS
    with pytest.raises(DeviceError, match="did not accept.*last reply a5 00"):
        s.upload_still(frame(lambda x, y: (1, 2, 3)))


def test_upload_takes_stills_or_one_animation():
    fake = FakeTft()
    s = session(fake)
    red, blue = frame(lambda x, y: (255, 0, 0)), frame(lambda x, y: (0, 0, 255))
    s.upload([Animation([red]), Animation([blue])])
    assert fake.pixel(0, 0, 0)[0] == 248 and fake.pixel(1, 0, 0)[2] == 248
    s.upload([Animation([red, blue, red], fps=8)])
    assert len(fake.animation) == 3 and fake.delay == 125
    with pytest.raises(ValueError):
        s.upload([Animation([red, blue]), Animation([red])])
    with pytest.raises(ValueError):
        s.upload([Animation([red])] * 6)


def test_partial_update_sends_only_the_box():
    fake = FakeTft()
    s = session(fake)
    s.upload_still(frame(lambda x, y: (0, 0, 0)))
    sent = s.upload_still(frame(lambda x, y: (255, 255, 255)), 0, (100, 100, 140, 120))
    assert sent == 40 * 20 * 2
    assert fake.pixel(0, 110, 110) == (248, 252, 248) and fake.pixel(0, 10, 10) == (0, 0, 0)


def test_refresh_live_mode_only_sends_changes():
    fake = FakeTft()
    s = session(fake)
    content = {"v": 0}
    render = lambda t: frame(lambda x, y: (content["v"], 0, 0) if x < 20 and y < 10 else (0, 0, 0))  # noqa: E731
    live = s.live(render, b"", RefreshConfig(interval_s=10, partial=True))
    live.start()
    assert live.stats["updates"] == 1
    live._update()  # nothing changed
    assert live.stats == {"updates": 1, "bytes": W * H * 2, "skipped": 1}
    content["v"] = 255
    live._update()
    assert live.stats["updates"] == 2 and live.stats["bytes"] == W * H * 2 + 20 * 10 * 2
    stop = threading.Event()
    stop.set()
    live.run_for(stop, 1)  # returns at once


def test_refresh_redraws_on_time_when_called_in_short_slices():
    # The service calls run_for in 2 s slices to feed the system page in between;
    # redraws must still come every interval, not only when a slice happens to cover one.
    fake = FakeTft()
    s = session(fake)
    content = {"v": 0}

    def render(t):
        content["v"] += 1
        return frame(lambda x, y: (content["v"] % 256, 0, 0) if x < 4 and y < 4 else (0, 0, 0), 8, 8)

    s.caps = s.caps.__class__(**{**s.caps.__dict__, "width": 8, "height": 8})
    live = s.live(render, b"", RefreshConfig(interval_s=0.2))
    live.start()
    stop = threading.Event()
    t0 = time.monotonic()
    while time.monotonic() - t0 < 1.05:
        live.run_for(stop, 0.03)
    assert 5 <= live.stats["updates"] <= 7


def test_system_info_waits_while_a_picture_is_stored():
    fake = FakeTft()
    s = session(fake)
    s.upload_still(frame(lambda x, y: (0, 0, 0)))
    s.system_info(SystemInfo(cpu_percent=55))
    assert fake.system_info is None
    s.busy_until = 0.0
    s.system_info(SystemInfo(cpu_percent=55))
    assert fake.system_info[4] == 55


def test_clock_and_system_info_reach_the_keyboard():
    fake = FakeTft()
    s = session(fake)
    s.sync_time(dt.datetime(2026, 1, 2, 3, 4, 5))
    assert fake.clock == (2026, 1, 2, 3, 4, 5)
    s.system_info(SystemInfo(disk_free=10 * 1024**3, cpu_percent=55, cpu_temperature=None))
    assert fake.system_info[0] == 10 and fake.system_info[4:6] == (55, 0)


# -- driver and detection ----------------------------------------------------------------


def test_driver_identifies_the_model_sharing_a_usb_id():
    fake = FakeTft(internal_id=3152)
    d = TftDriver(0x3151, 0x5002, "")
    assert d.model is None and d.caps is None and "not identified" in d.name
    s = d.open(transport_factory=lambda: fake)
    assert d.model.key == "rt100pro" and d.caps.width == 240 and d.caps.system_info
    assert s.model.key == "rt100pro"


def test_driver_refuses_an_unknown_internal_id():
    d = TftDriver(0x3151, 0x5002, "")
    fake = FakeTft(internal_id=9999)
    with pytest.raises(Unsupported, match="9999"):
        d.open(transport_factory=lambda: fake)
    assert fake.closed


# A YC3123-style vendor collection: usage page 0xFFFF, usage 2, one 64-byte feature report.
VENDOR = bytes.fromhex("06ffff0902a101150026ff0075089540b102c0")
KEYBOARD = bytes.fromhex("05010906a101050719e029e71500250175019508810295067508150026ff00050719002aff008100c0")


def info(node, vid, pid, usb, desc, interface=0) -> hid.HidInfo:
    return hid.HidInfo(node, vid, pid, interface, hid.parse_descriptor(desc), "test", usb)


def test_vendor_descriptor_is_recognised():
    d = hid.parse_descriptor(VENDOR)
    assert d.has(0xFFFF, 2) and d.feature == {0: 64} and not d.input


def test_detect_groups_by_usb_device_and_picks_drivers():
    nodes = [
        info("/dev/hidraw2", 0x36B0, 0x30A3, "3-9.3", KEYBOARD),
        info("/dev/hidraw3", 0x36B0, 0x30A3, "3-9.3", bytes.fromhex("0660ff0961a101c0"), 1),
        info("/dev/hidraw5", 0x3151, 0x5002, "1-4", KEYBOARD),
        info("/dev/hidraw7", 0x3151, 0x5002, "1-4", VENDOR, 2),
        info("/dev/hidraw8", 0x3151, 0x4011, "1-5", VENDOR, 2),  # some other Epomaker device
        info("/dev/hidraw9", 0x046D, 0xC52B, "1-6", KEYBOARD),  # not Epomaker
    ]
    found = devices.detect(nodes)
    assert [f.usb_path for f in found] == ["1-4", "1-5", "3-9.3"]
    assert found[0].driver.family == "tft" and found[1].driver is None
    assert found[2].driver.family == "rdm" and found[2].driver.model.key == "rt82"
    assert [d.family for d in devices.screens(nodes)] == ["tft", "rdm"]
