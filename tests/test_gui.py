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
    assert window.canvas.width() == int(240 * window.canvas.scale) + 2
    assert window.layout is not rt82_layout and "RT100 Pro" in window.windowTitle()
    assert max(w["y"] + w["h"] for w in window.layout["widgets"]) > 135  # default layout scaled to the square screen
    window.canvas.repaint()
    window._on_device(None)  # unplugged: keep editing the last screen
    assert window.geometry.key == "240x240"


def test_undo_redo_and_alignment(window):
    before = len(window.layout["widgets"])
    window.add_widget("text")
    window.align_widget("hcenter")
    w = window.layout["widgets"][-1]
    assert w["x"] == (window.geometry.width - w["w"]) // 2
    window._hist_time = 0.0  # no merging with the next edit
    window.align_widget("top")
    assert window.layout["widgets"][-1]["y"] == 0
    while window.undo_action.isEnabled():
        window.undo()
    assert len(window.layout["widgets"]) == before
    window.redo()
    assert len(window.layout["widgets"]) == before + 1


def test_untick_hides_a_widget(window):
    item = window.list.item(0)  # topmost widget
    item.setCheckState(qt.QtCore.Qt.CheckState.Unchecked)
    assert window.layout["widgets"][-1]["hidden"] is True


def test_keyboard_menu_follows_the_family(window, monkeypatch):
    from epomaker_screen.drivers.tft import TftDriver
    from epomaker_screen.models import by_key

    assert not window.language_action.isEnabled()
    jobs = []
    monkeypatch.setattr(window.ctl, "submit", lambda name, run, done=None: jobs.append(name))
    window._on_device(TftDriver(0x3151, 0x5002, "1-4", model=by_key("rt100pro")))
    assert window.language_action.isEnabled() and window.erase_action.isEnabled()
    assert "5 pictures" in window.card.text()
    window.toggle_language()
    monkeypatch.setattr(
        qt.QtWidgets.QMessageBox, "question", staticmethod(lambda *a, **k: qt.QtWidgets.QMessageBox.StandardButton.Yes)
    )
    window.reset_screen()
    window.keyboard_info()
    assert jobs == ["language", "clear pictures", "keyboard info"]


def test_zoom_and_grid(window):
    window.zoom_box.setCurrentIndex(window.zoom_box.findData(3))
    assert window.canvas.width() == window.geometry.width * 3 + 2
    window._on_grid(True)
    window.canvas.repaint()
