import datetime as dt
import os

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
qt = pytest.importorskip("epomaker_screen.qt")

from epomaker_screen import widgets  # noqa: E402

app = qt.QtWidgets.QApplication.instance() or qt.QtWidgets.QApplication([])
DATA = {"sys": {"cpu": 42.0, "ram": 60.0, "net_down": 2048.0, "history": {"cpu": [10.0, 50.0, 30.0]}}}


def variants():
    for name, t in widgets.TYPES.items():
        yield name, {}
        for prop in t.props:
            if prop.kind == "choice":
                for value in prop.options:
                    yield name, {prop.key: value}


@pytest.mark.parametrize(("name", "override"), list(variants()))
def test_every_widget_style_paints(name, override):
    t = widgets.TYPES[name]
    props = {p.key: p.default for p in t.props} | override
    img = qt.QtGui.QImage(240, 135, qt.QtGui.QImage.Format.Format_RGB32)
    p = qt.QtGui.QPainter(img)
    try:
        t.paint(p, qt.QtCore.QRectF(0, 0, 120, 80), props, widgets.Context(1_800_000_000, DATA, preview=True))
    finally:
        p.end()


def test_period_fraction():
    t = dt.datetime(2026, 7, 2, 12, 0).timestamp()
    assert widgets.period_fraction(t, "day") == pytest.approx(0.5)
    assert 0 < widgets.period_fraction(t, "week") < 1
    assert widgets.period_fraction(t, "month") == pytest.approx(1.5 / 31)
    assert widgets.period_fraction(t, "year") == pytest.approx(0.5, abs=0.01)


def test_countdown_text():
    now = dt.datetime(2026, 12, 30, 22, 0).timestamp()
    props = {"target": "2026-12-31 23:59", "label": "NY", "format": "{label} {d}d {h:02}:{m:02}"}
    assert widgets.countdown_text(now, props) == "NY 1d 01:59"
    assert widgets.countdown_text(now + 3 * 86400, props | {"done": "!"}) == "!"
    assert "set a date" in widgets.countdown_text(now, props | {"target": "soon"})
    assert "format" in widgets.countdown_text(now, props | {"format": "{x}"})


def test_weather_icons():
    assert widgets._weather_icon(0).startswith("☀")
    assert widgets._weather_icon(61).startswith("☂")
    assert widgets._weather_icon(-1) == ""
