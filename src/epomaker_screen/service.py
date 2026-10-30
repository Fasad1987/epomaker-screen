"""Background controller: hotplug, clock sync, live mode, system info and jobs.

Everything that talks to a keyboard runs on one worker thread, so a screen
controller never sees two conversations at once.
"""

from __future__ import annotations

import queue
import threading
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Optional

from . import devices
from .drivers.base import POWER_CYCLE_HINT, DeviceError, ScreenFrozen, SystemInfo, exclusive

Log = Callable[[str], None]
Done = Callable[[Optional[Exception], object], None]
# geometry -> (render, fallback frame); see widgets.renderer
RenderFactory = Callable[[Any], tuple[Callable[[float], bytes], bytes]]

LIVE_RETRY_S = 30.0  # back-off after live mode failed
SYSTEM_INFO_S = 2.0  # how often the keyboard's own system page is refreshed

# States reported through on_state.
NO_KEYBOARD = "no keyboard"
CONNECTED = "connected"
LIVE = "live mode"
LIVE_STARTING = "live mode: starting…"
PAUSED = "paused: keyboard released"
FROZEN = "screen not responding: power-cycle the keyboard"


@dataclass
class Job:
    name: str
    run: Callable[[Any], object]  # gets an open driver session
    done: Done


def pick(found: list, prefer: str = ""):
    """The preferred keyboard (by model key or USB id), else the first one."""
    for d in found:
        model = getattr(d, "model", None)
        usb = f"{model.vid:04x}:{model.pid:04x}" if model else f"{d.vid:04x}:{d.pid:04x}"
        if prefer and prefer in (getattr(model, "key", None), usb):
            return d
    return found[0] if found else None


class Controller:
    def __init__(
        self,
        log: Log = print,
        on_state: Callable[[str], None] = lambda s: None,
        auto_sync: bool = True,
        resync_hours: float = 6.0,
        on_device: Callable[[Any], None] = lambda driver: None,
        find: Callable[[], list] = devices.screens,
        prefer: str = "",
    ):
        self.log = log
        self.on_state = on_state
        self.on_device = on_device
        self.find = find
        self.prefer = prefer
        self.auto_sync = auto_sync
        self.resync_s = max(0.25, resync_hours) * 3600
        self.paused = False
        self.frozen = False  # until the keyboard is unplugged or re-enumerates
        self.driver = None
        self._jobs: queue.Queue[Job] = queue.Queue()
        self._stop = threading.Event()
        self._live_stop = threading.Event()
        self._live_wanted = False
        self._live_factory: RenderFactory | None = None
        self._live_configs: dict[str, Any] = {}
        self._live_poke = threading.Event()  # wakes the live loop: stop request or inline clock sync
        self._live_running = False
        self._sync_waiters: list[Done] = []
        self._sync_lock = threading.Lock()
        self._system_info: Callable[[], SystemInfo] | None = None
        self._thread: threading.Thread | None = None
        self.state = NO_KEYBOARD
        self._last_live_fail = 0.0
        self._model_seen = None

    # -- public API (any thread) ----------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="epomaker-device", daemon=True)
            self._thread.start()

    def _stop_live(self) -> None:
        self._live_stop.set()
        self._live_poke.set()

    def shutdown(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._stop_live()
        if self._thread:
            self._thread.join(timeout)

    def submit(self, name: str, run: Callable[[Any], object], done: Done = lambda e, r: None) -> None:
        self.frozen = False  # an explicit request is worth one more try
        self._jobs.put(Job(name, run, done))
        self._stop_live()  # live mode yields to jobs and resumes afterwards

    def sync_now(self, done: Done = lambda e, r: None) -> None:
        with self._sync_lock:
            if self._live_running:
                # Done between frames, without restarting live mode.
                self._sync_waiters.append(done)
                self._live_poke.set()
                return
        self.submit("clock sync", lambda s: s.sync_time(), done)

    def set_live(self, enabled: bool, factory: RenderFactory | None = None, configs: dict | None = None) -> None:
        """``configs`` maps a driver family ("rdm", "tft") to its live-mode config."""
        if factory is not None:
            self._live_factory = factory
        if configs is not None:
            self._live_configs = dict(configs)
        self._live_wanted = enabled
        self._last_live_fail = 0.0
        self._stop_live()

    def set_system_info(self, provider: Callable[[], SystemInfo] | None) -> None:
        """Keeps keyboards whose firmware draws a system page (TFT family) fed with values."""
        self._system_info = provider

    def set_paused(self, paused: bool) -> None:
        """Releases the keyboard entirely (e.g. to use Epomaker Hub in a browser)."""
        self.paused = paused
        self._stop_live()
        self._set_state(PAUSED if paused else self.state)

    def set_prefer(self, prefer: str) -> None:
        self.prefer = prefer
        self._stop_live()

    # -- worker -----------------------------------------------------------

    def _set_state(self, s: str) -> None:
        if not self.paused or s == PAUSED:
            self.state = s
        self.on_state(s)

    def _mark_frozen(self, e: Exception) -> None:
        if not self.frozen:
            self.frozen = True
            self.log(f"{e}")
        self._set_state(FROZEN)

    def _open(self):
        session = self.driver.open(log=self.log)
        if getattr(self.driver, "model", None) is not self._model_seen:
            self._model_seen = self.driver.model  # e.g. a TFT keyboard was just identified
            self.on_device(self.driver)
        return session

    def _with_session(self, fn, wait: float = 2.0):
        with exclusive(wait=wait):
            s = self._open()
            try:
                return fn(s)
            except DeviceError:
                s.recover()  # close what a failed sequence left open
                raise
            finally:
                s.close()

    def _loop(self) -> None:
        connection: str | None = None
        last_sync = 0.0
        last_info = 0.0
        last_wall, last_mono = time.time(), time.monotonic()
        self._set_state(self.state)
        while not self._stop.is_set():
            # A jump of wall time against monotonic time means suspend/resume or a clock change.
            wall, mono = time.time(), time.monotonic()
            jumped = abs((wall - last_wall) - (mono - last_mono)) > 5
            last_wall, last_mono = wall, mono

            try:
                found = self.find()
            except OSError as e:
                self.log(f"could not list devices: {e}")
                found = []
            driver = pick(found, self.prefer)
            now = None
            if driver is not None:
                now = f"{driver.usb_path}@{driver.connection_id()}"
            if now != connection:
                if connection and now and connection.split("@")[0] == now.split("@")[0]:
                    self.log("keyboard reconnected")
                else:
                    self.log(f"connected: {driver.name}" if driver else "keyboard disconnected")
                connection = now
                self.driver = driver
                self._model_seen = getattr(driver, "model", None)
                self.on_device(driver)
                self.frozen = False  # a re-plug or power cycle gives a controller a fresh start
                self._set_state(CONNECTED if driver else NO_KEYBOARD)
                if driver:
                    time.sleep(1.0)  # let it finish enumerating
                    last_sync = 0.0

            present = self.driver is not None
            if self.paused or not present or self.frozen:
                if self.frozen:
                    reason: Exception = ScreenFrozen(f"the screen controller does not answer. {POWER_CYCLE_HINT}")
                else:
                    reason = DeviceError("paused" if self.paused else "keyboard not connected")
                self._drain_jobs(reason)
                self._stop.wait(2.0)
                continue

            if not self._jobs.empty():
                self._run_jobs()
                continue

            if self.auto_sync and (last_sync == 0.0 or jumped or mono - last_sync > self.resync_s):
                try:
                    self._with_session(lambda s: s.sync_time())
                    self.log("keyboard clock set")
                except ScreenFrozen as e:
                    self._mark_frozen(e)
                except (DeviceError, OSError) as e:
                    self.log(f"could not set the keyboard clock: {e}")
                last_sync = mono

            if (
                self._live_wanted
                and self._live_factory
                and not self.frozen
                and time.monotonic() - self._last_live_fail > LIVE_RETRY_S
            ):
                self._run_live()
                continue

            if self._wants_system_info() and time.monotonic() - last_info >= SYSTEM_INFO_S:
                last_info = time.monotonic()
                try:
                    self._with_session(lambda s: s.system_info(self._system_info()))
                except (DeviceError, OSError) as e:
                    self.log(f"could not send system info: {e}")
                self._stop.wait(SYSTEM_INFO_S)
                continue

            self._stop.wait(2.0)

    def _wants_system_info(self) -> bool:
        caps = getattr(self.driver, "caps", None)
        return bool(self._system_info and caps and caps.system_info)

    def _drain_jobs(self, err: Exception) -> None:
        while True:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                return
            job.done(err, None)

    def _run_jobs(self) -> None:
        while not self.frozen:
            try:
                job = self._jobs.get_nowait()
            except queue.Empty:
                return
            self._set_state(job.name + "…")
            try:
                result = self._with_session(job.run, wait=5.0)
            except ScreenFrozen as e:
                self._mark_frozen(e)
                job.done(e, None)
                continue
            except Exception as e:  # report every failure to the caller
                self.log(f"{job.name}: error: {e}")
                self._set_state(CONNECTED)
                job.done(e, None)
                continue
            self.log(f"{job.name}: done")
            self._set_state(CONNECTED)
            job.done(None, result)

    def _run_live(self) -> None:
        assert self._live_factory
        self._live_stop.clear()
        self._live_poke.clear()
        self._set_state(LIVE_STARTING)
        try:
            with exclusive(wait=2.0):
                s = self._open()
                try:
                    from .render import Geometry  # noqa: PLC0415 (Qt is only needed for live mode)

                    if self.driver.caps is None:
                        raise DeviceError(f"{self.driver.name}: unknown screen")
                    render, fallback = self._live_factory(Geometry.of(self.driver.caps))
                    config = self._live_configs.get(self.driver.family)
                    streamer = s.live(render, fallback, config) if config is not None else s.live(render, fallback)
                    streamer.start()
                    self._set_state(LIVE)
                    with self._sync_lock:
                        self._live_running = True
                    last_sync = time.monotonic()
                    tick = SYSTEM_INFO_S if self._wants_system_info() else 60.0
                    while not self._live_stop.is_set() and not self._stop.is_set():
                        streamer.run_for(self._live_poke, tick)
                        with self._sync_lock:
                            waiters, self._sync_waiters = self._sync_waiters, []
                            if not self._live_stop.is_set():
                                self._live_poke.clear()
                        if self._wants_system_info() and not self._live_stop.is_set():
                            s.system_info(self._system_info())
                        resync = self.auto_sync and time.monotonic() - last_sync > self.resync_s
                        if waiters or resync:
                            err = None
                            try:
                                s.sync_time()
                                last_sync = time.monotonic()
                            except DeviceError as e:
                                err = e
                            for done in waiters:
                                done(err, None)
                            if err:
                                raise err
                    streamer.stop()
                finally:
                    with self._sync_lock:
                        self._live_running = False
                        waiters, self._sync_waiters = self._sync_waiters, []
                    for done in waiters:  # handled as a normal job once live mode is gone
                        self.submit("clock sync", lambda s: s.sync_time(), done)
                    s.close()
            self._set_state(CONNECTED)
        except ScreenFrozen as e:
            self._mark_frozen(e)
        except Exception as e:  # keep the controller alive whatever happened
            self._last_live_fail = time.monotonic()
            self.log(f"live mode stopped: {e}")
            if not isinstance(e, (DeviceError, OSError)):
                self.log(traceback.format_exc())
            self._set_state(f"live mode failed, retrying in {int(LIVE_RETRY_S)} s")
