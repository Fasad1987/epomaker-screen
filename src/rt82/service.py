"""Background controller: hotplug, time sync, live mode and upload jobs.

Everything that talks to the keyboard runs on one worker thread, so the
screen controller never sees two conversations at once.
"""

from __future__ import annotations

import queue
import threading
import time
import traceback
from dataclasses import dataclass
from typing import Callable, Optional

from .device import POWER_CYCLE_HINT, DeviceError, Keyboard, Screen, ScreenFrozen, exclusive, keyboard_id
from .live import LiveConfig, LiveStreamer

Log = Callable[[str], None]
Done = Callable[[Optional[Exception], object], None]
RenderFactory = Callable[[], tuple[Callable[[float], bytes], bytes]]  # -> (render, fallback frame)

LIVE_RETRY_S = 30.0  # back-off after live mode failed

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
    run: Callable[[Screen], object]
    done: Done


class Controller:
    def __init__(
        self,
        log: Log = print,
        on_state: Callable[[str], None] = lambda s: None,
        auto_sync: bool = True,
        resync_hours: float = 6.0,
    ):
        self.log = log
        self.on_state = on_state
        self.auto_sync = auto_sync
        self.resync_s = max(0.25, resync_hours) * 3600
        self.paused = False
        self.frozen = False  # until the keyboard is unplugged or re-enumerates
        self._jobs: queue.Queue[Job] = queue.Queue()
        self._stop = threading.Event()
        self._live_stop = threading.Event()
        self._live_wanted = False
        self._live_factory: RenderFactory | None = None
        self._live_config = LiveConfig()
        self._live_poke = threading.Event()  # wakes the live loop: stop request or inline time sync
        self._live_running = False
        self._sync_waiters: list[Done] = []
        self._sync_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self.state = NO_KEYBOARD
        self._last_live_fail = 0.0

    # -- public API (any thread) ----------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._loop, name="rt82-device", daemon=True)
            self._thread.start()

    def _stop_live(self) -> None:
        self._live_stop.set()
        self._live_poke.set()

    def shutdown(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._stop_live()
        if self._thread:
            self._thread.join(timeout)

    def submit(self, name: str, run: Callable[[Screen], object], done: Done = lambda e, r: None) -> None:
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
        self.submit("time sync", lambda s: s.sync_time(), done)

    def set_live(self, enabled: bool, factory: RenderFactory | None = None, config: LiveConfig | None = None) -> None:
        if factory is not None:
            self._live_factory = factory
        if config is not None:
            config.check()
            self._live_config = config
        self._live_wanted = enabled
        self._last_live_fail = 0.0
        self._stop_live()

    def set_paused(self, paused: bool) -> None:
        """Releases the keyboard entirely (e.g. to use Epomaker Hub in a browser)."""
        self.paused = paused
        self._stop_live()
        self._set_state(PAUSED if paused else self.state)

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

    def _loop(self) -> None:
        connection: str | None = None
        last_sync = 0.0
        last_wall, last_mono = time.time(), time.monotonic()
        self._set_state(self.state)
        while not self._stop.is_set():
            # A jump of wall time against monotonic time means suspend/resume or a clock change.
            wall, mono = time.time(), time.monotonic()
            jumped = abs((wall - last_wall) - (mono - last_mono)) > 5
            last_wall, last_mono = wall, mono

            now = keyboard_id()
            if now != connection:
                self.log(
                    "keyboard reconnected"
                    if connection and now
                    else "keyboard connected"
                    if now
                    else "keyboard disconnected"
                )
                connection = now
                present = now is not None
                self.frozen = False  # a re-plug or power cycle gives the controller a fresh start
                self._set_state(CONNECTED if present else NO_KEYBOARD)
                if present:
                    time.sleep(1.0)  # let it finish enumerating
                    last_sync = 0.0

            present = connection is not None
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
                    with exclusive(wait=2.0):
                        s = Screen.connect(log=self.log)
                        try:
                            s.sync_time()
                        finally:
                            s.close()
                    self.log("keyboard clock synchronised")
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

            self._stop.wait(2.0)

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
                with exclusive(wait=5.0):
                    s = Screen.connect(log=self.log)
                    try:
                        result = job.run(s)
                    except DeviceError:
                        s.recover()  # close what the failed sequence left open
                        raise
                    finally:
                        s.close()
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
            render, fallback = self._live_factory()
            with exclusive(wait=2.0):
                s = Screen.connect(log=self.log)
                try:
                    kb = Keyboard.open()
                    try:
                        streamer = LiveStreamer(s, render, fallback, self._live_config, log=self.log)
                        streamer.start(kb)
                    finally:
                        kb.close()
                    self._set_state(LIVE)
                    with self._sync_lock:
                        self._live_running = True
                    last_sync = time.monotonic()
                    while not self._live_stop.is_set() and not self._stop.is_set():
                        streamer.run_for(self._live_poke, 60.0)
                        with self._sync_lock:
                            waiters, self._sync_waiters = self._sync_waiters, []
                            if not self._live_stop.is_set():
                                self._live_poke.clear()
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
                        self.submit("time sync", lambda s: s.sync_time(), done)
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
