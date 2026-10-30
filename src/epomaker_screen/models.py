"""Known Epomaker keyboards with screens.

Two screen families are supported:

* ``rdm``: an "RDMCTMZT" screen controller behind the keyboard's internal USB
  hub (USB 1919:1919) that plays QGIF animations from its own flash
  (RT82, QK108, RT98). See docs/protocols/rdm.md.
* ``tft``: the keyboard firmware itself takes raw RGB565 pixels over a
  64-byte feature report on the vendor collection (usage page 0xFFFF,
  usage 2), with five picture banks (RT100 Pro, RT75, RT85, Glyph, RT100).
  Models sharing a USB id are told apart by the id the 0x8F command reports.
  See docs/protocols/tft.md.

Screen sizes, memory and internal ids of the ``tft`` models come from the
model catalog in Epomaker's installers, as recovered by
ramarivera/epomaker-driver-linux (MIT). ``verified`` means the model was tried
with this project on a real keyboard.
"""

from __future__ import annotations

from .drivers.base import Model

EPOMAKER_RDM_VID = 0x36B0
EPOMAKER_TFT_VID = 0x3151


def _tft(key, name, pid, internal_id, w, h, *, rgb=16, memory_mib=6, banks=5, verified=False, notes=""):
    pixel_bytes = 3 if rgb == 24 else 2
    # The vendor's drawing board rounds each frame up to whole 4 KiB blocks (even
    # when already aligned), keeps a 4 KiB header and reserves the still banks.
    block = (w * h * pixel_bytes // 4096 + 1) * 4096
    max_frames = min(255, (memory_mib * 1024 * 1024 - 4096) // block - banks)
    return Model(
        key, name, "tft", EPOMAKER_TFT_VID, pid, w, h, internal_id, verified, notes,
        extra={"pixel_bytes": pixel_bytes, "banks": banks, "max_frames": max_frames, "memory_mib": memory_mib},
    )  # fmt: skip


MODELS: tuple[Model, ...] = (
    # -- rdm: QGIF in the screen controller's flash
    Model("rt82", "Epomaker RT82", "rdm", EPOMAKER_RDM_VID, 0x30A3, 240, 135, verified=True),
    Model(
        "qk108",
        "Epomaker QK108",
        "rdm",
        EPOMAKER_RDM_VID,
        0x30AF,
        240,
        136,
        notes="same controller as the RT82; verified by wyattades/keyscreen, not by this project",
    ),  # fmt: skip
    Model(
        "rt98", "Epomaker RT98", "rdm", EPOMAKER_RDM_VID, 0x30B9, 240, 136, notes="same screen settings in Epomaker Hub"
    ),
    # -- tft: raw RGB565 pixels through the keyboard firmware
    _tft(
        "rt100pro",
        "Epomaker RT100 Pro",
        0x5002,
        3152,
        240,
        240,
        memory_mib=7,
        notes="memory size missing from the catalog; vendor default 7 MiB",
    ),
    _tft("rt75", "Epomaker RT75", 0x5002, 3223, 240, 240),
    _tft("rt85", "Epomaker RT85", 0x5002, 2895, 320, 172),
    _tft("glyph", "Epomaker Glyph", 0x5002, 3059, 428, 142),
    _tft("rt100", "Epomaker RT100", 0x4015, 1379, 162, 173),
    _tft("dynatab75x", "Epomaker Dynatab75X-UK", 0x4015, 1723, 60, 9, rgb=24, memory_mib=7),
    _tft("he65mag", "Epomaker HE65 Mag", 0x502F, 2376, 128, 128),
)

TFT_PIDS = frozenset(m.pid for m in MODELS if m.family == "tft")


def by_key(key: str) -> Model:
    for m in MODELS:
        if m.key == key:
            return m
    raise KeyError(key)


def by_usb_id(vid: int, pid: int) -> list[Model]:
    return [m for m in MODELS if (m.vid, m.pid) == (vid, pid)]


def by_internal_id(internal_id: int) -> Model | None:
    for m in MODELS:
        if m.internal_id == internal_id:
            return m
    return None
