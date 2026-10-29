import pytest

from fake_screen import Clock, FakeScreen
from rt82 import device
from rt82.device import Screen
from rt82.live import LiveConfig, LiveStreamer, erase_steps
from rt82.protocol import QGIF_HEIGHT, WIDTH, Kb

BITS = 32


def stamp(t: float) -> bytes:
    """A frame whose first 32 tiles encode int(t) in binary (white = 1)."""
    v = int(t)
    img = bytearray(WIDTH * QGIF_HEIGHT * 3)
    for bit in range(BITS):
        if v >> bit & 1:
            tx, ty = bit % (WIDTH // 4), bit // (WIDTH // 4)
            for y in range(4):
                o = ((ty * 4 + y) * WIDTH + tx * 4) * 3
                img[o : o + 12] = b"\xff" * 12
    return bytes(img)


def read_stamp(tiles) -> int:
    return sum(1 << bit for bit in range(BITS) if tiles[bit][0] == 0xFF)


class FakeKeyboard:
    def __init__(self):
        self.sent = []

    def send(self, cmd, payload=b"", timeout=1.0):
        self.sent.append((cmd, bytes(payload)))

    def set_screen_count(self, n):
        self.send(Kb.SCREEN_COUNT, [n])

    def close(self):
        pass


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(device.time, "sleep", lambda s: None)


def make(cfg: LiveConfig, slowdown: float = 1.0):
    clock = Clock()
    fake = FakeScreen(clock, slowdown=slowdown)
    screen = Screen(fake)
    streamer = LiveStreamer(screen, stamp, stamp(0), cfg, monotonic=clock, wall=clock.wall)
    return clock, fake, streamer


def run(clock, fake, streamer, seconds: float, step: float = 0.2, check=True):
    lags = []
    end = clock.t + seconds
    while clock.t < end:
        clock.t += step
        fake.tick()
        streamer.tick()
        tiles = fake.showing()
        if check and tiles is not None and streamer.seg is not None:
            lags.append(clock.wall() - read_stamp(tiles))
    return lags


def test_erase_steps_stay_short():
    for used in range(1, 29):
        steps = erase_steps(used)
        assert steps[-1] == used
        done = 0
        for n in steps:
            assert device.ERASE_S_BLANK * done + device.ERASE_S_DATA * (n - done) <= device.MAX_ERASE_S + 1e-9
            done = n


@pytest.mark.parametrize("fps", [1, 2])
def test_streams_for_hours_without_reading_unwritten_flash(fps):
    cfg = LiveConfig(fps=fps, area_blocks=24, segment_seconds=300)
    clock, fake, streamer = make(cfg)
    kb = FakeKeyboard()
    streamer.start(kb)
    assert kb.sent == [(Kb.SCREEN_COUNT, b"\x01")]
    lags = run(clock, fake, streamer, 3 * 3600)
    # A frame shows the time it was rendered for, within a frame and the prep estimate.
    on_screen = [lag for lag in lags if abs(lag) < 3600]
    assert max(abs(lag) for lag in on_screen) < 2.0
    assert streamer.stats["segments"] > 30
    assert streamer.stats["erases"] >= 1
    assert streamer.stats["underruns"] == 0
    assert fake.erased_while_playing == 0  # the picture on screen is never erased
    assert fake.max_erase_s <= 3.5
    # Wear: the stream area is erased about once an hour or less at 1 fps.
    per_hour = max(fake.erase_count_per_block[:24]) / 3
    assert per_hour <= (2 if fps == 1 else 4)


def test_slower_playback_is_reset_by_new_segments():
    cfg = LiveConfig(fps=1, segment_seconds=300)
    clock, fake, streamer = make(cfg, slowdown=1.03)  # screen plays 3% slower than declared
    streamer.start(FakeKeyboard())
    lags = run(clock, fake, streamer, 3600)
    # 3% of a 300 s segment is 9 s at worst, and it never accumulates past that.
    assert max(lags) < 11


def test_recovers_after_suspend():
    clock, fake, streamer = make(LiveConfig())
    streamer.start(FakeKeyboard())
    run(clock, fake, streamer, 30)
    clock.t += 120  # system slept; the screen kept playing into erased flash
    fake.play = None  # what it shows is undefined; the next flip restores it
    streamer.tick()
    assert streamer.stats["underruns"] == 1
    lags = run(clock, fake, streamer, 60)
    assert max(abs(lag) for lag in lags[5:]) < 2.0


def test_stop_leaves_a_still():
    clock, fake, streamer = make(LiveConfig())
    streamer.start(FakeKeyboard())
    run(clock, fake, streamer, 20)
    streamer.stop()
    assert fake.play and fake.play["count"] == 1
    assert read_stamp(fake.showing()) == int(clock.wall())
