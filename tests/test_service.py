import threading
import time

from fake_screen import Clock, FakeScreen
from rt82 import device, live, service  # noqa: F401
from rt82.device import Screen
from rt82.live import LiveConfig
from rt82.protocol import Cmd
from rt82.service import CONNECTED, FROZEN, LIVE
from test_live import FakeKeyboard, stamp


def wait_for(cond, timeout=10.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def test_controller_syncs_time_streams_and_runs_jobs(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(device.time, "sleep", lambda s: None)
    fake = FakeScreen(Clock())
    cmds = []
    orig = fake.write
    fake.write = lambda p: (cmds.append(p[1]), orig(p))
    monkeypatch.setattr(service, "keyboard_id", lambda: "3:10")
    monkeypatch.setattr(service.Screen, "connect", classmethod(lambda cls, timeout=15.0, log=None: Screen(fake)))
    monkeypatch.setattr(service.Keyboard, "open", classmethod(lambda cls: FakeKeyboard()))
    monkeypatch.setattr(live.Keyboard, "open", classmethod(lambda cls: FakeKeyboard()))

    states, logs = [], []
    c = service.Controller(log=logs.append, on_state=states.append)
    c.set_live(True, lambda: (stamp, stamp(0)), LiveConfig(fps=2))
    c.start()
    try:
        assert wait_for(lambda: LIVE in states), (states, logs)
        assert Cmd.SET_TIME in cmds  # time synced on connect
        before = cmds.count(Cmd.WRITE)
        assert wait_for(lambda: cmds.count(Cmd.WRITE) > before + 5)  # frames keep flowing

        # Time sync while live: done between frames, no restart (no second download session).
        done = threading.Event()
        result = {}
        syncs = cmds.count(Cmd.SET_TIME)
        c.sync_now(lambda e, r: (result.update(err=e), done.set()))
        assert done.wait(10) and result["err"] is None
        assert cmds.count(Cmd.SET_TIME) == syncs + 1
        assert Cmd.ERASE not in cmds[-20:]  # live mode was not restarted

        # Any other job interrupts live mode, which comes back afterwards.
        done.clear()
        n = states.count(LIVE)
        c.submit("info", lambda s: s.info(), lambda e, r: (result.update(err=e, info=r), done.set()))
        assert done.wait(10) and result["err"] is None
        assert wait_for(lambda: states.count(LIVE) > n), states
        assert Cmd.START_DOWNLOAD not in cmds

        c.set_live(False)
        assert wait_for(lambda: states[-1] == CONNECTED), states
        assert fake.play and fake.play["count"] == 1  # a still was left on screen
    finally:
        c.shutdown()


def _patched(monkeypatch, tmp_path, fake):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(device.time, "sleep", lambda s: None)
    conn = {"id": "3:10"}
    monkeypatch.setattr(service, "keyboard_id", lambda: conn["id"])
    monkeypatch.setattr(
        service.Screen, "connect", classmethod(lambda cls, timeout=15.0, log=None, probe=True: _connect(fake, probe))
    )
    monkeypatch.setattr(service.Keyboard, "open", classmethod(lambda cls: FakeKeyboard()))
    return conn


def _connect(fake, probe):
    s = Screen(fake)
    if probe:
        s.probe()
    return s


def test_frozen_controller_is_reported_and_left_alone_until_replug(monkeypatch, tmp_path):
    fake = FakeScreen(Clock())
    conn = _patched(monkeypatch, tmp_path, fake)
    states, logs = [], []
    c = service.Controller(log=logs.append, on_state=states.append)
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
