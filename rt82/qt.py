"""Qt binding shim: PyQt6 (preferred) or PySide6."""

from __future__ import annotations

try:
    from PyQt6 import QtCore, QtGui, QtWidgets  # noqa: F401
    from PyQt6.QtCore import pyqtSignal as Signal  # noqa: F401

    BINDING = "PyQt6"
except ImportError:  # pragma: no cover - depends on the system
    from PySide6 import QtCore, QtGui, QtWidgets  # noqa: F401
    from PySide6.QtCore import Signal  # noqa: F401

    BINDING = "PySide6"


def image_bytes(img: "QtGui.QImage") -> bytes:
    """Raw bytes of a QImage (both bindings)."""
    ptr = img.constBits()
    if hasattr(ptr, "setsize"):  # PyQt: sip.voidptr
        ptr.setsize(img.sizeInBytes())
    return bytes(ptr)
