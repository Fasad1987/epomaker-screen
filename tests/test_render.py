import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
qt = pytest.importorskip("epomaker_screen.qt")

from epomaker_screen.drivers.rdm import qgif  # noqa: E402
from epomaker_screen.drivers.rdm.protocol import QGIF_HEIGHT, WIDTH  # noqa: E402
from epomaker_screen.render import to_rgb  # noqa: E402
from epomaker_screen.widgets import TYPES, Context, default_layout, new_widget, render_layout, renderer  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def app():
    a = qt.QtGui.QGuiApplication.instance() or qt.QtGui.QGuiApplication([])
    yield a


SAMPLE = {
    "sys": {
        "cpu": 42.0,
        "ram": 63.0,
        "temp": 55.0,
        "net_down": 123456.0,
        "disk": 70.0,
        "history": {"cpu": [10, 50, 90, 20], "net_down": [1, 1000, 50]},
    },
    # Non-ASCII on purpose: track titles come in any script.
    "media": {"status": "Playing", "artist": "Кино", "title": "Группа крови"},
    "weather:55.75,37.62": {"temp": -3.4, "text": "Snow", "code": 71, "wind": 4},
    "cmd:uptime -p": "up 3 hours",
}


@pytest.mark.parametrize("type_name", list(TYPES))
def test_every_widget_renders(type_name):
    layout = {"background": "#000000", "widgets": [new_widget(type_name, 0, 0)]}
    if type_name == "sysmon":
        layout["widgets"] += [
            dict(new_widget("sysmon", 0, 40), props={"style": s, "metric": m})
            for s in ("bar", "graph")
            for m in ("cpu", "net_down")
        ]
    for data in ({}, SAMPLE):
        img = render_layout(layout, Context(time.time(), data))
        assert (img.width(), img.height()) == (WIDTH, QGIF_HEIGHT)
        assert len(to_rgb(img)) == WIDTH * QGIF_HEIGHT * 3


def test_renderer_follows_layout_edits_and_encodes():
    current = {"layout": default_layout()}
    render, fallback = renderer(lambda: current["layout"], lambda: SAMPLE)
    a = render(time.time())
    current["layout"] = dict(current["layout"], background="#ff0000")
    b = render(time.time())
    assert a != b and len(fallback) == len(a)
    data = qgif.encode([a, b], WIDTH, QGIF_HEIGHT, fps=1)
    assert qgif.decode(data)[3][1][:3] == bytes([0xFF, 0, 0])
