"""rt82ctl: command-line control of the Epomaker RT82 screen."""

from __future__ import annotations

import argparse
import datetime as dt
import os
import signal
import sys
import threading
import time
from pathlib import Path

from . import __version__, hidraw
from .device import DeviceError, screen_session
from .protocol import KEYBOARD_PID, KEYBOARD_VID, MAX_SLOTS, QGIF_HEIGHT, SCREEN_PID, SCREEN_VID, WIDTH

SOFT_LIMIT = 1536 * 1024  # verified uploads are well below this; flash size of the RT82 is unknown


def log(msg: str) -> None:
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


_app = None


def _qt_app():
    """Fonts and painting need a QGuiApplication; it must stay referenced."""
    global _app
    if _app is None:
        if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
            os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        from .qt import QtGui

        _app = QtGui.QGuiApplication.instance() or QtGui.QGuiApplication(sys.argv[:1])
    return _app


def cmd_list(a) -> int:
    found = False
    for d in hidraw.enumerate_hidraw():
        if (d.vendor_id, d.product_id) in ((KEYBOARD_VID, KEYBOARD_PID), (SCREEN_VID, SCREEN_PID)):
            found = True
            ok = os.access(d.node, os.R_OK | os.W_OK)
            up = f"0x{d.usage_page:04X}" if d.usage_page is not None else "?"
            print(f"{d.node}  {d.vendor_id:04x}:{d.product_id:04x}  if={d.interface}  usage_page={up}  "
                  f"report={d.out_size}  доступ={'да' if ok else 'НЕТ'}  {d.name}")
    if not found:
        print("RT82 не найдена (36b0:30a3). Подключите кабелем USB.")
        return 1
    return 0


def cmd_sync(a) -> int:
    with screen_session(log) as s:
        s.sync_time()
    log("время установлено")
    return 0


def cmd_info(a) -> int:
    with screen_session(log) as s:
        info = s.info()
        print(f"экран: {info['width']}x{info['height']}, поворот {info['rotation']}, связь: {s.status()}")
    return 0


def _encode_files(paths: list[str], fit: str, fps: int | None) -> list[bytes]:
    _qt_app()
    from . import qgif
    from .render import load_frames, to_rgb

    out = []
    for p in paths:
        frames, file_fps = load_frames(p, fit)
        data = qgif.encode([to_rgb(f) for f in frames], WIDTH, QGIF_HEIGHT, fps or file_fps)
        log(f"{p}: {len(frames)} кадр(ов), {fps or file_fps} fps, {len(data) // 1024} КБ")
        out.append(data)
    return out


def _upload(images: list[bytes]) -> int:
    total = sum(map(len, images))
    if total > SOFT_LIMIT:
        log(f"внимание: {total // 1024} КБ — больше, чем проверено на RT82; при артефактах уменьшите число кадров")
    with screen_session(log) as s:
        last = [0.0]

        def progress(done: int, all_: int) -> None:
            if time.monotonic() - last[0] > 0.5 or done == all_:
                last[0] = time.monotonic()
                print(f"\r  загрузка {done * 100 // all_}%", end="", flush=True)

        s.upload(images, progress=progress)
        print()
    log("готово: Fn + правый Shift показывает страницу GIF")
    return 0


def cmd_upload(a) -> int:
    if len(a.files) > MAX_SLOTS:
        log(f"RT82 хранит до {MAX_SLOTS} экранов")
        return 2
    images = []
    for f in a.files:
        if f.endswith(".qgif"):
            images.append(Path(f).read_bytes())
        else:
            images += _encode_files([f], a.fit, a.fps)
    return _upload(images)


def _layout(a) -> dict:
    from .config import load_layout

    return load_layout(Path(a.file) if a.file else None)


def cmd_preview(a) -> int:
    _qt_app()
    from .data import DataHub
    from .widgets import Context, render_layout

    layout = _layout(a)
    hub = DataHub()
    hub.configure(layout)
    hub.start()
    time.sleep(1.5)  # let the first samples arrive
    img = render_layout(layout, Context(time.time(), hub.snapshot()))
    img.save(a.output)
    log(f"сохранено {a.output}")
    return 0


def cmd_layout(a) -> int:
    _qt_app()
    from . import qgif
    from .data import DataHub
    from .render import to_rgb
    from .widgets import Context, render_layout

    layout = _layout(a)
    hub = DataHub()
    hub.configure(layout)
    hub.start()
    time.sleep(1.5)
    img = render_layout(layout, Context(time.time(), hub.snapshot()))
    return _upload([qgif.encode([to_rgb(img)], WIDTH, QGIF_HEIGHT, 1)])


def _install_stop() -> threading.Event:
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    return stop


def cmd_live(a) -> int:
    _qt_app()
    from .config import Settings
    from .data import DataHub
    from .device import Keyboard, exclusive
    from .device import Screen
    from .live import LiveStreamer
    from .widgets import renderer

    layout = _layout(a)
    cfg = Settings.load().live_config
    if a.fps:
        cfg.fps = a.fps
    hub = DataHub()
    hub.configure(layout)
    hub.start()
    stop = _install_stop()
    render, fallback = renderer(lambda: layout, hub.snapshot)
    with exclusive():
        s = Screen.connect(log=log)
        try:
            kb = Keyboard.open()
            try:
                st = LiveStreamer(s, render, fallback, cfg, log=log)
                st.start(kb)
            finally:
                kb.close()
            log("живой режим: Ctrl+C для остановки")
            st.run_for(stop)
            st.stop()
        finally:
            s.close()
    return 0


def cmd_watch(a) -> int:
    from .config import Settings
    from .service import Controller

    settings = Settings.load()
    stop = _install_stop()
    c = Controller(log=log, auto_sync=True, resync_hours=settings.resync_hours)
    if a.live:
        _qt_app()
        from .data import DataHub
        from .widgets import renderer

        layout = _layout(a)
        hub = DataHub()
        hub.configure(layout)
        hub.start()
        c.set_live(True, lambda: renderer(lambda: layout, hub.snapshot), settings.live_config)
    c.start()
    log("слежу за подключением клавиатуры (время синхронизируется автоматически)")
    stop.wait()
    c.shutdown()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="rt82ctl", description="Управление экраном Epomaker RT82 без браузера")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="показать интерфейсы RT82 и права доступа").set_defaults(fn=cmd_list)
    sub.add_parser("sync-time", help="установить время на клавиатуре").set_defaults(fn=cmd_sync)
    sub.add_parser("info", help="параметры экрана и тип подключения").set_defaults(fn=cmd_info)
    up = sub.add_parser("upload", help=f"загрузить до {MAX_SLOTS} картинок/GIF/.qgif")
    up.add_argument("files", nargs="+")
    up.add_argument("--fit", choices=["contain", "cover", "stretch"], default="contain")
    up.add_argument("--fps", type=int, help="переопределить частоту кадров (1..30)")
    up.set_defaults(fn=cmd_upload)
    for name, fn, hlp in (
        ("layout", cmd_layout, "отрисовать раскладку виджетов один раз и загрузить картинкой"),
        ("live", cmd_live, "живой режим: виджеты обновляются постоянно (Ctrl+C — стоп)"),
        ("preview", cmd_preview, "сохранить раскладку в PNG"),
        ("watch", cmd_watch, "фоновая служба: синхронизация времени при подключении"),
    ):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("--file", help="файл раскладки (по умолчанию ~/.config/rt82-plasma/layout.json)")
        if name == "preview":
            p.add_argument("output", nargs="?", default="rt82-preview.png")
        if name == "live":
            p.add_argument("--fps", type=int)
        if name == "watch":
            p.add_argument("--live", action="store_true", help="также держать живой режим")
        p.set_defaults(fn=fn)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except (DeviceError, PermissionError, ValueError) as e:
        log(f"ошибка: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
