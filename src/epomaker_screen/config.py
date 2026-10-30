"""Settings and layout files (~/.config/epomaker-screen)."""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .drivers.rdm.live import LiveConfig
from .drivers.tft import RefreshConfig

APP = "epomaker-screen"
OLD_APP = "rt82-plasma"  # before 0.3


def config_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    d = base / APP
    if not d.exists() and (base / OLD_APP).is_dir():
        _migrate(base / OLD_APP, d)
    d.mkdir(parents=True, exist_ok=True)
    return d


def _migrate(old: Path, new: Path) -> None:
    """0.2 kept one RT82 layout in ~/.config/rt82-plasma; carry it over."""
    new.mkdir(parents=True, exist_ok=True)
    if (old / "settings.json").is_file():
        shutil.copy2(old / "settings.json", new / "settings.json")
    if (old / "layout.json").is_file():
        shutil.copy2(old / "layout.json", new / "layout-240x135.json")


def _load_into(obj, raw: dict) -> None:
    for f in fields(obj):
        if f.name in raw and not isinstance(getattr(obj, f.name), (LiveConfig, RefreshConfig)):
            try:
                setattr(obj, f.name, type(getattr(obj, f.name))(raw[f.name]))
            except (TypeError, ValueError):
                pass


@dataclass
class Settings:
    auto_sync_time: bool = True
    resync_hours: float = 6.0
    tray_on_close: bool = True  # closing the window keeps the app running in the system tray
    limit_frames: bool = True  # thin out animations to what the vendor software allows
    device: str = ""  # preferred keyboard: model key, or "" for the first one found
    live: bool = False
    live_config: LiveConfig = field(default_factory=LiveConfig)  # rdm family: streamed frames
    refresh_config: RefreshConfig = field(default_factory=RefreshConfig)  # tft family: rewritten pictures
    system_info: bool = True  # tft family: keep the keyboard's own system page up to date

    @classmethod
    def load(cls) -> Settings:
        s = cls()
        try:
            raw = json.loads((config_dir() / "settings.json").read_text())
        except (OSError, ValueError):
            return s
        _load_into(s, raw)
        _load_into(s.live_config, raw.get("live_config", {}))
        _load_into(s.refresh_config, raw.get("refresh_config", {}))
        try:
            s.live_config.check()
        except ValueError:
            s.live_config = LiveConfig()
        s.refresh_config.interval_s = max(10.0, s.refresh_config.interval_s)
        return s

    def save(self) -> None:
        _write(config_dir() / "settings.json", asdict(self))


def _write(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)


def layout_path(geometry_key: str = "240x135") -> Path:
    return config_dir() / f"layout-{geometry_key}.json"


def load_layout(path: Path | None = None, geometry=None) -> dict:
    """The saved layout for a screen size (or ``path``), or a default one scaled to it."""
    from .render import RT82  # noqa: PLC0415 (needs Qt; config itself does not)
    from .widgets import default_layout  # noqa: PLC0415

    geometry = geometry or RT82
    try:
        data = json.loads((path or layout_path(geometry.key)).read_text())
        if isinstance(data, dict) and isinstance(data.get("widgets"), list):
            return data
    except (OSError, ValueError):
        pass
    return default_layout(geometry)


def save_layout(layout: dict, path: Path | None = None, geometry_key: str = "240x135") -> None:
    _write(path or layout_path(geometry_key), layout)
