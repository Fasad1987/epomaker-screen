"""Finding connected Epomaker keyboards and the driver for each."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Union

from . import hid
from .drivers.rdm import RdmDriver
from .drivers.tft import COMMAND_PAGE, COMMAND_USAGE, TftDriver
from .models import EPOMAKER_RDM_VID, EPOMAKER_TFT_VID, MODELS, TFT_PIDS

Driver = Union[RdmDriver, TftDriver]


@dataclass
class Found:
    usb_path: str
    vid: int
    pid: int
    name: str  # HID name reported by the keyboard
    driver: Driver | None  # None: an Epomaker device without a supported screen
    nodes: list[hid.HidInfo]

    @property
    def usb_id(self) -> str:
        return f"{self.vid:04x}:{self.pid:04x}"


RDM_PIDS = {m.pid: m for m in MODELS if m.family == "rdm"}
EPOMAKER_VIDS = {EPOMAKER_RDM_VID, EPOMAKER_TFT_VID}
SCREEN_CONTROLLER = (0x1919, 0x1919)  # the RDM screen controller, not a keyboard


def detect(devices: list[hid.HidInfo] | None = None) -> list[Found]:
    """Epomaker keyboards on USB, one entry per USB device."""
    devices = devices if devices is not None else hid.enumerate_hidraw()
    groups: dict[str, list[hid.HidInfo]] = {}
    for d in devices:
        if d.vendor_id in EPOMAKER_VIDS:
            groups.setdefault(d.usb_path or d.node, []).append(d)
    found = []
    for usb, nodes in sorted(groups.items()):
        vid, pid, name = nodes[0].vendor_id, nodes[0].product_id, nodes[0].name
        driver: Driver | None = None
        if vid == EPOMAKER_RDM_VID and pid in RDM_PIDS:
            driver = RdmDriver(RDM_PIDS[pid], usb)
        elif (
            vid == EPOMAKER_TFT_VID
            and pid in TFT_PIDS
            and any(n.descriptor.has(COMMAND_PAGE, COMMAND_USAGE) for n in nodes)
        ):
            driver = TftDriver(vid, pid, usb)
        found.append(Found(usb, vid, pid, name, driver, sorted(nodes, key=lambda n: n.node)))
    return found


def screens(devices: list[hid.HidInfo] | None = None) -> list[Driver]:
    return [f.driver for f in detect(devices) if f.driver is not None]
