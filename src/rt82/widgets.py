"""Screen widgets and layout rendering.

A layout is plain JSON::

    {"background": "#101820", "background_image": "", "widgets": [
        {"type": "clock", "x": 0, "y": 10, "w": 240, "h": 60, "props": {...}}, ...]}

Coordinates are screen pixels (240x135).
"""

from __future__ import annotations

import copy
import math
from dataclasses import dataclass
from typing import Any, Callable

from .protocol import HEIGHT, QGIF_HEIGHT, WIDTH
from .qt import QtCore, QtGui
from .render import FIT_MODES, canvas, draw_fitted

Qt = QtCore.Qt


@dataclass(frozen=True)
class Prop:
    key: str
    label: str
    kind: str  # text, multiline, color, int, float, bool, font, choice, file
    default: Any
    options: dict | None = None  # choice: value -> label; int/float: {"min":, "max":}


ALIGN = {"left": "Left", "center": "Center", "right": "Right"}
TEXT_PROPS = [
    Prop("color", "Color", "color", "#ffffff"),
    Prop("font", "Font", "font", "Sans Serif"),
    Prop("size", "Size, px", "int", 20, {"min": 6, "max": 130}),
    Prop("bold", "Bold", "bool", False),
    Prop("align", "Alignment", "choice", "center", ALIGN),
    Prop("fit", "Shrink to fit", "bool", True),
]

METRICS = {
    "cpu": "CPU load, %",
    "ram": "Memory, %",
    "swap": "Swap, %",
    "temp": "CPU temperature, °C",
    "disk": "Disk /, %",
    "net_down": "Network: download",
    "net_up": "Network: upload",
}


@dataclass
class Context:
    t: float  # wall-clock time shown on the frame
    data: dict
    preview: bool = False


@dataclass(frozen=True)
class WidgetType:
    name: str
    title: str
    props: list[Prop]
    paint: Callable[[QtGui.QPainter, QtCore.QRectF, dict, Context], None]
    size: tuple[int, int] = (120, 30)


# -- drawing helpers ---------------------------------------------------------


def _font(props: dict, size: float | None = None) -> QtGui.QFont:
    f = QtGui.QFont(props.get("font") or "Sans Serif")
    f.setPixelSize(max(1, int(size or props.get("size", 20))))
    f.setBold(bool(props.get("bold")))
    f.setHintingPreference(QtGui.QFont.HintingPreference.PreferFullHinting)
    return f


def _align(props: dict) -> Qt.AlignmentFlag:
    h = {"left": Qt.AlignmentFlag.AlignLeft, "right": Qt.AlignmentFlag.AlignRight}.get(
        props.get("align", "center"), Qt.AlignmentFlag.AlignHCenter
    )
    return h | Qt.AlignmentFlag.AlignVCenter


def draw_text(p: QtGui.QPainter, r: QtCore.QRectF, text: str, props: dict, color: str | None = None) -> None:
    size = float(props.get("size", 20))
    font = _font(props, size)
    if props.get("fit", True) and text:
        while size > 6:
            m = QtGui.QFontMetricsF(font)
            br = m.boundingRect(r, int(_align(props)) | int(Qt.TextFlag.TextWordWrap), text)
            if br.width() <= r.width() + 0.5 and br.height() <= r.height() + 0.5:
                break
            size -= 1
            font = _font(props, size)
    p.setFont(font)
    p.setPen(QtGui.QColor(color or props.get("color", "#ffffff")))
    p.drawText(r, int(_align(props)) | int(Qt.TextFlag.TextWordWrap), text)


def _datetime(t: float) -> QtCore.QDateTime:
    return QtCore.QDateTime.fromMSecsSinceEpoch(int(t * 1000))


def _human_rate(v: float) -> str:
    for unit in ("B/s", "KB/s", "MB/s", "GB/s"):
        if v < 1024 or unit == "GB/s":
            return f"{v:.0f} {unit}" if unit == "B/s" else f"{v:.1f} {unit}"
        v /= 1024
    return ""


def _metric_text(metric: str, v: float | None) -> str:
    if v is None:
        return "—"
    if metric.startswith("net"):
        return _human_rate(v)
    if metric == "temp":
        return f"{v:.0f}°C"
    return f"{v:.0f}%"


# -- widgets -------------------------------------------------------------------


def paint_clock(p, r, props, ctx):
    draw_text(p, r, QtCore.QLocale().toString(_datetime(ctx.t), props.get("format", "HH:mm")), props)


def paint_date(p, r, props, ctx):
    draw_text(p, r, QtCore.QLocale().toString(_datetime(ctx.t), props.get("format", "ddd, d MMMM")), props)


def paint_text(p, r, props, ctx):
    draw_text(p, r, props.get("text", ""), props)


def paint_panel(p, r, props, ctx):
    p.save()
    p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
    pen = QtGui.QPen(QtGui.QColor(props.get("border_color", "#ffffff")))
    pen.setWidthF(float(props.get("border", 0)))
    p.setPen(pen if props.get("border", 0) else Qt.PenStyle.NoPen)
    p.setBrush(QtGui.QColor(props.get("color", "#203040")))
    rad = float(props.get("radius", 6))
    p.drawRoundedRect(r.adjusted(0.5, 0.5, -0.5, -0.5), rad, rad)
    p.restore()


_images: dict[str, QtGui.QImage] = {}


def _image(path: str) -> QtGui.QImage:
    img = _images.get(path)
    if img is None:
        img = QtGui.QImage(path) if path else QtGui.QImage()
        if len(_images) > 32:
            _images.clear()
        _images[path] = img
    return img


def paint_image(p, r, props, ctx):
    img = _image(props.get("path", ""))
    if img.isNull():
        if ctx.preview:
            draw_text(p, r, "no image", {"size": 11, "color": "#888888"})
        return
    p.save()
    p.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
    draw_fitted(p, img, r, props.get("mode", "contain"))
    p.restore()


def paint_sysmon(p, r, props, ctx):
    sysd = ctx.data.get("sys", {})
    metric = props.get("metric", "cpu")
    v = sysd.get(metric)
    label = props.get("label", "")
    style = props.get("style", "text")
    color = QtGui.QColor(props.get("color", "#ffffff"))
    accent = QtGui.QColor(props.get("accent", "#38bdf8"))
    text = f"{label} {_metric_text(metric, v)}".strip()
    if style == "text":
        draw_text(p, r, text, props)
        return
    p.save()
    if style == "bar":
        frac = 0.0 if v is None else max(0.0, min(1.0, v / (100.0 if metric != "temp" else 100.0)))
        if metric.startswith("net"):
            frac = 0.0 if v is None else min(1.0, math.log10(1 + v) / 8)
        p.fillRect(r, QtGui.QColor(props.get("track", "#333333")))
        p.fillRect(QtCore.QRectF(r.left(), r.top(), r.width() * frac, r.height()), accent)
        draw_text(p, r, text, props, color.name())
    else:  # graph
        hist = sysd.get("history", {}).get(metric, [])
        if hist:
            top = max(hist) if metric.startswith("net") else 100.0
            top = top or 1.0
            n = max(2, len(hist))
            path = QtGui.QPainterPath(QtCore.QPointF(r.right(), r.bottom()))
            for i, hv in enumerate(reversed(hist)):
                x = r.right() - i * r.width() / (n - 1)
                path.lineTo(x, r.bottom() - r.height() * min(1.0, hv / top))
            path.lineTo(r.right() - (len(hist) - 1) * r.width() / (n - 1), r.bottom())
            path.closeSubpath()
            fill = QtGui.QColor(accent)
            fill.setAlpha(110)
            p.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing)
            p.setPen(QtGui.QPen(accent, 1))
            p.setBrush(fill)
            p.drawPath(path)
        small = dict(props, size=min(int(props.get("size", 12)), int(r.height())), align="left")
        draw_text(p, r.adjusted(2, 0, 0, 0), text, small, color.name())
    p.restore()


def _marquee(p, r, text, props, ctx):
    font = _font(props)
    width = QtGui.QFontMetricsF(font).horizontalAdvance(text)
    if width <= r.width() or not props.get("scroll", True):
        draw_text(p, r, text, props)
        return
    speed = float(props.get("speed", 20))  # px/s
    gap = 40
    off = (ctx.t * speed) % (width + gap)
    p.save()
    p.setClipRect(r)
    p.setFont(font)
    p.setPen(QtGui.QColor(props.get("color", "#ffffff")))
    for x in (r.left() - off, r.left() - off + width + gap):
        p.drawText(QtCore.QRectF(x, r.top(), width + 1, r.height()), int(Qt.AlignmentFlag.AlignVCenter), text)
    p.restore()


def paint_media(p, r, props, ctx):
    m = ctx.data.get("media") or {}
    if not m.get("title"):
        text = props.get("idle", "♪ —")
    else:
        text = f"{m.get('artist')} — {m['title']}" if m.get("artist") else m["title"]
        if m.get("status") == "Paused":
            text = "❚❚ " + text
    _marquee(p, r, text, dict(props, fit=False), ctx)


def paint_weather(p, r, props, ctx):
    try:
        key = f"weather:{round(float(props.get('lat', 55.75)), 3)},{round(float(props.get('lon', 37.62)), 3)}"
    except (TypeError, ValueError):
        key = ""
    w = ctx.data.get(key) or {}
    if "temp" in w and w["temp"] is not None:
        text = (
            props.get("format", "{city} {temp:+.0f}°C {text}")
            .format(city=props.get("city", ""), temp=float(w["temp"]), text=w.get("text", ""), wind=w.get("wind") or 0)
            .strip()
        )
    else:
        text = "weather…" if "error" not in w else "offline"
    draw_text(p, r, text, props)


def paint_command(p, r, props, ctx):
    out = ctx.data.get("cmd:" + props.get("command", ""), "…" if props.get("command") else "")
    if props.get("scroll") and "\n" not in out:
        _marquee(p, r, out, props, ctx)
    else:
        draw_text(p, r, out, props)


def paint_progress_day(p, r, props, ctx):
    dt = _datetime(ctx.t)
    secs = dt.time().msecsSinceStartOfDay() / 1000
    frac = secs / 86400
    p.fillRect(r, QtGui.QColor(props.get("track", "#333333")))
    p.fillRect(
        QtCore.QRectF(r.left(), r.top(), r.width() * frac, r.height()), QtGui.QColor(props.get("accent", "#f59e0b"))
    )
    if props.get("show_text", True):
        draw_text(p, r, f"{frac * 100:.0f}% of the day", props)


TYPES: dict[str, WidgetType] = {}


def _register(t: WidgetType) -> None:
    TYPES[t.name] = t


_register(
    WidgetType(
        "clock",
        "Clock",
        [Prop("format", "Format (Qt: HH:mm:ss)", "text", "HH:mm"), *TEXT_PROPS],
        paint_clock,
        (200, 60),
    )
)
_register(
    WidgetType(
        "date",
        "Date",
        [Prop("format", "Format (Qt: dddd, d MMMM yyyy)", "text", "ddd, d MMMM"), *TEXT_PROPS],
        paint_date,
        (200, 24),
    )
)
_register(WidgetType("text", "Text", [Prop("text", "Text", "multiline", "Hello!"), *TEXT_PROPS], paint_text, (160, 30)))
_register(
    WidgetType(
        "sysmon",
        "System monitor",
        [
            Prop("metric", "Metric", "choice", "cpu", METRICS),
            Prop("style", "Style", "choice", "text", {"text": "Text", "bar": "Bar", "graph": "Graph"}),
            Prop("label", "Label", "text", "CPU"),
            Prop("accent", "Bar/graph color", "color", "#38bdf8"),
            Prop("track", "Bar background", "color", "#333333"),
            *TEXT_PROPS,
        ],
        paint_sysmon,
        (110, 22),
    )
)
_register(
    WidgetType(
        "media",
        "Now playing (MPRIS)",
        [
            Prop("idle", "Text when nothing plays", "text", "♪ —"),
            Prop("scroll", "Scroll long text", "bool", True),
            Prop("speed", "Speed, px/s", "int", 20, {"min": 1, "max": 200}),
            *TEXT_PROPS,
        ],
        paint_media,
        (240, 22),
    )
)
_register(
    WidgetType(
        "weather",
        "Weather (Open-Meteo)",
        [
            Prop("city", "Label", "text", "Moscow"),
            Prop("lat", "Latitude", "float", 55.75, {"min": -90, "max": 90}),
            Prop("lon", "Longitude", "float", 37.62, {"min": -180, "max": 180}),
            Prop("format", "Format ({city} {temp} {text} {wind})", "text", "{city} {temp:+.0f}°C {text}"),
            *TEXT_PROPS,
        ],
        paint_weather,
        (240, 22),
    )
)
_register(
    WidgetType(
        "command",
        "Command output",
        [
            Prop("command", "Shell command", "text", "uptime -p"),
            Prop("interval", "Interval, s", "int", 30, {"min": 1, "max": 86400}),
            Prop("scroll", "Scroll long text", "bool", False),
            Prop("speed", "Speed, px/s", "int", 20, {"min": 1, "max": 200}),
            *TEXT_PROPS,
        ],
        paint_command,
        (240, 22),
    )
)
_register(
    WidgetType(
        "image",
        "Image",
        [Prop("path", "File", "file", ""), Prop("mode", "Scaling", "choice", "contain", FIT_MODES)],
        paint_image,
        (60, 60),
    )
)
_register(
    WidgetType(
        "panel",
        "Panel",
        [
            Prop("color", "Color", "color", "#203040"),
            Prop("radius", "Corner radius", "int", 6, {"min": 0, "max": 60}),
            Prop("border", "Border, px", "int", 0, {"min": 0, "max": 10}),
            Prop("border_color", "Border color", "color", "#ffffff"),
        ],
        paint_panel,
        (120, 40),
    )
)
_register(
    WidgetType(
        "dayprogress",
        "Day progress",
        [
            Prop("accent", "Color", "color", "#f59e0b"),
            Prop("track", "Background", "color", "#333333"),
            Prop("show_text", "Show percentage", "bool", True),
            *TEXT_PROPS,
        ],
        paint_progress_day,
        (240, 12),
    )
)


def new_widget(type_name: str, x: int = 10, y: int = 10) -> dict:
    t = TYPES[type_name]
    return {
        "type": type_name,
        "x": x,
        "y": y,
        "w": t.size[0],
        "h": t.size[1],
        "props": {p.key: p.default for p in t.props},
    }


def props_of(widget: dict) -> dict:
    t = TYPES.get(widget.get("type", ""))
    base = {p.key: p.default for p in t.props} if t else {}
    base.update(widget.get("props", {}))
    return base


DEFAULT_LAYOUT = {
    "background": "#0b1220",
    "background_image": "",
    "background_mode": "cover",
    "widgets": [
        {
            "type": "clock",
            "x": 0,
            "y": 4,
            "w": 240,
            "h": 62,
            "props": {"format": "HH:mm", "size": 60, "bold": True, "color": "#f8fafc"},
        },
        {"type": "date", "x": 0, "y": 64, "w": 240, "h": 22, "props": {"size": 18, "color": "#94a3b8"}},
        {
            "type": "sysmon",
            "x": 6,
            "y": 92,
            "w": 112,
            "h": 18,
            "props": {"metric": "cpu", "style": "bar", "label": "CPU", "size": 12},
        },
        {
            "type": "sysmon",
            "x": 122,
            "y": 92,
            "w": 112,
            "h": 18,
            "props": {"metric": "ram", "style": "bar", "label": "RAM", "size": 12, "accent": "#a78bfa"},
        },
        {"type": "media", "x": 4, "y": 113, "w": 232, "h": 20, "props": {"size": 13, "color": "#e2e8f0"}},
    ],
}


def default_layout() -> dict:
    return copy.deepcopy(DEFAULT_LAYOUT)


def render_layout(layout: dict, ctx: Context, selected: int | None = None) -> QtGui.QImage:
    img = canvas(layout.get("background", "#000000"))
    p = QtGui.QPainter(img)
    p.setRenderHint(QtGui.QPainter.RenderHint.TextAntialiasing)
    if layout.get("background_image"):
        bg = _image(layout["background_image"])
        draw_fitted(p, bg, QtCore.QRectF(0, 0, WIDTH, HEIGHT), layout.get("background_mode", "cover"))
    for i, w in enumerate(layout.get("widgets", [])):
        t = TYPES.get(w.get("type", ""))
        if t is None or w.get("hidden"):
            continue
        r = QtCore.QRectF(float(w.get("x", 0)), float(w.get("y", 0)), float(w.get("w", 10)), float(w.get("h", 10)))
        p.save()
        p.setClipRect(r)
        try:
            t.paint(p, r, props_of(w), ctx)
        except Exception as e:  # a broken widget must not stop the screen
            draw_text(p, r, f"error: {e}", {"size": 10, "color": "#ff5555"})
        p.restore()
        if ctx.preview and i == selected:
            p.setPen(QtGui.QPen(QtGui.QColor("#ff00ff"), 1, Qt.PenStyle.DashLine))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawRect(r.adjusted(0, 0, -1, -1))
    p.end()
    if QGIF_HEIGHT > HEIGHT:  # hidden bottom row
        q = QtGui.QPainter(img)
        q.fillRect(0, HEIGHT, WIDTH, QGIF_HEIGHT - HEIGHT, QtGui.QColor(layout.get("background", "#000000")))
        q.end()
    return img


def renderer(get_layout: Callable[[], dict], snapshot: Callable[[], dict]) -> tuple[Callable[[float], bytes], bytes]:
    """Live-mode render function, plus the fallback still.

    ``get_layout`` is called for every frame, so edits show up without
    restarting live mode (return a new dict instead of mutating the old one).
    """
    import time

    from .render import to_rgb

    def render(t: float) -> bytes:
        return to_rgb(render_layout(get_layout(), Context(t, snapshot())))

    fallback = to_rgb(render_layout(fallback_layout(get_layout()), Context(time.time(), {})))
    return render, fallback


def fallback_layout(layout: dict) -> dict:
    """The layout without live data: shown for a few seconds while flash is erased."""
    out = copy.deepcopy(layout)
    out["widgets"] = [w for w in out.get("widgets", []) if w.get("type") in ("panel", "image", "text")]
    return out
