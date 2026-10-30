import json
import os
from types import SimpleNamespace

import pytest

from epomaker_screen.drivers import base
from epomaker_screen.drivers.base import DeviceError

pytest.importorskip("epomaker_screen.qt")

from epomaker_screen import cli  # noqa: E402
from epomaker_screen.render import RT82  # noqa: E402
from epomaker_screen.widgets import commands_in, new_widget  # noqa: E402


def layout_with_command(tmp_path, command="echo hi"):
    w = new_widget("command")
    w["props"] = {"command": command}
    path = tmp_path / "layout.json"
    path.write_text(json.dumps({"background": "#000000", "widgets": [w]}))
    return path


def test_commands_in_lists_shell_commands(tmp_path):
    layout = json.loads(layout_with_command(tmp_path, "rm -rf ~").read_text())
    assert commands_in(layout) == ["rm -rf ~"]
    assert commands_in({"widgets": [new_widget("clock")]}) == []


def test_a_layout_file_with_commands_needs_allow_commands(tmp_path):
    path = layout_with_command(tmp_path)
    with pytest.raises(DeviceError, match="runs shell commands: echo hi"):
        cli._layout(SimpleNamespace(file=str(path), allow_commands=False), RT82)
    layout = cli._layout(SimpleNamespace(file=str(path), allow_commands=True), RT82)
    assert commands_in(layout) == ["echo hi"]


def test_lock_without_runtime_dir_stays_out_of_tmp(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert base._lock_path() == str(tmp_path / ".cache" / "epomaker-screen" / "epomaker-screen.lock")
    with base.exclusive():
        pass


def test_lock_refuses_a_symlink(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    os.symlink(tmp_path / "elsewhere", tmp_path / "epomaker-screen.lock")
    with pytest.raises(OSError), base.exclusive():
        pass
    assert not (tmp_path / "elsewhere").exists()
