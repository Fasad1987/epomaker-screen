"""Minimal Linux hidraw access without native dependencies.

Devices are found through sysfs (/sys/class/hidraw) by USB vendor/product id
and by the usage page declared in the HID report descriptor, so the right
interface is picked even when several interfaces share the same ids.
"""

from __future__ import annotations

import errno
import os
import select
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

SYSFS_HIDRAW = Path("/sys/class/hidraw")


@dataclass(frozen=True)
class HidInfo:
    node: str  # /dev/hidrawN
    vendor_id: int
    product_id: int
    usage_page: int | None
    interface: int | None
    in_size: int | None
    out_size: int | None
    name: str


def _parse_items(desc: bytes) -> Iterator[tuple[int, int, int]]:
    """Yields (tag, type, value) for each short item of a report descriptor."""
    i = 0
    while i < len(desc):
        prefix = desc[i]
        if prefix == 0xFE:  # long item
            if i + 1 >= len(desc):
                return
            i += 3 + desc[i + 1]
            continue
        size = (0, 1, 2, 4)[prefix & 3]
        value = int.from_bytes(desc[i + 1 : i + 1 + size], "little") if size else 0
        yield prefix >> 4, (prefix >> 2) & 3, value
        i += 1 + size


def parse_descriptor(desc: bytes) -> tuple[int | None, int | None, int | None]:
    """Returns (first usage page, input report bytes, output report bytes).

    Only meant for the simple vendor descriptors used by the RT82 (one
    report, no report ids); good enough to pick the report size.
    """
    usage_page = None
    report_size = 0
    report_count = 0
    bits = {"in": 0, "out": 0}
    for tag, typ, value in _parse_items(desc):
        if typ == 1:  # global
            if tag == 0x0 and usage_page is None:
                usage_page = value
            elif tag == 0x7:
                report_size = value
            elif tag == 0x9:
                report_count = value
        elif typ == 0:  # main
            if tag == 0x8:
                bits["in"] += report_size * report_count
            elif tag == 0x9:
                bits["out"] += report_size * report_count
    size = lambda b: (b + 7) // 8 if b else None  # noqa: E731
    return usage_page, size(bits["in"]), size(bits["out"])


def _uevent(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(errors="replace").splitlines():
        key, _, value = line.partition("=")
        out[key] = value
    return out


def _interface_number(device_dir: Path) -> int | None:
    # .../1-2:1.1/0003:36B0:30A3.0005 -> the parent directory names the USB interface
    try:
        parent = device_dir.resolve().parent.name
    except OSError:
        return None
    if ":" in parent and "." in parent:
        try:
            return int(parent.rsplit(".", 1)[1])
        except ValueError:
            return None
    return None


def enumerate_hidraw(sysfs: Path = SYSFS_HIDRAW) -> list[HidInfo]:
    result = []
    try:
        names = sorted(os.listdir(sysfs), key=lambda n: (len(n), n))
    except OSError:
        return result
    for name in names:
        base = sysfs / name / "device"
        try:
            ev = _uevent(base / "uevent")
            bus, vid, pid = ev.get("HID_ID", "0:0:0").split(":")
            desc = (base / "report_descriptor").read_bytes()
        except (OSError, ValueError):
            continue  # device vanished while we looked
        usage_page, in_size, out_size = parse_descriptor(desc)
        result.append(
            HidInfo(
                node=f"/dev/{name}",
                vendor_id=int(vid, 16),
                product_id=int(pid, 16),
                usage_page=usage_page,
                interface=_interface_number(base),
                in_size=in_size,
                out_size=out_size,
                name=ev.get("HID_NAME", ""),
            )
        )
    return result


def find(vendor_id: int, product_id: int, usage_page: int, interface: int | None = None) -> HidInfo | None:
    """Finds a node by ids and usage page; falls back to the interface number."""
    candidates = [d for d in enumerate_hidraw() if d.vendor_id == vendor_id and d.product_id == product_id]
    for d in candidates:
        if d.usage_page == usage_page:
            return d
    if interface is not None:
        for d in candidates:
            if d.interface == interface:
                return d
    return None


def present(vendor_id: int, product_id: int) -> bool:
    return any(d.vendor_id == vendor_id and d.product_id == product_id for d in enumerate_hidraw())


class HidTimeout(IOError):
    pass


class HidDevice:
    """One open hidraw node. Writes prepend report id 0 (devices without report ids)."""

    def __init__(self, info: HidInfo, report_size: int):
        self.info = info
        self.report_size = report_size
        try:
            self.fd: int | None = os.open(info.node, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
        except PermissionError as e:
            raise PermissionError(
                f"no access to {info.node}: install the udev rule (scripts/install.sh) and reconnect the keyboard"
            ) from e

    @property
    def node(self) -> str:
        return self.info.node

    def close(self) -> None:
        if self.fd is not None:
            try:
                os.close(self.fd)
            finally:
                self.fd = None

    def __enter__(self) -> HidDevice:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _fd(self) -> int:
        if self.fd is None:
            raise OSError(f"{self.info.node} is closed")
        return self.fd

    def write(self, data: bytes) -> None:
        buf = bytes(1) + bytes(data[: self.report_size]).ljust(self.report_size, b"\0")
        os.write(self._fd(), buf)

    def try_read(self) -> bytes | None:
        try:
            return os.read(self._fd(), max(self.report_size, 64))
        except BlockingIOError:
            return None
        except OSError as e:
            if e.errno == errno.EAGAIN:
                return None
            raise

    def drain(self) -> None:
        while self.try_read():
            pass

    def read_until(self, match: Callable[[bytes], bool], timeout: float) -> bytes:
        end = time.monotonic() + timeout
        while True:
            r = self.try_read()
            if r is not None:
                if match(r):
                    return r
                continue
            left = end - time.monotonic()
            if left <= 0:
                raise HidTimeout(f"{self.info.node}: no reply from the device")
            select.select([self._fd()], [], [], min(left, 0.05))
