"""QImage helpers: screen-sized canvases, RGB888 export and media loading."""

from __future__ import annotations

from .protocol import MAX_FRAMES, QGIF_HEIGHT, WIDTH
from .qt import QtCore, QtGui, image_bytes

Qt = QtCore.Qt
FIT_MODES = {"contain": "Fit inside", "cover": "Fill (crop)", "stretch": "Stretch"}


def canvas(color: str = "#000000") -> QtGui.QImage:
    img = QtGui.QImage(WIDTH, QGIF_HEIGHT, QtGui.QImage.Format.Format_RGB888)
    img.fill(QtGui.QColor(color))
    return img


def to_rgb(img: QtGui.QImage) -> bytes:
    """240x136 packed RGB888, as the QGIF encoder wants it."""
    if img.width() != WIDTH or img.height() != QGIF_HEIGHT:
        img = fit(img, "contain")
    img = img.convertToFormat(QtGui.QImage.Format.Format_RGB888)
    raw = image_bytes(img)
    line, stride = WIDTH * 3, img.bytesPerLine()
    if stride == line:
        return raw[: line * QGIF_HEIGHT]
    return b"".join(raw[y * stride : y * stride + line] for y in range(QGIF_HEIGHT))


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


def fit(src: QtGui.QImage, mode: str = "contain", background: str = "#000000") -> QtGui.QImage:
    out = canvas(background)
    p = QtGui.QPainter(out)
    p.setRenderHint(QtGui.QPainter.RenderHint.SmoothPixmapTransform)
    # The panel shows 135 rows; keep content off the hidden last row.
    draw_fitted(p, src, QtCore.QRectF(0, 0, WIDTH, QGIF_HEIGHT - 1), mode)
    p.end()
    return out


def load_frames(
    path: str, mode: str = "contain", max_frames: int = MAX_FRAMES, read_limit: int = 1000
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
        return [fit(frames[0], mode)], 1
    good = [d for d in delays if d > 0]
    fps = 1000 / (sum(good) / len(good) if good else 100)
    if max_frames and len(frames) > max_frames:
        step = len(frames) / max_frames
        frames = [frames[int(i * step)] for i in range(max_frames)]
        fps /= step
    return [fit(f, mode) for f in frames], max(1, min(30, round(fps)))
