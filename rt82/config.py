"""Settings and layout files (~/.config/rt82-plasma)."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

from .live import LiveConfig


def config_dir() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    d = base / "rt82-plasma"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class Settings:
    auto_sync_time: bool = True
    resync_hours: float = 6.0
    live: bool = False
    live_config: LiveConfig = field(default_factory=LiveConfig)

    @classmethod
    def load(cls) -> "Settings":
        try:
            raw = json.loads((config_dir() / "settings.json").read_text())
        except (OSError, ValueError):
            return cls()
        s = cls()
        for f in fields(cls):
            if f.name in raw and f.name != "live_config":
                setattr(s, f.name, type(getattr(s, f.name))(raw[f.name]))
        lc = raw.get("live_config", {})
        for f in fields(LiveConfig):
            if f.name in lc:
                setattr(s.live_config, f.name, type(getattr(s.live_config, f.name))(lc[f.name]))
        try:
            s.live_config.check()
        except ValueError:
            s.live_config = LiveConfig()
        return s

    def save(self) -> None:
        _write(config_dir() / "settings.json", asdict(self))


def _write(path: Path, data) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    tmp.replace(path)


def layout_path() -> Path:
    return config_dir() / "layout.json"


def load_layout(path: Path | None = None) -> dict:
    from .widgets import default_layout

    try:
        data = json.loads((path or layout_path()).read_text())
        if isinstance(data, dict) and isinstance(data.get("widgets"), list):
            return data
    except (OSError, ValueError):
        pass
    return default_layout()


def save_layout(layout: dict, path: Path | None = None) -> None:
    _write(path or layout_path(), layout)
