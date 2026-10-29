"""QImage helpers: screen-sized canvases, RGB888 export and media loading."""

from __future__ import annotations

from .protocol import QGIF_HEIGHT, WIDTH
from .qt import QtCore, QtGui, image_bytes

Qt = QtCore.Qt
FIT_MODES = {"contain": "Вписать", "cover": "Заполнить (обрезать)", "stretch": "Растянуть"}


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


def load_frames(path: str, mode: str = "contain", max_frames: int = 300) -> tuple[list[QtGui.QImage], int]:
    """Loads a still or an animation (GIF, APNG, WebP). Returns frames and fps."""
    reader = QtGui.QImageReader(path)
    reader.setAutoTransform(True)
    frames, delays = [], []
    # read() moves on to the next image of an animation by itself.
    while len(frames) < max_frames and reader.canRead():
        img = reader.read()
        if img.isNull():
            break
        frames.append(fit(img, mode))
        delays.append(reader.nextImageDelay())
    if not frames:
        raise ValueError(f"не удалось открыть {path}: {reader.errorString()}")
    if len(frames) == 1:
        return frames, 1
    good = [d for d in delays if d > 0]
    delay = sum(good) / len(good) if good else 100
    fps = max(1, min(30, round(1000 / delay)))
    return frames, fps
