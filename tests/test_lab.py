import contextlib
from types import SimpleNamespace

import pytest

from epomaker_screen import lab
from epomaker_screen.drivers.tft import TftDriver, TftSession
from epomaker_screen.models import by_key
from fake_tft import FakeTft


def run(experiment: str, **kw):
    fake = FakeTft()
    driver = TftDriver(0x3151, 0x5002, "1-4", model=by_key("rt100pro"))
    s = TftSession(driver.model, fake, sleep=lambda t: None)

    @contextlib.contextmanager
    def session(a):
        yield driver, s

    args = SimpleNamespace(
        experiment=experiment, bank=kw.get("bank", 1), delays=kw.get("delays", "2,0"), pause=0.0, every=32, frames=6
    )
    logs = []
    assert lab.main(args, session, logs.append) == 0
    return fake, logs


def test_pattern_draws_bars_and_marker():
    fake, logs = run("pattern", bank=3)
    assert set(fake.banks) == {2}
    assert fake.pixel(2, 0, 0) == (248, 252, 248)  # white top-left marker
    assert fake.pixel(2, 40, 60) == (0, 252, 0)  # second bar: green
    assert any("bars" in m for m in logs)


@pytest.mark.parametrize("experiment", ["speed", "partial", "animation", "banks", "flow", "frames"])
def test_experiments_run(experiment):
    fake, logs = run(experiment)
    assert logs
    if experiment == "banks":
        assert set(fake.banks) == {0, 1, 2, 3, 4}
    if experiment == "frames":
        assert len(fake.animation) == 6
    if experiment == "animation":
        assert len(fake.animation) == 4 and fake.delay == 250
    if experiment == "partial":
        assert fake.pixel(0, 120, 120) == (248, 0, 0) and fake.pixel(0, 0, 0) == (248, 252, 248)


def test_numbered_frames_draw_the_number():
    img = lab.numbered(240, 240, 7)
    assert len(img) == 240 * 240 * 3
    assert bytes([255, 255, 255]) in {img[i : i + 3] for i in range(0, len(img), 3)}
