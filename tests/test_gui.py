import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
qt = pytest.importorskip("epomaker_screen.qt")

from epomaker_screen import gui  # noqa: E402
from epomaker_screen.widgets import TYPES  # noqa: E402


@pytest.fixture
def window(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(gui.Controller, "start", lambda self: None)  # no hardware in tests
    monkeypatch.setattr(qt.QtWidgets.QSystemTrayIcon, "isSystemTrayAvailable", staticmethod(lambda: True))
    app = qt.QtWidgets.QApplication.instance() or qt.QtWidgets.QApplication([])
    quits = []
    monkeypatch.setattr(app, "quit", lambda: quits.append(True))
    w = gui.MainWindow(app)
    w.quits = quits
    w.show()
    yield w
    w.quitting = True
    w.close()


@pytest.mark.parametrize("tray_on_close", [True, False])
def test_close_follows_the_tray_setting(window, tray_on_close):
    window.settings.tray_on_close = tray_on_close
    window.close()
    assert window.isVisible() is False
    assert bool(window.quits) is not tray_on_close
    assert window.quitting is not tray_on_close


def test_editor_actions_do_not_crash(window):
    for name in TYPES:
        window.add_widget(name)
    window.duplicate_widget()
    window.move_widget(-1)
    window.remove_widget()
    window.canvas.repaint()
    assert len(window.layout["widgets"]) > len(TYPES) - 1


def test_editor_follows_the_connected_keyboards_screen(window):
    from epomaker_screen.drivers.tft import TftDriver
    from epomaker_screen.models import by_key

    rt82_layout = window.layout
    window._on_device(TftDriver(0x3151, 0x5002, "1-4", model=by_key("rt100pro")))
    assert window.geometry.key == "240x240"
    assert window.canvas.width() == 240 * window.canvas.scale + 2
    assert window.layout is not rt82_layout and "RT100 Pro" in window.windowTitle()
    assert max(w["y"] + w["h"] for w in window.layout["widgets"]) > 135  # default layout scaled to the square screen
    window.canvas.repaint()
    window._on_device(None)  # unplugged: keep editing the last screen
    assert window.geometry.key == "240x240"
