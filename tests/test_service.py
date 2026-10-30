import threading
import time

from epomaker_screen import service
from epomaker_screen.drivers import rdm
from epomaker_screen.drivers.rdm import device
from epomaker_screen.drivers.rdm.device import Screen
from epomaker_screen.drivers.rdm.live import LiveConfig
from epomaker_screen.drivers.rdm.protocol import Cmd
from epomaker_screen.models import by_key
from epomaker_screen.service import CONNECTED, FROZEN, LIVE
from fake_screen import Clock, FakeScreen
from test_live import FakeKeyboard, stamp


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class FakeRdmDriver(rdm.RdmDriver):
    """An RT82 driver whose sessions talk to the simulated controller."""

    def __init__(self, fake: FakeScreen, conn: dict):
        super().__init__(by_key("rt82"), "3-9.3")
        self.fake, self.conn = fake, conn

    def connection_id(self):
        return self.conn["id"]

    def open(self, log=lambda m: None):
        screen = Screen(self.fake)
        screen.probe()
        return rdm.RdmSession(self.model, self.usb_path, log, screen=screen)


def _setup(monkeypatch, tmp_path, fake):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(device.time, "sleep", lambda s: None)
    monkeypatch.setattr(rdm.Keyboard, "open", classmethod(lambda cls, pid=0, usb_path="": FakeKeyboard()))
    conn = {"id": "3:10"}
    driver = FakeRdmDriver(fake, conn)
    return driver, conn


def test_controller_syncs_time_streams_and_runs_jobs(monkeypatch, tmp_path):
    fake = FakeScreen(Clock())
    driver, _conn = _setup(monkeypatch, tmp_path, fake)
    states, logs, seen = [], [], []
    c = service.Controller(log=logs.append, on_state=states.append, on_device=seen.append, find=lambda: [driver])
    c.set_live(True, lambda geometry: (stamp, stamp(0)), {"rdm": LiveConfig(fps=2)})
    c.start()
    try:
        assert wait_for(lambda: LIVE in states), (states, logs)
        assert seen[0] is driver
        assert Cmd.SET_TIME in fake.sent  # clock set on connect
        before = fake.sent.count(Cmd.WRITE)
        assert wait_for(lambda: fake.sent.count(Cmd.WRITE) > before + 5)  # frames keep flowing

        # Clock sync while live: done between frames, no restart.
        done = threading.Event()
        result = {}
        syncs = fake.sent.count(Cmd.SET_TIME)
        c.sync_now(lambda e, r: (result.update(err=e), done.set()))
        assert done.wait(10) and result["err"] is None
        assert fake.sent.count(Cmd.SET_TIME) == syncs + 1
        assert Cmd.ERASE not in fake.sent[-20:]  # live mode was not restarted

        # Any other job interrupts live mode, which comes back afterwards.
        done.clear()
        n = states.count(LIVE)
        c.submit("info", lambda s: s.info(), lambda e, r: (result.update(err=e, info=r), done.set()))
        assert done.wait(10) and result["err"] is None
        assert wait_for(lambda: states.count(LIVE) > n), states
        assert Cmd.START_DOWNLOAD not in fake.sent

        c.set_live(False)
        assert wait_for(lambda: states[-1] == CONNECTED), states
        assert fake.play and fake.play["count"] == 1  # a still was left on screen
    finally:
        c.shutdown()


def test_frozen_controller_is_reported_and_left_alone_until_replug(monkeypatch, tmp_path):
    fake = FakeScreen(Clock())
    driver, conn = _setup(monkeypatch, tmp_path, fake)
    states, logs = [], []
    c = service.Controller(log=logs.append, on_state=states.append, find=lambda: [driver])
    fake.frozen = True
    c.start()
    try:
        assert wait_for(lambda: FROZEN in states), (states, logs)
        assert any("Power-cycle" in m for m in logs)
        sent = len(fake.sent)
        time.sleep(0.5)
        assert len(fake.sent) == sent  # nothing more is sent to a frozen controller

        fake.frozen = False  # the user power-cycled it: new USB device number
        conn["id"] = "3:11"
        assert wait_for(lambda: states[-1] == CONNECTED and fake.sent.count(Cmd.SET_TIME) == 1), states
    finally:
        c.shutdown()


def test_pick_prefers_a_model_or_usb_id():
    a, b = rdm.RdmDriver(by_key("rt82"), "1-1"), rdm.RdmDriver(by_key("qk108"), "1-2")
    assert service.pick([a, b]) is a
    assert service.pick([a, b], "qk108") is b
    assert service.pick([a, b], "36b0:30af") is b
    assert service.pick([], "rt82") is None


class FakeTftDriver:
    """A TFT driver whose sessions talk to the simulated keyboard."""

    def __init__(self, fake):
        from epomaker_screen.drivers.tft import TftDriver

        self.inner = TftDriver(0x3151, 0x5002, "1-4")
        self.fake = fake

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def connection_id(self):
        return "1:7"

    def open(self, log=lambda m: None):
        s = self.inner.open(log, transport_factory=lambda: self.fake)
        s.sleep = lambda t: None
        s.chunk_delay = 0
        return s


def test_controller_identifies_tft_keyboards_and_feeds_their_system_page(monkeypatch, tmp_path):
    from epomaker_screen.drivers.base import SystemInfo
    from epomaker_screen.drivers.tft import RefreshConfig
    from fake_tft import FakeTft

    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(service, "SYSTEM_INFO_S", 0.05)
    fake = FakeTft()
    driver = FakeTftDriver(fake)
    states, logs, seen = [], [], []
    c = service.Controller(log=logs.append, on_state=states.append, on_device=seen.append, find=lambda: [driver])
    c.set_system_info(lambda: SystemInfo(cpu_percent=42))
    c.start()
    try:
        assert wait_for(lambda: fake.clock is not None), logs
        # The keyboard was identified on first contact and the GUI told about its real screen.
        assert wait_for(lambda: any(d is driver and d.model and d.model.key == "rt100pro" for d in seen)), seen
        assert wait_for(lambda: fake.system_info is not None and fake.system_info[4] == 42)

        frame = bytes(240 * 240 * 3)
        c.set_live(True, lambda geometry: (lambda t: frame, frame), {"tft": RefreshConfig(interval_s=10)})
        assert wait_for(lambda: LIVE in states), (states, logs)
        assert wait_for(lambda: 0 in fake.banks)
        c.set_live(False)
        assert wait_for(lambda: states[-1] == CONNECTED), states
    finally:
        c.shutdown()
