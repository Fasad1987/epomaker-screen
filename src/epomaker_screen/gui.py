"""Epomaker Screen: Qt desktop app (editor and tray) for the screens of Epomaker keyboards.

Lives in the system tray, keeps the keyboard clock in sync and lets you lay
out widgets on the connected keyboard's screen.
"""

from __future__ import annotations

import copy
import datetime as dt
import json
import sys
import time
from pathlib import Path

from . import __version__
from .config import Settings, load_layout, save_layout
from .drivers.base import Animation
from .drivers.rdm.live import LiveConfig
from .drivers.tft import MIN_REFRESH_S, RefreshConfig, erase_notice
from .qt import QtCore, QtGui, QtWidgets, Signal
from .render import FIT_MODES, RT82, Geometry, load_frames, to_rgb
from .service import CONNECTED, FROZEN, LIVE, LIVE_STARTING, NO_KEYBOARD, PAUSED, Controller
from .sources import DataHub, FirmwareSystemInfo
from .widgets import METRICS, TYPES, Context, Prop, commands_in, new_widget, props_of, render_layout, renderer

Qt = QtCore.Qt
HANDLE = 6  # resize handle, screen pixels
APP_ID = "epomaker-screen"
APP_NAME = "Epomaker Screen"
HISTORY = 200  # undo steps
SP = QtWidgets.QStyle.StandardPixmap
# Icon themes differ between desktops (GNOME has few of these names): fall back to Qt's own icons.
FALLBACK_ICONS = {
    "list-add": SP.SP_FileDialogNewFolder,
    "media-playback-start": SP.SP_MediaPlay,
    "media-playback-pause": SP.SP_MediaPause,
    "document-send": SP.SP_ArrowUp,
    "folder-pictures": SP.SP_DirOpenIcon,
    "chronometer": SP.SP_BrowserReload,
    "go-up": SP.SP_ArrowUp,
    "go-down": SP.SP_ArrowDown,
    "edit-copy": SP.SP_FileDialogContentsView,
    "list-remove": SP.SP_TrashIcon,
    "edit-select-none": SP.SP_DesktopIcon,
    "edit-undo": SP.SP_ArrowBack,
    "edit-redo": SP.SP_ArrowForward,
    "configure": SP.SP_FileDialogDetailedView,
    "application-exit": SP.SP_DialogCloseButton,
    "dialog-information": SP.SP_MessageBoxInformation,
    "edit-clear": SP.SP_DialogResetButton,
    "document-open": SP.SP_DialogOpenButton,
    "document-save-as": SP.SP_DialogSaveButton,
}
STATE_COLORS = {
    NO_KEYBOARD: "#9ca3af",
    CONNECTED: "#22c55e",
    LIVE: "#38bdf8",
    LIVE_STARTING: "#38bdf8",
    PAUSED: "#f59e0b",
    FROZEN: "#ef4444",
}


def icon(name: str) -> QtGui.QIcon:
    i = QtGui.QIcon.fromTheme(name)
    if i.isNull() and name in FALLBACK_ICONS:
        i = QtWidgets.QApplication.style().standardIcon(FALLBACK_ICONS[name])
    return i


class Bridge(QtCore.QObject):
    """Moves controller callbacks (worker thread) onto the GUI thread."""

    log = Signal(str)
    state = Signal(str)
    job_done = Signal(object, object, object)  # callback, error, result
    device = Signal(object)  # driver or None


# -- screen canvas -------------------------------------------------------------


class CanvasView(QtWidgets.QScrollArea):
    """Holds the canvas; in "Fit" zoom the canvas follows this area's size."""

    def resizeEvent(self, e):
        super().resizeEvent(e)
        canvas = self.widget()
        if isinstance(canvas, Canvas):
            canvas.resize_for_geometry()


class Canvas(QtWidgets.QWidget):
    selected = Signal(int)
    geometry_changed = Signal()

    def __init__(self, window: MainWindow):
        super().__init__()
        self.win = window
        self.sel: int | None = None
        self._drag: tuple[str, QtCore.QPointF, dict] | None = None
        self.resize_for_geometry()
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip(
            "Click a widget to select it; drag to move it, drag its bottom-right corner to resize\n"
            "(hold Alt to ignore the grid). Arrow keys move by 1 px (with Shift: resize). Delete removes it."
        )

    def paintEvent(self, e):
        g, scale = self.win.geometry, self.scale
        img = render_layout(self.win.layout, Context(time.time(), self.win.hub.snapshot(), preview=True), self.sel, g)
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor("#000000"))
        p.drawImage(QtCore.QRectF(1, 1, g.width * scale, g.height * scale), img, QtCore.QRectF(0, 0, g.width, g.height))
        step = self.win.grid
        if self.win.show_grid and step * scale >= 6:
            p.setPen(QtGui.QPen(QtGui.QColor(255, 255, 255, 40), 1))
            for x in range(step, g.width, step):
                p.drawLine(QtCore.QLineF(1 + x * scale, 1, 1 + x * scale, g.height * scale))
            for y in range(step, g.height, step):
                p.drawLine(QtCore.QLineF(1, 1 + y * scale, g.width * scale, 1 + y * scale))
        if self._drag and self.sel is not None:  # centre guides while moving
            w = self.win.layout["widgets"][self.sel]
            p.setPen(QtGui.QPen(QtGui.QColor("#ff00ff"), 1, Qt.PenStyle.DotLine))
            if w["x"] * 2 + w["w"] == g.width:
                p.drawLine(QtCore.QLineF(1 + g.width * scale / 2, 1, 1 + g.width * scale / 2, g.height * scale))
            if w["y"] * 2 + w["h"] == g.height:
                p.drawLine(QtCore.QLineF(1, 1 + g.height * scale / 2, g.width * scale, 1 + g.height * scale / 2))
        p.setPen(QtGui.QColor("#555555"))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))
        p.end()

    def _screen_pos(self, e) -> QtCore.QPointF:
        pos = e.position()
        return QtCore.QPointF((pos.x() - 1) / self.scale, (pos.y() - 1) / self.scale)

    @property
    def scale(self) -> float:
        if self.win.zoom:
            return float(self.win.zoom)
        g = self.win.geometry
        view = getattr(self.win, "canvas_view", None)
        w, h = (view.viewport().width() - 2, view.viewport().height() - 2) if view is not None else (720, 480)
        return max(1.0, min(w / g.width, h / g.height, 6.0))

    def resize_for_geometry(self) -> None:
        g = self.win.geometry
        self.setFixedSize(int(g.width * self.scale) + 2, int(g.height * self.scale) + 2)

    def _hit(self, pt: QtCore.QPointF) -> int | None:
        widgets = self.win.layout["widgets"]
        for i in range(len(widgets) - 1, -1, -1):
            w = widgets[i]
            if QtCore.QRectF(w["x"], w["y"], w["w"], w["h"]).contains(pt):
                return i
        return None

    def _on_handle(self, pt: QtCore.QPointF) -> bool:
        if self.sel is None:
            return False
        w = self.win.layout["widgets"][self.sel]
        return abs(pt.x() - (w["x"] + w["w"])) <= HANDLE and abs(pt.y() - (w["y"] + w["h"])) <= HANDLE

    def mousePressEvent(self, e):
        pt = self._screen_pos(e)
        if not self._on_handle(pt):
            self.sel = self._hit(pt)
            self.selected.emit(-1 if self.sel is None else self.sel)
        if self.sel is not None:
            w = self.win.layout["widgets"][self.sel]
            mode = "resize" if self._on_handle(pt) else "move"
            self._drag = (mode, pt, dict(w))
        self.update()

    def mouseMoveEvent(self, e):
        pt = self._screen_pos(e)
        resize = self._on_handle(pt) or (self._drag and self._drag[0] == "resize")
        self.setCursor(Qt.CursorShape.SizeFDiagCursor if resize else Qt.CursorShape.ArrowCursor)
        if not self._drag or self.sel is None:
            return
        mode, start, orig = self._drag
        dx, dy = round(pt.x() - start.x()), round(pt.y() - start.y())
        w = self.win.layout["widgets"][self.sel]
        g = self.win.geometry
        # Snap to the grid and to the screen centre; hold Alt to place freely.
        snap = self.win.snap and not e.modifiers() & Qt.KeyboardModifier.AltModifier
        step = self.win.grid if snap else 1
        if mode == "move":
            x, y = orig["x"] + dx, orig["y"] + dy
            x, y = round(x / step) * step, round(y / step) * step
            if snap and abs(x * 2 + orig["w"] - g.width) <= 4:
                x = (g.width - orig["w"]) // 2
            if snap and abs(y * 2 + orig["h"] - g.height) <= 4:
                y = (g.height - orig["h"]) // 2
            w["x"] = max(-orig["w"] + 4, min(g.width - 4, x))
            w["y"] = max(-orig["h"] + 4, min(g.height - 4, y))
        else:
            right, bottom = orig["x"] + orig["w"] + dx, orig["y"] + orig["h"] + dy
            w["w"] = max(4, round(right / step) * step - orig["x"])
            w["h"] = max(4, round(bottom / step) * step - orig["y"])
        self.geometry_changed.emit()
        self.update()

    def mouseReleaseEvent(self, e):
        if self._drag:
            self._drag = None
            self.win.changed()

    def keyPressEvent(self, e):
        if self.sel is None:
            return super().keyPressEvent(e)
        d = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0), Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1)}.get(
            e.key()
        )
        if e.key() == Qt.Key.Key_Delete:
            self.win.remove_widget()
            return
        if not d:
            return super().keyPressEvent(e)
        w = self.win.layout["widgets"][self.sel]
        if e.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            w["w"], w["h"] = max(4, w["w"] + d[0]), max(4, w["h"] + d[1])
        else:
            w["x"], w["y"] = w["x"] + d[0], w["y"] + d[1]
        self.geometry_changed.emit()
        self.win.changed()
        self.update()


# -- property editor -------------------------------------------------------------


class ColorButton(QtWidgets.QPushButton):
    changed = Signal(str)

    def __init__(self, color: str):
        super().__init__()
        self.set(color)
        self.clicked.connect(self._pick)

    def set(self, color: str) -> None:
        self.color = color
        self.setText(color)
        fg = "#000000" if QtGui.QColor(color).lightness() > 128 else "#ffffff"
        self.setStyleSheet(f"background:{color};color:{fg}")

    def _pick(self):
        c = QtWidgets.QColorDialog.getColor(QtGui.QColor(self.color), self, "Color")
        if c.isValid():
            self.set(c.name())
            self.changed.emit(c.name())


def editor_for(prop: Prop, value, on_change, parent) -> QtWidgets.QWidget:
    k = prop.kind
    if k == "color":
        w = ColorButton(str(value))
        w.changed.connect(on_change)
    elif k == "bool":
        w = QtWidgets.QCheckBox()
        w.setChecked(bool(value))
        w.toggled.connect(on_change)
    elif k == "int":
        w = QtWidgets.QSpinBox()
        o = prop.options or {}
        w.setRange(int(o.get("min", -9999)), int(o.get("max", 9999)))
        w.setValue(int(value))
        w.valueChanged.connect(on_change)
    elif k == "float":
        w = QtWidgets.QDoubleSpinBox()
        o = prop.options or {}
        w.setDecimals(3)
        w.setRange(float(o.get("min", -1e6)), float(o.get("max", 1e6)))
        w.setValue(float(value))
        w.valueChanged.connect(on_change)
    elif k == "choice":
        w = QtWidgets.QComboBox()
        for key, label in (prop.options or {}).items():
            w.addItem(label, key)
        i = w.findData(value)
        w.setCurrentIndex(max(0, i))
        w.currentIndexChanged.connect(lambda _i: on_change(w.currentData()))
    elif k == "font":
        w = QtWidgets.QFontComboBox()
        w.setCurrentFont(QtGui.QFont(str(value)))
        w.currentFontChanged.connect(lambda f: on_change(f.family()))
    elif k == "multiline":
        w = QtWidgets.QPlainTextEdit(str(value))
        w.setFixedHeight(70)
        w.textChanged.connect(lambda: on_change(w.toPlainText()))
    elif k == "file":
        w = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        edit = QtWidgets.QLineEdit(str(value))
        btn = QtWidgets.QPushButton("…")
        btn.setFixedWidth(32)
        h.addWidget(edit)
        h.addWidget(btn)
        edit.editingFinished.connect(lambda: on_change(edit.text()))

        def browse():
            path, _ = QtWidgets.QFileDialog.getOpenFileName(
                parent, "Image", str(Path.home()), "Images (*.png *.jpg *.jpeg *.gif *.webp *.svg *.bmp)"
            )
            if path:
                edit.setText(path)
                on_change(path)

        btn.clicked.connect(browse)
    else:
        w = QtWidgets.QLineEdit(str(value))
        w.textChanged.connect(on_change)
    return w


class PropertyPanel(QtWidgets.QScrollArea):
    def __init__(self, window: MainWindow):
        super().__init__()
        self.win = window
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.index: int | None = None
        self.geo: dict[str, QtWidgets.QSpinBox] = {}
        self.show_widget(None)

    def show_widget(self, index: int | None) -> None:
        self.index = index
        body = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(body)
        form.setFieldGrowthPolicy(QtWidgets.QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setRowWrapPolicy(QtWidgets.QFormLayout.RowWrapPolicy.WrapLongRows)
        self.geo = {}
        if index is None:
            self._background_form(form)
        else:
            w = self.win.layout["widgets"][index]
            t = TYPES[w["type"]]
            title = QtWidgets.QLabel(f"<b>{t.title}</b>")
            form.addRow(title)
            g = self.win.geometry
            for label, pair in (("Position", (("x", g.width), ("y", g.height))), ("Size", (("w", 1000), ("h", 1000)))):
                row = QtWidgets.QHBoxLayout()
                for key, hi in pair:
                    sb = QtWidgets.QSpinBox()
                    sb.setRange(-400 if key in "xy" else 1, hi)
                    sb.setValue(int(w[key]))
                    sb.setPrefix({"x": "x ", "y": "y ", "w": "w ", "h": "h "}[key])
                    sb.valueChanged.connect(lambda v, key=key: self._set_geo(key, v))
                    self.geo[key] = sb
                    row.addWidget(sb)
                form.addRow(label, row)
            hidden = QtWidgets.QCheckBox("Hidden")
            hidden.setChecked(bool(w.get("hidden")))
            hidden.toggled.connect(lambda v: self._set_raw("hidden", v))
            form.addRow(hidden)
            props = props_of(w)
            for prop in t.props:
                form.addRow(
                    prop.label, editor_for(prop, props[prop.key], lambda v, k=prop.key: self._set_prop(k, v), self)
                )
        self.setWidget(body)

    def _background_form(self, form: QtWidgets.QFormLayout) -> None:
        L = self.win.layout
        form.addRow(
            QtWidgets.QLabel("<b>Screen background</b><br><small>Select a widget on the screen to edit it.</small>")
        )
        form.addRow(
            "Color",
            editor_for(
                Prop("background", "", "color", ""),
                L.get("background", "#000000"),
                lambda v: self._set_layout("background", v),
                self,
            ),
        )
        form.addRow(
            "Image",
            editor_for(
                Prop("background_image", "", "file", ""),
                L.get("background_image", ""),
                lambda v: self._set_layout("background_image", v),
                self,
            ),
        )
        form.addRow(
            "Scaling",
            editor_for(
                Prop("background_mode", "", "choice", "cover", FIT_MODES),
                L.get("background_mode", "cover"),
                lambda v: self._set_layout("background_mode", v),
                self,
            ),
        )

    def refresh_geometry(self) -> None:
        if self.index is None:
            return
        w = self.win.layout["widgets"][self.index]
        for key, sb in self.geo.items():
            if sb.value() != int(w[key]):
                sb.blockSignals(True)
                sb.setValue(int(w[key]))
                sb.blockSignals(False)

    def _set_layout(self, key, value):
        self.win.layout[key] = value
        self.win.changed()

    def _set_geo(self, key, v):
        self.win.layout["widgets"][self.index][key] = v
        self.win.changed()

    def _set_raw(self, key, v):
        self.win.layout["widgets"][self.index][key] = v
        self.win.changed()

    def _set_prop(self, key, v):
        self.win.layout["widgets"][self.index].setdefault("props", {})[key] = v
        self.win.changed()


# -- main window -------------------------------------------------------------------


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self, app: QtWidgets.QApplication):
        super().__init__()
        self.app = app
        self.settings = Settings.load()
        self.driver = None
        self.geometry: Geometry = RT82
        self.layout = load_layout(geometry=self.geometry)
        self._live_layout = copy.deepcopy(self.layout)
        self.hub = DataHub()
        self.hub.configure(self.layout)
        self.hub.start()
        self.quitting = False
        self._last_state = ""
        self._state = NO_KEYBOARD
        self._told_about_tray = False
        self._told_about_tft_live = False
        self.zoom = self.settings.zoom  # 0: fit the window
        self.show_grid = self.settings.show_grid
        self.snap = self.settings.snap
        self.grid = 4
        self._history = [json.dumps(self.layout)]
        self._hist_pos = 0
        self._hist_time = 0.0
        self._hist_sel: int | None = None

        self.bridge = Bridge()
        self.bridge.log.connect(self._append_log)
        self.bridge.state.connect(self._on_state)
        self.bridge.job_done.connect(lambda cb, err, res: cb(err, res))
        self.bridge.device.connect(self._on_device)
        self.ctl = Controller(
            log=self.bridge.log.emit,
            on_state=self.bridge.state.emit,
            on_device=self.bridge.device.emit,
            auto_sync=self.settings.auto_sync_time,
            resync_hours=self.settings.resync_hours,
            prefer=self.settings.device,
        )
        if self.settings.system_info:
            self.ctl.set_system_info(FirmwareSystemInfo())

        self.setWindowTitle(APP_NAME)
        self.setWindowIcon(app_icon())
        self._build()
        self._build_tray()
        self._update_undo()

        self._save_timer = QtCore.QTimer(self, singleShot=True, interval=800)
        self._save_timer.timeout.connect(self._save)
        self._tick = QtCore.QTimer(self, interval=1000)
        self._tick.timeout.connect(self.canvas.update)
        self._tick.start()

        self.ctl.start()
        if self.settings.live:
            self.live_action.setChecked(True)

    # UI -------------------------------------------------------------------

    def _build(self) -> None:
        self._build_actions()
        self._build_menus()
        self._build_toolbar()

        self.canvas = Canvas(self)
        self.canvas.selected.connect(lambda i: self._select(None if i < 0 else i, from_canvas=True))
        self.canvas.geometry_changed.connect(lambda: self.props.refresh_geometry())

        # left: the widget list
        self.list = QtWidgets.QListWidget()
        self.list.setToolTip("Topmost first. Untick a widget to hide it.")
        self.list.currentRowChanged.connect(lambda r: self._select(r if r >= 0 else None))
        self.list.itemChanged.connect(self._on_item_changed)
        add_btn = QtWidgets.QToolButton()
        add_btn.setText("Add widget")
        add_btn.setIcon(icon("list-add"))
        add_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        add_btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        add_btn.setMenu(self.add_menu)
        btns = QtWidgets.QHBoxLayout()
        for name, tip, fn in (
            ("go-up", "Raise (draw on top)", lambda: self.move_widget(1)),
            ("go-down", "Lower", lambda: self.move_widget(-1)),
            ("edit-copy", "Duplicate (Ctrl+D)", self.duplicate_widget),
            ("list-remove", "Remove (Delete)", self.remove_widget),
            ("edit-select-none", "Screen background", lambda: self._select(None)),
        ):
            b = QtWidgets.QToolButton()
            b.setIcon(icon(name))
            b.setToolTip(tip)
            b.clicked.connect(fn)
            btns.addWidget(b)
        btns.addStretch()
        left = QtWidgets.QWidget()
        lv = QtWidgets.QVBoxLayout(left)
        lv.setContentsMargins(0, 0, 0, 0)
        lv.addWidget(add_btn)
        lv.addWidget(self.list, 1)
        lv.addLayout(btns)
        left.setMinimumWidth(200)

        # centre: keyboard card, view controls, the screen
        self.card = QtWidgets.QLabel()
        self.card.setObjectName("card")
        self.card.setTextFormat(Qt.TextFormat.RichText)
        self.card.setWordWrap(True)
        self.card.setStyleSheet("#card { border: 1px solid palette(mid); border-radius: 6px; padding: 8px; }")
        view = QtWidgets.QHBoxLayout()
        self.zoom_box = QtWidgets.QComboBox()
        self.zoom_box.addItem("Fit", 0)
        for z in (1, 2, 3, 4):
            self.zoom_box.addItem(f"{z}×", z)
        self.zoom_box.setToolTip("Preview size")
        self.zoom_box.setCurrentIndex(max(0, self.zoom_box.findData(self.zoom)))
        self.zoom_box.currentIndexChanged.connect(self._on_zoom)
        grid = QtWidgets.QCheckBox("Grid")
        grid.setChecked(self.show_grid)
        grid.toggled.connect(self._on_grid)
        snap = QtWidgets.QCheckBox("Snap")
        snap.setToolTip(f"Move and resize in steps of {self.grid} px and snap to the screen centre (Alt: off)")
        snap.setChecked(self.snap)
        snap.toggled.connect(self._on_snap)
        view.addWidget(QtWidgets.QLabel("Zoom"))
        view.addWidget(self.zoom_box)
        view.addWidget(grid)
        view.addWidget(snap)
        view.addStretch()
        for name, tip, how in (
            ("format-justify-center", "Centre horizontally", "hcenter"),
            ("format-align-vertical-center", "Centre vertically", "vcenter"),
            ("zoom-fit-best", "Fill the screen", "fill"),
        ):
            b = QtWidgets.QToolButton()
            b.setIcon(icon(name))
            b.setText({"hcenter": "↔", "vcenter": "↕", "fill": "⤢"}[how])
            if b.icon().isNull():
                b.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            b.setToolTip(tip)
            b.clicked.connect(lambda _c=False, how=how: self.align_widget(how))
            view.addWidget(b)
        scroll = self.canvas_view = CanvasView()
        scroll.setAlignment(Qt.AlignmentFlag.AlignCenter)
        scroll.setWidget(self.canvas)
        scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        scroll.setMinimumSize(250, 150)
        self.hint = QtWidgets.QLabel(
            "On the keyboard: Fn + Right Shift shows the GIF page, where widgets and pictures "
            "appear; Fn + Enter cycles the screen pages."
        )
        self.hint.setWordWrap(True)
        self.hint.setEnabled(False)  # drawn in the muted text colour
        centre = QtWidgets.QWidget()
        cv = QtWidgets.QVBoxLayout(centre)
        cv.setContentsMargins(0, 0, 0, 0)
        cv.addWidget(self.card)
        cv.addLayout(view)
        cv.addWidget(scroll, 1)
        cv.addWidget(self.hint)

        # right: properties of the selection
        self.props = PropertyPanel(self)
        self.props.setMinimumWidth(300)

        split = QtWidgets.QSplitter()
        split.addWidget(left)
        split.addWidget(centre)
        split.addWidget(self.props)
        split.setStretchFactor(1, 1)
        split.setSizes([220, 640, 320])
        split.setContentsMargins(6, 6, 6, 0)
        self.setCentralWidget(split)

        # bottom: the log, as a dock that can be hidden
        self.log_view = QtWidgets.QPlainTextEdit(readOnly=True)
        self.log_view.setMaximumBlockCount(500)
        self.log_view.setPlaceholderText("Log")
        self.log_dock = QtWidgets.QDockWidget("Log", self)
        self.log_dock.setObjectName("log")
        self.log_dock.setWidget(self.log_view)
        self.addDockWidget(Qt.DockWidgetArea.BottomDockWidgetArea, self.log_dock)
        self.resizeDocks([self.log_dock], [110], Qt.Orientation.Vertical)
        self.view_menu.addAction(self.log_dock.toggleViewAction())
        self.view_menu.addAction(self.toolbar.toggleViewAction())

        self.status = QtWidgets.QLabel("…")
        self.statusBar().addPermanentWidget(self.status)
        self._refresh_list()
        self._update_card()
        self._update_keyboard_actions()
        self.resize(1240, 800)

    def _action(self, text: str, fn, icon_name: str = "", shortcut=None, tip: str = "") -> QtGui.QAction:
        a = QtGui.QAction(icon(icon_name) if icon_name else QtGui.QIcon(), text, self)
        a.triggered.connect(lambda _checked=False: fn())
        if shortcut is not None:
            a.setShortcut(QtGui.QKeySequence(shortcut))
        if tip:
            a.setToolTip(tip)
            a.setStatusTip(tip)
        return a

    def _build_actions(self) -> None:
        self.add_menu = QtWidgets.QMenu("Add widget", self)
        for name, t in TYPES.items():
            self.add_menu.addAction(t.title, lambda name=name: self.add_widget(name))

        self.live_action = QtGui.QAction(icon("media-playback-start"), "Live mode", self, checkable=True)
        self.live_action.setToolTip("Keep the widgets on the keyboard screen up to date")
        self.live_action.toggled.connect(self.set_live)
        self.pause_action = QtGui.QAction(icon("media-playback-pause"), "Pause", self, checkable=True)
        self.pause_action.setToolTip("Release the keyboard, e.g. to use Epomaker Hub in a browser")
        self.pause_action.toggled.connect(self.ctl.set_paused)
        a = self._action
        self.snapshot_action = a(
            "Send snapshot", self.send_snapshot, "document-send", tip="Store the layout on the keyboard as a picture"
        )
        self.media_action = a("Pictures/GIFs…", self.upload_media, "folder-pictures", "Ctrl+U")
        self.bank_action = a(
            "Picture into one bank…", self.upload_to_bank, tip="Replace one picture and keep the others"
        )
        self.clock_action = a("Set clock", self.sync_time, "chronometer", "Ctrl+T")
        self.language_action = a(
            "Switch screen language", self.toggle_language, tip="Toggles the language of the keyboard's own pages"
        )
        self.reset_action = a(
            "Clear pictures…", self.reset_screen, "edit-clear", tip="Black pictures and animation (safe)"
        )
        self.erase_action = a("Erase screen memory…", self.erase_screen, tip="Remove every picture and the animation")
        self.info_action = a("Keyboard info…", self.keyboard_info, "dialog-information", "Ctrl+I")
        self.undo_action = a("Undo", self.undo, "edit-undo", QtGui.QKeySequence.StandardKey.Undo)
        self.redo_action = a("Redo", self.redo, "edit-redo", QtGui.QKeySequence.StandardKey.Redo)
        self.duplicate_action = a("Duplicate widget", self.duplicate_widget, "edit-copy", "Ctrl+D")

    def _build_menus(self) -> None:
        a = self._action
        menu = self.menuBar().addMenu("&File")
        menu.addAction(a("Open layout…", self.open_layout, "document-open", QtGui.QKeySequence.StandardKey.Open))
        menu.addAction(
            a("Save layout as…", self.save_layout_as, "document-save-as", QtGui.QKeySequence.StandardKey.SaveAs)
        )
        menu.addAction(a("Export preview PNG…", self.export_png, shortcut="Ctrl+E"))
        menu.addSeparator()
        menu.addAction(a("Settings…", self.edit_settings, "configure", QtGui.QKeySequence.StandardKey.Preferences))
        menu.addSeparator()
        menu.addAction(a("Quit", self.quit, "application-exit", QtGui.QKeySequence.StandardKey.Quit))
        edit = self.menuBar().addMenu("&Edit")
        edit.addAction(self.undo_action)
        edit.addAction(self.redo_action)
        edit.addSeparator()
        edit.addMenu(self.add_menu)
        edit.addAction(self.duplicate_action)
        align = edit.addMenu("Align widget")
        for text, how in (
            ("Centre horizontally", "hcenter"),
            ("Centre vertically", "vcenter"),
            ("Left edge", "left"),
            ("Right edge", "right"),
            ("Top edge", "top"),
            ("Bottom edge", "bottom"),
            ("Fill the screen", "fill"),
        ):
            align.addAction(text, lambda how=how: self.align_widget(how))
        kb = self.menuBar().addMenu("&Keyboard")
        for act in (self.live_action, self.snapshot_action, self.media_action, self.bank_action, self.clock_action):
            kb.addAction(act)
        kb.addSeparator()
        kb.addAction(self.language_action)
        kb.addAction(self.reset_action)
        kb.addAction(self.erase_action)
        kb.addSeparator()
        kb.addAction(self.info_action)
        kb.addAction(self.pause_action)
        self.view_menu = self.menuBar().addMenu("&View")
        helpm = self.menuBar().addMenu("&Help")
        helpm.addAction("About", self.about)

    def _build_toolbar(self) -> None:
        tb = self.toolbar = self.addToolBar("Main")
        tb.setObjectName("main")
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        tb.setMovable(False)
        tb.addAction(self.undo_action)
        tb.addAction(self.redo_action)
        tb.addSeparator()
        for act in (self.live_action, self.snapshot_action, self.media_action, self.clock_action, self.pause_action):
            tb.addAction(act)

    def _build_tray(self) -> None:
        self.tray = QtWidgets.QSystemTrayIcon(app_icon(), self)
        m = QtWidgets.QMenu()
        m.addAction("Show window", self.show_window)
        m.addAction(self.live_action)
        m.addAction(self.clock_action)
        m.addAction(self.snapshot_action)
        m.addAction(self.pause_action)
        m.addSeparator()
        m.addAction("Quit", self.quit)
        self.tray.setContextMenu(m)
        self.tray.activated.connect(
            lambda reason: self.show_window() if reason == QtWidgets.QSystemTrayIcon.ActivationReason.Trigger else None
        )
        self.tray.setToolTip(APP_NAME)
        self.tray.show()

    # view -------------------------------------------------------------------

    def _on_zoom(self, _index: int) -> None:
        self.zoom = self.settings.zoom = self.zoom_box.currentData() or 0
        self.settings.save()
        self.canvas.resize_for_geometry()
        self.canvas.update()

    def _on_grid(self, on: bool) -> None:
        self.show_grid = self.settings.show_grid = on
        self.settings.save()
        self.canvas.update()

    def _on_snap(self, on: bool) -> None:
        self.snap = self.settings.snap = on
        self.settings.save()

    # layout editing ---------------------------------------------------------

    def _refresh_list(self, select: int | None = None) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        widgets = self.layout["widgets"]
        # Show the topmost widget first.
        for i in range(len(widgets) - 1, -1, -1):
            w = widgets[i]
            t = TYPES.get(w["type"])
            p = w.get("props", {})
            detail = (
                p.get("text")
                or p.get("label")
                or p.get("city")
                or p.get("command")
                or METRICS.get(p.get("metric", ""))
                or Path(p.get("path", "")).name
                or p.get("format")
            )
            item = QtWidgets.QListWidgetItem(
                f"{t.title if t else w['type']}{': ' + str(detail)[:30] if detail else ''}"
            )
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked if w.get("hidden") else Qt.CheckState.Checked)
            self.list.addItem(item)
        if select is not None:
            self.list.setCurrentRow(len(widgets) - 1 - select)
        self.list.blockSignals(False)

    def _on_item_changed(self, item: QtWidgets.QListWidgetItem) -> None:
        widgets = self.layout["widgets"]
        i = len(widgets) - 1 - self.list.row(item)
        hidden = item.checkState() != Qt.CheckState.Checked
        if 0 <= i < len(widgets) and bool(widgets[i].get("hidden")) != hidden:
            widgets[i]["hidden"] = hidden
            if self.props.index == i:
                self.props.show_widget(i)
            self.changed()

    def align_widget(self, how: str) -> None:
        i = self.canvas.sel
        if i is None:
            return
        w, g = self.layout["widgets"][i], self.geometry
        if how == "hcenter":
            w["x"] = (g.width - w["w"]) // 2
        elif how == "vcenter":
            w["y"] = (g.height - w["h"]) // 2
        elif how == "left":
            w["x"] = 0
        elif how == "right":
            w["x"] = g.width - w["w"]
        elif how == "top":
            w["y"] = 0
        elif how == "bottom":
            w["y"] = g.height - w["h"]
        elif how == "fill":
            w["x"], w["y"], w["w"], w["h"] = 0, 0, g.width, g.height
        self.props.refresh_geometry()
        self.changed()

    # undo ---------------------------------------------------------------------

    def _record(self) -> None:
        """Adds the layout to the undo history; quick edits of one widget (typing, dragging a spin box) merge."""
        snap = json.dumps(self.layout)
        if snap == self._history[self._hist_pos]:
            return
        now = time.monotonic()
        merge = now - self._hist_time < 0.8 and self._hist_sel == self.canvas.sel and self._hist_pos > 0
        del self._history[self._hist_pos + (0 if merge else 1) :]
        self._history.append(snap)
        del self._history[:-HISTORY]
        self._hist_pos = len(self._history) - 1
        self._hist_time, self._hist_sel = now, self.canvas.sel
        self._update_undo()

    def _restore(self, pos: int) -> None:
        self._hist_pos = pos
        self._hist_time = 0.0
        self.layout = json.loads(self._history[pos])
        sel = self.canvas.sel if self.canvas.sel is not None and self.canvas.sel < len(self.layout["widgets"]) else None
        self._refresh_list(sel)
        self._select(sel, from_canvas=True)
        self.changed(record=False)
        self._update_undo()

    def undo(self) -> None:
        if self._hist_pos > 0:
            self._restore(self._hist_pos - 1)

    def redo(self) -> None:
        if self._hist_pos + 1 < len(self._history):
            self._restore(self._hist_pos + 1)

    def _reset_history(self) -> None:
        self._history, self._hist_pos = [json.dumps(self.layout)], 0
        self._update_undo()

    def _update_undo(self) -> None:
        self.undo_action.setEnabled(self._hist_pos > 0)
        self.redo_action.setEnabled(self._hist_pos + 1 < len(self._history))

    def _select(self, index: int | None, from_canvas: bool = False) -> None:
        widgets = self.layout["widgets"]
        if index is not None and not from_canvas:
            index = len(widgets) - 1 - index  # list rows are reversed
        if index is not None and not 0 <= index < len(widgets):
            index = None
        self.canvas.sel = index
        if from_canvas:
            self.list.blockSignals(True)
            self.list.setCurrentRow(-1 if index is None else len(widgets) - 1 - index)
            self.list.blockSignals(False)
        elif index is None:
            self.list.blockSignals(True)
            self.list.setCurrentRow(-1)
            self.list.blockSignals(False)
        self.props.show_widget(index)
        self.canvas.update()

    def add_widget(self, type_name: str) -> None:
        self.layout["widgets"].append(new_widget(type_name, 20, 20))
        i = len(self.layout["widgets"]) - 1
        self._refresh_list(i)
        self._select(i, from_canvas=True)
        self.changed()

    def remove_widget(self) -> None:
        i = self.canvas.sel
        if i is None:
            return
        del self.layout["widgets"][i]
        self._refresh_list()
        self._select(None)
        self.changed()

    def duplicate_widget(self) -> None:
        i = self.canvas.sel
        if i is None:
            return
        w = copy.deepcopy(self.layout["widgets"][i])
        w["x"] += 6
        w["y"] += 6
        self.layout["widgets"].insert(i + 1, w)
        self._refresh_list(i + 1)
        self._select(i + 1, from_canvas=True)
        self.changed()

    def move_widget(self, delta: int) -> None:
        i = self.canvas.sel
        widgets = self.layout["widgets"]
        if i is None or not 0 <= i + delta < len(widgets):
            return
        widgets[i], widgets[i + delta] = widgets[i + delta], widgets[i]
        self._refresh_list(i + delta)
        self._select(i + delta, from_canvas=True)
        self.changed()

    def changed(self, record: bool = True) -> None:
        """Called after every edit: preview, autosave, undo history and live mode pick it up."""
        if record:
            self._record()
        self.canvas.update()
        self._save_timer.start()
        self._live_layout = copy.deepcopy(self.layout)  # live render thread reads this reference
        self.hub.configure(self.layout)
        if self.canvas.sel is not None:
            self._refresh_list(self.canvas.sel)

    def _save(self) -> None:
        try:
            save_layout(self.layout, geometry_key=self.geometry.key)
        except OSError as e:
            self._append_log(f"could not save the layout: {e}")

    # device actions -----------------------------------------------------------

    def _done(self, what: str, then=None):
        def cb(err, res):
            if err:
                self.notify(f"{what}: {err}", error=True)
            else:
                self.notify(f"{what}: done")
            if then:
                then(err, res)

        return lambda err, res: self.bridge.job_done.emit(cb, err, res)

    def set_live(self, on: bool) -> None:
        self.settings.live = on
        self.settings.save()
        self.ctl.set_live(
            on,
            lambda geometry: renderer(lambda: self._live_layout, self.hub.snapshot, geometry),
            {"rdm": self.settings.live_config, "tft": self.settings.refresh_config},
        )
        if on and self.driver is not None and self.driver.family == "tft":
            minutes = self.settings.refresh_config.interval_s / 60
            self._append_log(
                f"live mode on: the picture in bank {self.settings.refresh_config.bank + 1} is redrawn every "
                f"{minutes:g} min when it changed; the keyboard's system page gets CPU/RAM/disk every 2 s"
            )
            if not self._told_about_tft_live:
                self._told_about_tft_live = True
                QtWidgets.QMessageBox.information(
                    self,
                    "Live mode on this keyboard",
                    f"<p>The {self.driver.name} stores pictures in flash memory, so the app can only redraw the "
                    f"picture now and then: every {minutes:g} minutes (Settings). Faster would wear the flash out "
                    "within months, and each redraw takes a few seconds.</p>"
                    "<p><b>For values that change in real time, use the keyboard's own pages:</b> the clock, and the "
                    "system page with CPU, temperature, RAM and disk, which the app updates every 2 seconds. "
                    "The keyboard draws them itself, without touching the flash.</p>"
                    "<p>The picture appears on the picture page; with an animation stored, the keyboard "
                    "switches back to the animation after a while.</p>",
                )
        elif on:
            self._append_log("live mode on (the screen is erased once at start, which takes a few seconds)")

    def _stop_live_for_upload(self) -> None:
        if self.live_action.isChecked():
            self.live_action.setChecked(False)

    def send_snapshot(self) -> None:
        self._stop_live_for_upload()
        g = self.geometry
        frame = to_rgb(render_layout(self.layout, Context(time.time(), self.hub.snapshot()), geometry=g), g)
        self.ctl.submit(
            "snapshot upload", lambda s: s.upload([Animation([frame], 1)]), self._done("Snapshot sent to the keyboard")
        )

    def upload_media(self) -> None:
        caps = self.driver.caps if self.driver is not None else None
        if caps is None:
            QtWidgets.QMessageBox.information(self, APP_NAME, "Connect a keyboard with a screen first.")
            return
        slots, name = caps.slots, self.driver.name
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self,
            f"Up to {slots} pictures or GIFs",
            str(Path.home()),
            "Images and animations (*.gif *.png *.jpg *.jpeg *.webp *.bmp)",
        )
        if not paths:
            return
        if len(paths) > slots:
            QtWidgets.QMessageBox.warning(
                self, APP_NAME, f"{name} stores up to {slots} pictures; the first {slots} will be uploaded."
            )
            paths = paths[:slots]
        g = self.geometry
        fit, ok = QtWidgets.QInputDialog.getItem(
            self, "Scaling", f"How to fit into {g.width}×{g.height}:", list(FIT_MODES.values()), 0, False
        )
        if not ok:
            return
        mode = next(k for k, v in FIT_MODES.items() if v == fit)
        items = []
        try:
            QtWidgets.QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            for p in paths:
                frames, fps = load_frames(p, mode, caps.max_frames if self.settings.limit_frames else 0, g)
                items.append(Animation([to_rgb(f, g) for f in frames], fps))
                self._append_log(f"{Path(p).name}: {len(frames)} frame(s), {fps} fps")
        except (OSError, ValueError) as e:
            QtWidgets.QMessageBox.critical(self, APP_NAME, str(e))
            return
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        animations = sum(1 for i in items if len(i.frames) > 1)
        if animations > caps.max_animations or (animations and caps.max_animations == 1 and len(items) > 1):
            QtWidgets.QMessageBox.warning(
                self, APP_NAME, f"{name} takes either one animation or up to {slots} still pictures."
            )
            return
        self._stop_live_for_upload()
        self.ctl.submit("picture upload", lambda s: s.upload(items), self._done("Pictures uploaded"))

    def sync_time(self) -> None:
        self.ctl.sync_now(self._done("Keyboard clock set"))

    def _tft_driver(self):
        if self.driver is None or self.driver.family != "tft" or self.driver.model is None:
            QtWidgets.QMessageBox.information(self, APP_NAME, "This needs a connected TFT keyboard (RT100 Pro, ...).")
            return None
        return self.driver

    def upload_to_bank(self) -> None:
        d = self._tft_driver()
        if d is None:
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Picture", str(Path.home()), "Images (*.png *.jpg *.jpeg *.webp *.bmp *.gif)"
        )
        if not path:
            return
        bank, ok = QtWidgets.QInputDialog.getInt(
            self, "Picture bank", f"Bank (1–{d.caps.slots}); the others stay as they are:", 1, 1, d.caps.slots
        )
        if not ok:
            return
        g = self.geometry
        try:
            frames, _fps = load_frames(path, "contain", 1, g)
        except (OSError, ValueError) as e:
            QtWidgets.QMessageBox.critical(self, APP_NAME, str(e))
            return
        frame = to_rgb(frames[0], g)
        self._stop_live_for_upload()
        self.ctl.submit(
            "picture upload",
            lambda s: s.upload_still(frame, bank - 1),
            self._done(f"{Path(path).name} stored in bank {bank}"),
        )

    def toggle_language(self) -> None:
        if self._tft_driver() is not None:
            self.ctl.submit("language", lambda s: s.toggle_language(), self._done("Screen language switched"))

    def reset_screen(self) -> None:
        if self._tft_driver() is None:
            return
        answer = QtWidgets.QMessageBox.question(
            self,
            "Clear pictures",
            "Write a black picture into every bank and a black animation? The clock and system pages stay.",
        )
        if answer == QtWidgets.QMessageBox.StandardButton.Yes:
            self._stop_live_for_upload()
            self.ctl.submit("clear pictures", lambda s: s.reset_screen(), self._done("Pictures cleared"))

    def erase_screen(self) -> None:
        d = self._tft_driver()
        if d is None:
            return
        verified = d.model.extra.get("erase_verified")
        box = QtWidgets.QMessageBox(self)
        box.setWindowTitle("Erase screen memory")
        box.setIcon(QtWidgets.QMessageBox.Icon.Question if verified else QtWidgets.QMessageBox.Icon.Warning)
        box.setText(erase_notice(d.model).replace("\n", " "))
        erase = box.addButton("Erase", QtWidgets.QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QtWidgets.QMessageBox.StandardButton.Cancel)
        box.exec()
        if box.clickedButton() is not erase:
            return
        self._stop_live_for_upload()
        self.ctl.submit("erase screen memory", lambda s: s.erase_screen_flash(), self._done("Screen memory erased"))

    def keyboard_info(self) -> None:
        d = self.driver
        if d is None:
            QtWidgets.QMessageBox.information(self, APP_NAME, "No keyboard connected.")
            return

        def show(err, res):
            rows = [("Model", d.name), ("Family", d.family.upper()), ("USB path", d.usb_path or "—")]
            if d.model is not None:
                rows.append(("USB id", f"{d.model.vid:04x}:{d.model.pid:04x}"))
            caps = d.caps
            if caps is not None:
                rows += [
                    ("Screen", f"{caps.width}×{caps.height}"),
                    ("Pictures", str(caps.slots)),
                    ("Animation frames", str(caps.max_frames)),
                    ("Verified", "yes" if caps.verified else "not yet"),
                ]
            for key, value in (res or {}).items():
                if key == "firmware" and isinstance(value, int):
                    value = f"{value:#06x}"
                if key not in ("width", "height"):
                    rows.append((key.replace("_", " ").capitalize(), str(value)))
            if err:
                rows.append(("Error", str(err)))
            table = "".join(f"<tr><td><b>{k}</b></td><td>&nbsp;{v}</td></tr>" for k, v in rows)
            QtWidgets.QMessageBox.information(self, "Keyboard info", f"<table>{table}</table>")

        self.ctl.submit("keyboard info", lambda s: s.info(), lambda err, res: self.bridge.job_done.emit(show, err, res))

    # misc -------------------------------------------------------------------

    def _append_log(self, msg: str) -> None:
        self.log_view.appendPlainText(f"{dt.datetime.now():%H:%M:%S}  {msg}")

    def _on_device(self, driver) -> None:
        """A keyboard was connected, identified or removed: follow its screen size."""
        self.driver = driver
        caps = driver.caps if driver is not None else None
        geometry = Geometry.of(caps) if caps is not None else self.geometry
        if geometry != self.geometry:
            self._save()
            self.geometry = geometry
            self.layout = load_layout(geometry=geometry)
            self.canvas.resize_for_geometry()
            self._refresh_list()
            self._select(None)
            self.changed(record=False)
            self._reset_history()
        if driver is not None:
            note = "" if caps is None or caps.verified else " — not yet verified on this model"
            size = f" {caps.width}×{caps.height}" if caps else ""
            self.setWindowTitle(f"{APP_NAME} — {driver.name}{size}{note}")
            self.hint.setText(self._hint(driver))
        else:
            self.setWindowTitle(APP_NAME)
        self._update_card()
        self._update_keyboard_actions()

    def _update_card(self) -> None:
        d = self.driver
        color = STATE_COLORS.get(self._state, "#9ca3af")
        dot = f'<span style="color:{color}">●</span>'
        if d is None:
            self.card.setText(
                f"{dot} <b>No keyboard</b><br><small>Connect an Epomaker keyboard with a screen by cable.</small>"
            )
            return
        caps = d.caps
        facts = []
        if caps is not None:
            facts.append(f"{caps.width}×{caps.height}")
            facts.append(f"{caps.slots} picture{'s' if caps.slots > 1 else ''}")
            if caps.max_frames > 1:
                facts.append(f"animations up to {caps.max_frames} frames")
            if caps.live == "stream":
                facts.append("live widgets")
            else:
                facts.append(f"widgets redrawn every {self.settings.refresh_config.interval_s / 60:g} min")
                if caps.system_info:
                    facts.append("real-time CPU/RAM on its system page")
            if not caps.verified:
                facts.append("<i>not yet verified on this model</i>")
        self.card.setText(f"{dot} <b>{d.name}</b> — {self._state}<br><small>{' · '.join(facts)}</small>")

    def _update_keyboard_actions(self) -> None:
        d = self.driver
        tft = d is not None and d.family == "tft"
        for act in (self.snapshot_action, self.media_action, self.clock_action, self.info_action):
            act.setEnabled(d is not None)
        for act in (self.bank_action, self.language_action, self.reset_action, self.erase_action):
            act.setEnabled(tft)
            act.setVisible(d is None or tft)

    @staticmethod
    def _hint(driver) -> str:
        if driver.family == "rdm":
            return (
                "On the keyboard: Fn + Right Shift shows the GIF page, where widgets and pictures appear; "
                "Fn + Enter cycles the screen pages."
            )
        keys = "Fn + PgUp / PgDn" if driver.model is not None and driver.model.key == "rt100pro" else "its screen keys"
        return (
            f"Pictures go into the keyboard's picture banks; switch the screen pages with {keys}. With an animation "
            "stored, the keyboard returns to it after a while. Its own clock and system pages are kept up to date."
        )

    def _on_state(self, s: str) -> None:
        name = self.driver.name if self.driver is not None else "Keyboard"
        self.status.setText(f"{name}: {s}")
        self._state = s
        self._update_card()
        self.tray.setToolTip(f"{APP_NAME} — {name}: {s}")
        if s == FROZEN and self._last_state != FROZEN:
            self.notify(
                "The keyboard screen stopped responding. Unplug the cable, switch the keyboard off with its "
                "power switch for ~10 s, then switch it on and plug it back in.",
                error=True,
            )
        self._last_state = s

    def notify(self, text: str, error: bool = False) -> None:
        self._append_log(text)
        if not self.isVisible():
            icon = (
                QtWidgets.QSystemTrayIcon.MessageIcon.Warning
                if error
                else QtWidgets.QSystemTrayIcon.MessageIcon.Information
            )
            self.tray.showMessage(APP_NAME, text, icon, 4000)
        elif error:
            self.statusBar().showMessage(text, 8000)

    def open_layout(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Open layout", str(Path.home()), "Screen layout (*.json)")
        if path:
            layout = load_layout(Path(path), self.geometry)
            commands = commands_in(layout)
            if commands and (
                QtWidgets.QMessageBox.warning(
                    self,
                    "Open layout",
                    "This layout runs shell commands every few seconds:\n\n"
                    + "\n".join(commands)
                    + "\n\nOpen it only if you trust these commands.",
                    QtWidgets.QMessageBox.StandardButton.Open | QtWidgets.QMessageBox.StandardButton.Cancel,
                    QtWidgets.QMessageBox.StandardButton.Cancel,
                )
                != QtWidgets.QMessageBox.StandardButton.Open
            ):
                return
            self.layout = layout
            self._refresh_list()
            self._select(None)
            self.changed()

    def save_layout_as(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self,
            "Save layout",
            str(Path.home() / f"epomaker-layout-{self.geometry.key}.json"),
            "Screen layout (*.json)",
        )
        if path:
            save_layout(self.layout, Path(path))

    def export_png(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Export", str(Path.home() / "epomaker-screen.png"), "PNG (*.png)"
        )
        if path:
            g = self.geometry
            img = render_layout(self.layout, Context(time.time(), self.hub.snapshot()), geometry=g)
            img.copy(0, 0, g.width, g.height).save(path)

    def edit_settings(self) -> None:
        d = QtWidgets.QDialog(self)
        d.setWindowTitle("Settings")
        f = QtWidgets.QFormLayout(d)
        tray = QtWidgets.QCheckBox("Keep running in the system tray when the window is closed")
        tray.setChecked(self.settings.tray_on_close)
        tray.setToolTip("When off, closing the window quits the app (the clock is then only set while it runs).")
        sync = QtWidgets.QCheckBox("Set the keyboard clock on connect and after suspend")
        sync.setChecked(self.settings.auto_sync_time)
        hours = QtWidgets.QDoubleSpinBox(suffix=" h", minimum=0.25, maximum=168, value=self.settings.resync_hours)
        limit = QtWidgets.QCheckBox("Thin out long animations to what Epomaker's software allows for the keyboard")
        limit.setChecked(self.settings.limit_frames)
        lc = self.settings.live_config
        fps = QtWidgets.QSpinBox(minimum=1, maximum=10, value=lc.fps)
        fps.setToolTip("Frames per second in live mode. 1 is gentlest on the flash; 2+ scrolls text more smoothly.")
        area = QtWidgets.QSpinBox(minimum=4, maximum=28, value=lc.area_blocks, suffix=" × 64 KB")
        area.setToolTip("Flash used for the stream. More means fewer erases (less wear).")
        seg = QtWidgets.QSpinBox(minimum=30, maximum=3600, value=int(lc.segment_seconds), suffix=" s")
        seg.setToolTip("How often to start a fresh stream, which resets the screen's playback drift.")
        from .models import MODELS

        device = QtWidgets.QComboBox()
        device.addItem("The first one connected", "")
        for m in MODELS:
            device.addItem(m.name + ("" if m.verified else " (not yet verified)"), m.key)
        device.setCurrentIndex(max(0, device.findData(self.settings.device)))
        device.setToolTip("Which keyboard to use when several are connected.")
        f.addRow("Keyboard", device)
        f.addRow(tray)
        f.addRow(sync)
        f.addRow("Set the clock again every", hours)
        f.addRow(limit)
        f.addRow(QtWidgets.QLabel("<b>Live mode</b>"))
        f.addRow("Frames per second", fps)
        f.addRow("Flash area", area)
        f.addRow("New stream every", seg)
        rc = self.settings.refresh_config
        interval = QtWidgets.QSpinBox(
            minimum=int(MIN_REFRESH_S), maximum=86400, value=int(max(MIN_REFRESH_S, rc.interval_s)), suffix=" s"
        )
        interval.setToolTip(
            "RT100 Pro and other TFT keyboards: how often live mode redraws the picture. "
            "Each change rewrites a picture in the keyboard's flash, which wears it out: every 15 minutes lasts "
            "years, every minute only months."
        )
        sysinfo = QtWidgets.QCheckBox("Keep the keyboard's own system page up to date (CPU, RAM, disk)")
        sysinfo.setChecked(self.settings.system_info)
        f.addRow(QtWidgets.QLabel("<b>RT100 Pro and other TFT keyboards</b>"))
        f.addRow("Redraw live widgets every", interval)
        f.addRow(sysinfo)
        bb = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(d.accept)
        bb.rejected.connect(d.reject)
        f.addRow(bb)
        if d.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        self.settings.device = device.currentData()
        self.ctl.set_prefer(self.settings.device)
        self.settings.tray_on_close = tray.isChecked()
        self.settings.limit_frames = limit.isChecked()
        self.settings.auto_sync_time = sync.isChecked()
        self.settings.resync_hours = hours.value()
        self.settings.live_config = LiveConfig(
            fps=fps.value(),
            area_blocks=area.value(),
            segment_seconds=seg.value(),
            lead_frames=lc.lead_frames,
            slowdown=lc.slowdown,
        )
        self.settings.refresh_config = RefreshConfig(interval_s=interval.value(), bank=rc.bank)
        self.settings.system_info = sysinfo.isChecked()
        self.ctl.set_system_info(FirmwareSystemInfo() if self.settings.system_info else None)
        self.settings.save()
        self.ctl.auto_sync = self.settings.auto_sync_time
        self.ctl.resync_s = self.settings.resync_hours * 3600
        if self.live_action.isChecked():
            self.set_live(True)  # restart with the new parameters

    def about(self) -> None:
        QtWidgets.QMessageBox.about(
            self,
            APP_NAME,
            f"<b>{APP_NAME} {__version__}</b><br>Screens of Epomaker keyboards on Linux, without a browser.<br><br>"
            "Protocol research by guysoft/rt82display, wyattades/keyscreen and ramarivera/epomaker-driver-linux.",
        )

    def show_window(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, e):
        if self.quitting:
            e.accept()
            return
        if not self.settings.tray_on_close or not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            e.accept()
            self.quit()
            return
        e.ignore()
        self.hide()
        if not self._told_about_tray:
            self._told_about_tray = True
            self.tray.showMessage(
                APP_NAME,
                "Still running in the system tray. Change this in File → Settings.",
                QtWidgets.QSystemTrayIcon.MessageIcon.Information,
                3000,
            )

    def _shutdown(self) -> None:
        self._save()
        self.hub.stop()
        self.ctl.shutdown()

    def quit(self) -> None:
        if self.quitting:
            return
        self.quitting = True
        self._shutdown()
        self.tray.hide()
        self.app.quit()


def app_icon() -> QtGui.QIcon:
    icon = QtGui.QIcon.fromTheme(APP_ID)
    if icon.isNull():
        icon = QtGui.QIcon.fromTheme("input-keyboard")
    if icon.isNull():
        here = Path(__file__).resolve().parent / "resources" / f"{APP_ID}.svg"
        icon = QtGui.QIcon(str(here))
    return icon


def main() -> int:
    QtWidgets.QApplication.setApplicationName(APP_ID)
    QtWidgets.QApplication.setApplicationDisplayName(APP_NAME)
    QtWidgets.QApplication.setDesktopFileName(APP_ID)
    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    win = MainWindow(app)
    if "--tray" not in sys.argv or not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
        win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
