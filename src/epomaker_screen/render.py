"""QImage helpers: screen-sized canvases, RGB888 export and media loading."""

from __future__ import annotations

from dataclasses import dataclass

from .qt import QtCore, QtGui, image_bytes

Qt = QtCore.Qt
FIT_MODES = {"contain": "Fit inside", "cover": "Fill (crop)", "stretch": "Stretch"}


@dataclass(frozen=True)
class Geometry:
    """Visible screen size, plus the frame height a driver wants (>= height)."""

    width: int
    height: int
    frame_height: int = 0

    @property
    def rows(self) -> int:
        return self.frame_height or self.height

    @property
    def key(self) -> str:
        return f"{self.width}x{self.height}"

    @classmethod
    def of(cls, caps) -> Geometry:
        return cls(caps.width, caps.height, caps.frame_height)


RT82 = Geometry(240, 135, 136)


def canvas(geometry: Geometry = RT82, color: str = "#000000") -> QtGui.QImage:
    img = QtGui.QImage(geometry.width, geometry.rows, QtGui.QImage.Format.Format_RGB888)
    img.fill(QtGui.QColor(color))
    return img


def to_rgb(img: QtGui.QImage, geometry: Geometry = RT82) -> bytes:
    """Packed RGB888 of ``width x rows``, as drivers want it."""
    w, rows = geometry.width, geometry.rows
    if img.width() != w or img.height() != rows:
        img = fit(img, "contain", geometry)
    img = img.convertToFormat(QtGui.QImage.Format.Format_RGB888)
    raw = image_bytes(img)
    line, stride = w * 3, img.bytesPerLine()
    if stride == line:
        return raw[: line * rows]
    return b"".join(raw[y * stride : y * stride + line] for y in range(rows))


def draw_fitted(p: QtGui.QPainter, src: QtGui.QImage, target: QtCore.QRectF, mode: str) -> None:
    if src.isNull():
        return
    sw, sh = src.width(), src.height()
    if mode == "stretch":
        p.drawImage(target, src)
        return
    scale = (max if mode == "cover" else min)(target.width() / sw, target.height() / sh)
    w, h = sw * scale, sh * scale
    dst = QtCore.QRectF(target.center().x() - w / 2, target.center().y() - h / 2, w, h)
    p.save()
    p.setClipRect(target)
    p.drawImage(dst, src)
    p.restore()


def fit(
    src: QtGui.QImage, mode: str = "contain", geometry: Geometry = RT82, background: str = "#000000"
) -> QtGui.QImage:
    out = canvas(geometry, background)
    p = QtGui.QPainter(out)
    p.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
    # Keep content on the visible rows (the RT82 panel hides the last frame row).
    draw_fitted(p, src, QtCore.QRectF(0, 0, geometry.width, geometry.height), mode)
    p.end()
    return out


def load_frames(
    path: str, mode: str = "contain", max_frames: int = 40, geometry: Geometry = RT82, read_limit: int = 1000
) -> tuple[list[QtGui.QImage], int]:
    """Loads a still or an animation (GIF, APNG, WebP). Returns frames and fps.

    Animations longer than ``max_frames`` are thinned out evenly, keeping
    their duration; 0 keeps every frame.
    """
    reader = QtGui.QImageReader(path)
    reader.setAutoTransform(True)
    frames, delays = [], []
    # read() moves on to the next image of an animation by itself.
    while len(frames) < read_limit and reader.canRead():
        img = reader.read()
        if img.isNull():
            break
        frames.append(img)
        delays.append(reader.nextImageDelay())
    if not frames:
        raise ValueError(f"cannot open {path}: {reader.errorString()}")
    if len(frames) == 1:
        return [fit(frames[0], mode, geometry)], 1
    good = [d for d in delays if d > 0]
    fps = 1000 / (sum(good) / len(good) if good else 100)
    if max_frames and len(frames) > max_frames:
        step = len(frames) / max_frames
        frames = [frames[int(i * step)] for i in range(max_frames)]
        fps /= step
    return [fit(f, mode, geometry) for f in frames], max(1, min(30, round(fps)))
