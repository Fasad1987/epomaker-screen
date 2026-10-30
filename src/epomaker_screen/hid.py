"""Linux hidraw access without native dependencies.

Devices are found through sysfs (/sys/class/hidraw) by USB vendor/product id
and by what their HID report descriptor declares (usage page, usage, report
sizes), so the right interface is picked even when several interfaces share
the same ids.
"""

from __future__ import annotations

import errno
import fcntl
import os
import select
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

SYSFS_HIDRAW = Path("/sys/class/hidraw")


@dataclass(frozen=True)
class Collection:
    usage_page: int
    usage: int


@dataclass(frozen=True)
class Descriptor:
    """What a report descriptor declares, as far as picking an interface needs."""

    collections: tuple[Collection, ...]
    # Report sizes in bytes (excluding the report id), keyed by report id (0 = unnumbered).
    input: dict[int, int] = field(default_factory=dict)
    output: dict[int, int] = field(default_factory=dict)
    feature: dict[int, int] = field(default_factory=dict)

    @property
    def usage_page(self) -> int | None:
        return self.collections[0].usage_page if self.collections else None

    def has(self, usage_page: int, usage: int | None = None) -> bool:
        return any(c.usage_page == usage_page and (usage is None or c.usage == usage) for c in self.collections)

    def size(self, kind: str) -> int | None:
        sizes = getattr(self, kind)
        return max(sizes.values()) if sizes else None


def _items(desc: bytes) -> Iterator[tuple[int, int, int, int]]:
    """Yields (tag, type, value, size) for each short item of a report descriptor."""
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
        yield prefix >> 4, (prefix >> 2) & 3, value, size
        i += 1 + size


def parse_descriptor(desc: bytes) -> Descriptor:
    """Collections and report sizes of a report descriptor.

    Handles the global/local items vendor descriptors use (usage page, usage,
    report id/size/count, push/pop). Good enough to identify interfaces and
    size reports; not a full HID parser.
    """
    g = {"page": 0, "size": 0, "count": 0, "id": 0}
    stack: list[dict] = []
    usages: list[tuple[int, int]] = []
    collections: list[Collection] = []
    bits: dict[str, dict[int, int]] = {"input": {}, "output": {}, "feature": {}}
    for tag, typ, value, size in _items(desc):
        if typ == 1:  # global
            if tag == 0x0:
                g["page"] = value
            elif tag == 0x7:
                g["size"] = value
            elif tag == 0x8:
                g["id"] = value
            elif tag == 0x9:
                g["count"] = value
            elif tag == 0xA:
                stack.append(dict(g))
            elif tag == 0xB and stack:
                g = stack.pop()
        elif typ == 2:  # local
            if tag == 0x0:
                # A 4-byte usage carries its own page in the high half.
                usages.append((value >> 16, value & 0xFFFF) if size == 4 else (g["page"], value))
        elif typ == 0:  # main
            if tag == 0xA and usages and value == 1:  # application collection
                collections.append(Collection(*usages[0]))
            elif tag in (0x8, 0x9, 0xB):
                kind = {0x8: "input", 0x9: "output", 0xB: "feature"}[tag]
                bits[kind][g["id"]] = bits[kind].get(g["id"], 0) + g["size"] * g["count"]
            usages = []
    to_bytes = {k: {rid: (b + 7) // 8 for rid, b in v.items()} for k, v in bits.items()}
    return Descriptor(tuple(collections), to_bytes["input"], to_bytes["output"], to_bytes["feature"])


@dataclass(frozen=True)
class HidInfo:
    node: str  # /dev/hidrawN
    vendor_id: int
    product_id: int
    interface: int | None
    descriptor: Descriptor
    name: str
    usb_path: str  # sysfs name of the USB device ("3-9.3"), "" if not USB

    @property
    def usage_page(self) -> int | None:
        return self.descriptor.usage_page

    @property
    def out_size(self) -> int | None:
        return self.descriptor.size("output")


def _uevent(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(errors="replace").splitlines():
        key, _, value = line.partition("=")
        out[key] = value
    return out


def _usb_location(device_dir: Path) -> tuple[int | None, str]:
    # .../3-9.3/3-9.3:1.1/0003:36B0:30A3.0005: the parent names the USB interface,
    # the grandparent the USB device.
    try:
        parent = device_dir.resolve().parent
    except OSError:
        return None, ""
    name = parent.name
    if ":" in name and "." in name:
        try:
            return int(name.rsplit(".", 1)[1]), parent.parent.name
        except ValueError:
            pass
    return None, ""


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
            _bus, vid, pid = ev.get("HID_ID", "0:0:0").split(":")
            desc = parse_descriptor((base / "report_descriptor").read_bytes())
        except (OSError, ValueError):
            continue  # device vanished while we looked
        interface, usb = _usb_location(base)
        result.append(
            HidInfo(
                node=f"/dev/{name}",
                vendor_id=int(vid, 16),
                product_id=int(pid, 16),
                interface=interface,
                descriptor=desc,
                name=ev.get("HID_NAME", ""),
                usb_path=usb,
            )
        )
    return result


def find(
    vendor_id: int,
    product_id: int,
    usage_page: int,
    interface: int | None = None,
    usage: int | None = None,
    devices: list[HidInfo] | None = None,
) -> HidInfo | None:
    """Finds a node by ids and collection; falls back to the interface number."""
    pool = devices if devices is not None else enumerate_hidraw()
    candidates = [d for d in pool if (d.vendor_id, d.product_id) == (vendor_id, product_id)]
    for d in candidates:
        if d.descriptor.has(usage_page, usage):
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


def _feature_ioctl(number: int, length: int) -> int:
    # _IOC(_IOC_READ | _IOC_WRITE, 'H', number, length)
    return (3 << 30) | (length << 16) | (ord("H") << 8) | number


class HidDevice:
    """One open hidraw node.

    ``write`` sends an output report and ``set_feature``/``get_feature`` use
    feature reports, all with report id 0 (devices without report ids) unless
    told otherwise.
    """

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

    def set_feature(self, data: bytes, report_id: int = 0) -> None:
        buf = bytes([report_id]) + bytes(data[: self.report_size]).ljust(self.report_size, b"\0")
        fcntl.ioctl(self._fd(), _feature_ioctl(0x06, len(buf)), buf)

    def get_feature(self, report_id: int = 0, size: int | None = None) -> bytes:
        size = size or self.report_size
        buf = bytearray([report_id]) + bytearray(size)
        n = fcntl.ioctl(self._fd(), _feature_ioctl(0x07, len(buf)), buf, True)
        # The kernel puts the report id first for numbered reports; for unnumbered
        # ones it may or may not include a 0 byte.
        if n == size + 1:
            return bytes(buf[1:n])
        return bytes(buf[: max(0, n)])

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
