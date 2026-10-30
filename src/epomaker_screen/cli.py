"""epomakerctl: command-line control of Epomaker keyboard screens."""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import os
import signal
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

from . import __version__, devices, hid
from .drivers.base import Animation, DeviceError, exclusive


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


def _driver(a):
    found = devices.screens()
    if not found:
        raise DeviceError("no supported Epomaker keyboard found. Connect it with a USB cable; see `epomakerctl list`.")
    if not a.device:
        return found[0]
    for d in found:
        if _matches(d, a.device):
            return d
    raise DeviceError(f"--device {a.device}: not connected. Found: {', '.join(d.name for d in found)}")


def _matches(driver, key: str) -> bool:
    from .models import by_usb_id

    vid, pid = (driver.model.vid, driver.model.pid) if driver.model else (driver.vid, driver.pid)
    keys = {driver.model.key} if driver.model else {m.key for m in by_usb_id(vid, pid)}
    return key in keys or key in (f"{vid:04x}:{pid:04x}", driver.usb_path)


@contextlib.contextmanager
def _session(a) -> Iterator:
    driver = _driver(a)
    with exclusive():
        s = driver.open(log=log)
        if getattr(a, "chunk_delay", None) is not None and hasattr(s, "chunk_delay"):
            s.chunk_delay = a.chunk_delay / 1000
        try:
            yield driver, s
        except DeviceError:
            s.recover()
            raise
        finally:
            s.close()


def _geometry(driver):
    from .render import Geometry

    if driver.caps is None:  # TFT keyboard not identified yet
        with exclusive():
            driver.open(log=log).close()
    return Geometry.of(driver.caps)


def _data_hub(layout: dict, warmup: float = 0.0):
    from .sources import DataHub

    hub = DataHub()
    hub.configure(layout)
    hub.start()
    if warmup:
        time.sleep(warmup)  # let the first samples arrive
    return hub


# -- inspection ---------------------------------------------------------------


def _collections(d: hid.HidInfo) -> str:
    return ", ".join(f"{c.usage_page:04X}:{c.usage:04X}" for c in d.descriptor.collections) or "-"


def cmd_list(a) -> int:
    found = devices.detect()
    if not found:
        print("No Epomaker keyboard found on USB (vendor 36b0 or 3151). Connect it with a cable.")
        return 1
    for f in found:
        driver = f.driver
        status = driver.name if driver else "no supported screen"
        if driver and driver.model and not driver.model.verified:
            status += " (not yet verified on hardware)"
        print(f"{f.usb_id}  usb {f.usb_path}  {f.name}: {status}")
        for n in f.nodes:
            ok = os.access(n.node, os.R_OK | os.W_OK)
            sizes = "/".join(str(n.descriptor.size(k) or 0) for k in ("input", "output", "feature"))
            print(
                f"    {n.node}  if={n.interface}  collections={_collections(n)}  in/out/feature={sizes}  access={'yes' if ok else 'NO'}"
            )
    return 0


def cmd_probe(a) -> int:
    """Read-only report to paste into an issue; --identify also asks TFT keyboards who they are."""
    print(f"epomaker-screen {__version__}")
    print(f"kernel {os.uname().release}")
    for f in devices.detect():
        print(f"\n== {f.usb_id} usb {f.usb_path} '{f.name}' driver={f.driver.family if f.driver else None}")
        for n in f.nodes:
            raw = Path(f"/sys/class/hidraw/{Path(n.node).name}/device/report_descriptor")
            try:
                desc = raw.read_bytes()
            except OSError:
                desc = b""
            print(f"  {n.node} if={n.interface} access={'yes' if os.access(n.node, os.R_OK | os.W_OK) else 'NO'}")
            print(f"    collections: {_collections(n)}")
            print(f"    reports in={n.descriptor.input} out={n.descriptor.output} feature={n.descriptor.feature}")
            print(f"    descriptor ({len(desc)} bytes): {desc.hex()}")
        if a.identify and f.driver is not None and f.driver.family == "tft":
            try:
                with exclusive():
                    s = f.driver.open(log=log)
                    try:
                        print(f"  identity: {s.identify()}  model: {f.driver.name}")
                    finally:
                        s.close()
            except (DeviceError, OSError, PermissionError) as e:
                print(f"  identity: failed: {e}")
    return 0


def cmd_info(a) -> int:
    with _session(a) as (driver, s):
        print(f"{driver.name}: {s.info()}")
        caps = driver.caps
        print(
            f"  screen {caps.width}x{caps.height}, {caps.slots} picture slots, up to {caps.max_frames} frames, "
            f"clock={caps.clock}, firmware system page={caps.system_info}, live={caps.live}, verified={caps.verified}"
        )
    return 0


def cmd_sync(a) -> int:
    with _session(a) as (_d, s):
        s.sync_time()
    log("clock set")
    return 0


# -- pictures -----------------------------------------------------------------


def _encode_files(paths: list[str], fit: str, fps: int | None, max_frames: int, geometry) -> list[Animation]:
    _qt_app()
    from .render import load_frames, to_rgb

    out = []
    for p in paths:
        frames, file_fps = load_frames(p, fit, max_frames, geometry)
        item = Animation([to_rgb(f, geometry) for f in frames], fps or file_fps)
        log(f"{p}: {len(frames)} frame(s), {item.fps} fps")
        out.append(item)
    return out


def _progress():
    last = [0.0]

    def progress(done: int, total: int) -> None:
        if time.monotonic() - last[0] > 0.5 or done == total:
            last[0] = time.monotonic()
            print(f"\r  uploading {done * 100 // max(1, total)}%", end="", flush=True)

    return progress


def cmd_upload(a) -> int:
    driver = _driver(a)
    geometry = _geometry(driver)
    caps = driver.caps
    if len(a.files) > caps.slots:
        log(f"{driver.name} stores up to {caps.slots} pictures")
        return 2
    max_frames = caps.max_frames if a.max_frames is None else a.max_frames
    items = _encode_files(a.files, a.fit, a.fps, max_frames, geometry)
    with _session(a) as (driver, s):
        t0 = time.monotonic()
        if a.bank is not None and driver.family == "tft":
            if len(items) != 1 or len(items[0].frames) != 1:
                raise ValueError("--bank takes one still picture")
            s.upload_still(items[0].frames[0], a.bank - 1, progress=_progress())
        else:
            s.upload(items, progress=_progress())
        print()
    log(f"done in {time.monotonic() - t0:.1f} s")
    return 0


def _layout(a, geometry) -> dict:
    from .config import load_layout

    return load_layout(Path(a.file) if a.file else None, geometry)


def cmd_preview(a) -> int:
    _qt_app()
    from .render import RT82, Geometry
    from .widgets import Context, render_layout

    geometry = Geometry(*map(int, a.size.split("x"))) if a.size else RT82
    layout = _layout(a, geometry)
    hub = _data_hub(layout, warmup=1.5)
    render_layout(layout, Context(time.time(), hub.snapshot()), geometry=geometry).save(a.output)
    log(f"saved {a.output}")
    return 0


def cmd_layout(a) -> int:
    _qt_app()
    from .render import to_rgb
    from .widgets import Context, render_layout

    driver = _driver(a)
    geometry = _geometry(driver)
    layout = _layout(a, geometry)
    hub = _data_hub(layout, warmup=1.5)
    frame = to_rgb(render_layout(layout, Context(time.time(), hub.snapshot()), geometry=geometry), geometry)
    with _session(a) as (_d, s):
        s.upload([Animation([frame], 1)], progress=_progress())
        print()
    log("done")
    return 0


def _install_stop() -> threading.Event:
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())
    return stop


def cmd_live(a) -> int:
    _qt_app()
    from .config import Settings
    from .widgets import renderer

    settings = Settings.load()
    driver = _driver(a)
    geometry = _geometry(driver)
    layout = _layout(a, geometry)
    hub = _data_hub(layout)
    stop = _install_stop()
    render, fallback = renderer(lambda: layout, hub.snapshot, geometry)
    config = settings.live_config if driver.family == "rdm" else settings.refresh_config
    if a.fps and driver.family == "rdm":
        config.fps = a.fps
    if a.interval and driver.family == "tft":
        config.interval_s = max(10.0, a.interval)
    with _session(a) as (_d, s):
        live = s.live(render, fallback, config)
        live.start()
        log("live mode: press Ctrl+C to stop")
        live.run_for(stop)
        live.stop()
    return 0


def cmd_sysinfo(a) -> int:
    from .sources import FirmwareSystemInfo

    provider = FirmwareSystemInfo()
    provider()  # first CPU sample
    stop = _install_stop()
    with _session(a) as (driver, s):
        if not driver.caps.system_info:
            raise DeviceError(f"{driver.name} has no firmware system page")
        while True:
            time.sleep(1.0)
            info = provider()
            s.system_info(info)
            log(
                f"sent: cpu {info.cpu_percent}% temp {info.cpu_temperature} mem {info.memory_used >> 30}/{info.memory_total >> 30} GiB"
            )
            if not a.watch or stop.wait(1.0):
                break
    return 0


def cmd_language(a) -> int:
    with _session(a) as (driver, s):
        if driver.family != "tft":
            raise DeviceError(f"{driver.name} has no language setting")
        s.toggle_language()
    log("screen language toggled")
    return 0


def cmd_watch(a) -> int:
    from .config import Settings
    from .service import Controller
    from .sources import FirmwareSystemInfo

    settings = Settings.load()
    stop = _install_stop()
    c = Controller(log=log, auto_sync=True, resync_hours=settings.resync_hours, prefer=a.device or settings.device)
    if settings.system_info:
        c.set_system_info(FirmwareSystemInfo())
    if a.live:
        _qt_app()
        from .widgets import renderer

        def factory(geometry):
            layout = _layout(a, geometry)
            hub = _data_hub(layout)
            return renderer(lambda: layout, hub.snapshot, geometry)

        c.set_live(True, factory, {"rdm": settings.live_config, "tft": settings.refresh_config})
    c.start()
    log("watching for keyboards (clocks are set automatically)")
    stop.wait()
    c.shutdown()
    return 0


def cmd_lab(a) -> int:
    from . import lab

    return lab.main(a, _session, log)


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="epomakerctl", description="Control Epomaker keyboard screens without a browser.")
    ap.add_argument("--version", action="version", version=__version__)
    ap.add_argument("--device", help="model key (rt82, rt100pro, ...) or USB id (3151:5002) when several are connected")
    ap.add_argument(
        "--chunk-delay",
        type=float,
        metavar="MS",
        help="TFT keyboards: milliseconds between pixel packets (default 2, the vendor's; lower can corrupt pictures)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list", help="Epomaker keyboards, their interfaces and access rights").set_defaults(fn=cmd_list)
    p = sub.add_parser("probe", help="detailed read-only report for bug reports")
    p.add_argument(
        "--identify", action="store_true", help="also ask TFT keyboards for their model (one harmless command)"
    )
    p.set_defaults(fn=cmd_probe)
    sub.add_parser("info", help="screen parameters of the connected keyboard").set_defaults(fn=cmd_info)
    sub.add_parser("sync-time", help="set the keyboard clock").set_defaults(fn=cmd_sync)
    up = sub.add_parser("upload", help="upload pictures and animations (PNG, JPEG, GIF, WebP)")
    up.add_argument("files", nargs="+")
    up.add_argument("--fit", choices=["contain", "cover", "stretch"], nargs="?", const="contain", default="contain")
    up.add_argument("--fps", type=int, help="override the frame rate")
    up.add_argument("--bank", type=int, help="TFT keyboards: write one still into bank 1..5 and keep the others")
    up.add_argument(
        "--max-frames", type=int, help="thin out longer animations (default: what the keyboard allows; 0 = keep all)"
    )
    up.set_defaults(fn=cmd_upload)
    p = sub.add_parser("sysinfo", help="TFT keyboards: send CPU/RAM/disk values to the keyboard's own system page")
    p.add_argument("--watch", action="store_true", help="keep sending every 2 s")
    p.set_defaults(fn=cmd_sysinfo)
    sub.add_parser("language", help="TFT keyboards: toggle the screen language").set_defaults(fn=cmd_language)
    for name, fn, hlp in (
        ("layout", cmd_layout, "render the widget layout once and upload it as a picture"),
        ("live", cmd_live, "live mode: widgets update continuously (Ctrl+C to stop)"),
        ("preview", cmd_preview, "save the layout as a PNG"),
        ("watch", cmd_watch, "background service: set clocks (and system pages) whenever keyboards connect"),
    ):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("--file", help="layout file (default: the saved layout for the keyboard's screen size)")
        if name == "preview":
            p.add_argument("output", nargs="?", default="epomaker-preview.png")
            p.add_argument("--size", help="screen size WxH (default 240x135)")
        if name == "live":
            p.add_argument("--fps", type=int, help="RT82 family: frames per second")
            p.add_argument("--interval", type=float, help="TFT family: seconds between picture updates (min 10)")
        if name == "watch":
            p.add_argument("--live", action="store_true", help="also keep live mode running")
        p.set_defaults(fn=fn)
    from . import lab

    lab.add_parser(sub).set_defaults(fn=cmd_lab)
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    try:
        return a.fn(a)
    except (DeviceError, PermissionError, ValueError) as e:
        log(f"error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
