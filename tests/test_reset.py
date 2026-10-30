import contextlib
import io

import pytest

from epomaker_screen import cli
from epomaker_screen.drivers.base import DeviceError
from epomaker_screen.drivers.tft import TftDriver, TftSession
from epomaker_screen.models import by_key
from fake_tft import FakeTft


@pytest.fixture
def fake(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    fake = FakeTft()
    driver = TftDriver(0x3151, 0x5002, "1-4", model=by_key("rt100pro"))

    @contextlib.contextmanager
    def session(a):
        s = TftSession(driver.model, fake, sleep=lambda t: None)
        s.chunk_delay = 0
        yield driver, s

    monkeypatch.setattr(cli, "_session", session)
    return fake


def test_erase_is_refused_without_confirmation(fake, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(""))
    with pytest.raises(DeviceError, match="needs confirmation"):
        cli.cmd_reset_screen(cli.build_parser().parse_args(["reset-screen", "--erase"]))
    assert fake.erased == 0 and fake.sent == []


def test_safe_reset_and_confirmed_erase(fake):
    assert cli.main(["reset-screen"]) == 0
    assert set(fake.banks) == {0, 1, 2, 3, 4} and fake.erased == 0
    assert cli.main(["reset-screen", "--erase", "--yes-erase-screen-flash"]) == 0
    assert fake.erased == 1
