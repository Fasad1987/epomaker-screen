"""Data sources for widgets. Polled in a background thread; read as snapshots."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from typing import Any, Optional

HISTORY = 60


def _read(path: str) -> str:
    with open(path) as f:
        return f.read()


class SystemStats:
    def __init__(self):
        self._cpu_prev: Optional[tuple[int, int]] = None
        self._net_prev: Optional[tuple[float, int, int]] = None
        self.history: dict[str, deque] = {}

    def _cpu(self) -> Optional[float]:
        fields = [int(x) for x in _read("/proc/stat").split("\n", 1)[0].split()[1:]]
        idle, total = fields[3] + (fields[4] if len(fields) > 4 else 0), sum(fields)
        prev, self._cpu_prev = self._cpu_prev, (idle, total)
        if not prev or total == prev[1]:
            return None
        return 100.0 * (1 - (idle - prev[0]) / (total - prev[1]))

    @staticmethod
    def _mem() -> dict[str, float]:
        info = {}
        for line in _read("/proc/meminfo").splitlines():
            k, v = line.split(":", 1)
            info[k] = int(v.split()[0])
        ram = 100.0 * (1 - info.get("MemAvailable", 0) / max(1, info["MemTotal"]))
        swap_total = info.get("SwapTotal", 0)
        swap = 100.0 * (1 - info.get("SwapFree", 0) / swap_total) if swap_total else 0.0
        return {"ram": ram, "swap": swap, "ram_used_gb": (info["MemTotal"] - info.get("MemAvailable", 0)) / 1048576}

    @staticmethod
    def _temp() -> Optional[float]:
        # Prefer CPU package sensors, then any thermal zone.
        best = None
        base = "/sys/class/hwmon"
        try:
            for hw in os.listdir(base):
                name = _read(f"{base}/{hw}/name").strip()
                if name in ("coretemp", "k10temp", "zenpower", "cpu_thermal", "acpitz"):
                    for f in sorted(os.listdir(f"{base}/{hw}")):
                        if f.startswith("temp") and f.endswith("_input"):
                            v = int(_read(f"{base}/{hw}/{f}")) / 1000
                            if name != "acpitz":
                                return v
                            best = best or v
        except OSError:
            pass
        if best is None:
            try:
                best = int(_read("/sys/class/thermal/thermal_zone0/temp")) / 1000
            except OSError:
                pass
        return best

    def _net(self) -> dict[str, float]:
        rx = tx = 0
        for line in _read("/proc/net/dev").splitlines()[2:]:
            name, rest = line.split(":", 1)
            if name.strip() == "lo":
                continue
            v = rest.split()
            rx, tx = rx + int(v[0]), tx + int(v[8])
        now = time.monotonic()
        prev, self._net_prev = self._net_prev, (now, rx, tx)
        if not prev or now <= prev[0]:
            return {}
        dt = now - prev[0]
        return {"net_down": (rx - prev[1]) / dt, "net_up": (tx - prev[2]) / dt}

    @staticmethod
    def _disk() -> float:
        st = os.statvfs("/")
        return 100.0 * (1 - st.f_bavail / max(1, st.f_blocks))

    def sample(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for fn in (lambda: {"cpu": self._cpu()}, self._mem, lambda: {"temp": self._temp()}, self._net, lambda: {"disk": self._disk()}):
            try:
                out.update({k: v for k, v in fn().items() if v is not None})
            except (OSError, ValueError, KeyError, IndexError):
                pass
        for k, v in out.items():
            self.history.setdefault(k, deque(maxlen=HISTORY)).append(v)
        out["history"] = {k: list(v) for k, v in self.history.items()}
        return out


_MPRIS_TITLE = re.compile(r"'xesam:title': <'((?:[^'\\]|\\.)*)'>")
_MPRIS_ARTIST = re.compile(r"'xesam:artist': <\['((?:[^'\\]|\\.)*)'")


def now_playing() -> dict[str, str]:
    """Current MPRIS track (playerctl if installed, otherwise gdbus)."""
    if shutil.which("playerctl"):
        try:
            out = subprocess.run(
                ["playerctl", "metadata", "--format", "{{status}}\t{{artist}}\t{{title}}"],
                capture_output=True, text=True, timeout=2,
            ).stdout.strip()
            if out:
                status, artist, title = (out.split("\t") + ["", "", ""])[:3]
                return {"status": status, "artist": artist, "title": title}
        except (OSError, subprocess.SubprocessError):
            pass
        return {}
    if not shutil.which("gdbus"):
        return {}
    try:
        names = subprocess.run(
            ["gdbus", "call", "--session", "--dest", "org.freedesktop.DBus", "--object-path", "/",
             "--method", "org.freedesktop.DBus.ListNames"],
            capture_output=True, text=True, timeout=2,
        ).stdout
        players = re.findall(r"'(org\.mpris\.MediaPlayer2\.[^']+)'", names)
        fallback = {}
        for name in players:
            def get(prop: str) -> str:
                return subprocess.run(
                    ["gdbus", "call", "--session", "--dest", name, "--object-path", "/org/mpris/MediaPlayer2",
                     "--method", "org.freedesktop.DBus.Properties.Get", "org.mpris.MediaPlayer2.Player", prop],
                    capture_output=True, text=True, timeout=2,
                ).stdout

            meta, status = get("Metadata"), get("PlaybackStatus")
            t, a = _MPRIS_TITLE.search(meta), _MPRIS_ARTIST.search(meta)
            info = {
                "status": "Playing" if "Playing" in status else "Paused",
                "title": t.group(1) if t else "",
                "artist": a.group(1) if a else "",
            }
            if info["status"] == "Playing":
                return info
            fallback = fallback or info
        return fallback
    except (OSError, subprocess.SubprocessError):
        return {}


WEATHER_CODES = {
    0: "Ясно", 1: "Малооблачно", 2: "Облачно", 3: "Пасмурно", 45: "Туман", 48: "Изморозь",
    51: "Морось", 53: "Морось", 55: "Морось", 61: "Дождь", 63: "Дождь", 65: "Ливень",
    66: "Ледяной дождь", 67: "Ледяной дождь", 71: "Снег", 73: "Снег", 75: "Снегопад", 77: "Снежная крупа",
    80: "Ливень", 81: "Ливень", 82: "Ливень", 85: "Снег", 86: "Снегопад", 95: "Гроза", 96: "Гроза", 99: "Гроза",
}


def weather(lat: float, lon: float) -> dict[str, Any]:
    """Current weather from Open-Meteo (no API key)."""
    q = urllib.parse.urlencode({"latitude": lat, "longitude": lon, "current": "temperature_2m,weather_code,wind_speed_10m"})
    with urllib.request.urlopen(f"https://api.open-meteo.com/v1/forecast?{q}", timeout=10) as r:
        cur = json.load(r)["current"]
    code = int(cur.get("weather_code", -1))
    return {"temp": cur.get("temperature_2m"), "wind": cur.get("wind_speed_10m"), "code": code, "text": WEATHER_CODES.get(code, "")}


class DataHub:
    """Keeps the data the current layout needs fresh, in a daemon thread."""

    def __init__(self):
        self._lock = threading.Lock()
        self._snap: dict[str, Any] = {}
        self._stats = SystemStats()
        self._commands: dict[str, float] = {}  # command -> interval
        self._places: set[tuple[float, float]] = set()
        self._want_media = False
        self._due: dict[str, float] = {}
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def configure(self, layout: dict) -> None:
        cmds, places, media = {}, set(), False
        for w in layout.get("widgets", []):
            p = w.get("props", {})
            if w.get("type") == "command" and p.get("command"):
                cmds[p["command"]] = max(1.0, float(p.get("interval", 10)))
            elif w.get("type") == "weather":
                try:
                    places.add((round(float(p.get("lat", 55.75)), 3), round(float(p.get("lon", 37.62)), 3)))
                except (TypeError, ValueError):
                    pass
            elif w.get("type") == "media":
                media = True
        with self._lock:
            self._commands, self._places, self._want_media = cmds, places, media
            self._due.clear()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._snap)

    def _set(self, key: str, value: Any) -> None:
        with self._lock:
            self._snap[key] = value

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="rt82-data", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _due_now(self, key: str, interval: float) -> bool:
        now = time.monotonic()
        if now >= self._due.get(key, 0):
            self._due[key] = now + interval
            return True
        return False

    def _run(self) -> None:
        while not self._stop.is_set():
            self._set("sys", self._stats.sample())
            with self._lock:
                cmds, places, media = dict(self._commands), set(self._places), self._want_media
            if media and self._due_now("media", 2):
                self._set("media", now_playing())
            for cmd, interval in cmds.items():
                if self._due_now("cmd:" + cmd, interval):
                    self._set("cmd:" + cmd, self._run_command(cmd))
            for lat, lon in places:
                key = f"weather:{lat},{lon}"
                if self._due_now(key, 900):
                    try:
                        self._set(key, weather(lat, lon))
                    except Exception as e:  # network errors of every kind
                        self._due[key] = time.monotonic() + 120
                        self._set(key, {"error": str(e)})
            self._stop.wait(1.0)

    @staticmethod
    def _run_command(cmd: str) -> str:
        try:
            r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=10)
            return (r.stdout or r.stderr).strip()
        except subprocess.TimeoutExpired:
            return "таймаут"
        except OSError as e:
            return str(e)
