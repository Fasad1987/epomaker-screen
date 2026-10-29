import threading
import time

from rt82 import device, live, service
from rt82.device import Screen
from rt82.live import LiveConfig
from rt82.protocol import Cmd

from fake_screen import Clock, FakeScreen
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
    monkeypatch.setattr(service, "keyboard_present", lambda: True)
    monkeypatch.setattr(service.Screen, "connect", classmethod(lambda cls, timeout=15.0, log=None: Screen(fake)))
    monkeypatch.setattr(service.Keyboard, "open", classmethod(lambda cls: FakeKeyboard()))
    monkeypatch.setattr(live.Keyboard, "open", classmethod(lambda cls: FakeKeyboard()))

    states, logs = [], []
    c = service.Controller(log=logs.append, on_state=states.append)
    c.set_live(True, lambda: (stamp, stamp(0)), LiveConfig(fps=2))
    c.start()
    try:
        assert wait_for(lambda: "живой режим" in states), (states, logs)
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
        assert fake.downloads == 1

        # Any other job interrupts live mode, which comes back afterwards.
        done.clear()
        n = states.count("живой режим")
        c.submit("info", lambda s: s.info(), lambda e, r: (result.update(err=e, info=r), done.set()))
        assert done.wait(10) and result["err"] is None
        assert wait_for(lambda: states.count("живой режим") > n), states
        assert fake.downloads == 2

        c.set_live(False)
        assert wait_for(lambda: states[-1] == "подключена"), states
        assert fake.play and fake.play["count"] == 1  # a still was left on screen
    finally:
        c.shutdown()
