"""RT82 Screen: KDE Plasma app for the Epomaker RT82 keyboard screen.

Lives in the system tray, keeps the keyboard clock in sync and lets you
lay out widgets on the 240x135 screen.
"""

from __future__ import annotations

import copy
import datetime as dt
import sys
import time
from pathlib import Path

from . import __version__, qgif
from .config import Settings, load_layout, save_layout
from .live import LiveConfig
from .protocol import HEIGHT, MAX_FRAMES, MAX_SLOTS, QGIF_HEIGHT, VERIFIED_UPLOAD_BYTES, WIDTH
from .qt import QtCore, QtGui, QtWidgets, Signal
from .render import FIT_MODES, load_frames, to_rgb
from .service import FROZEN, Controller
from .sources import DataHub
from .widgets import METRICS, TYPES, Context, Prop, new_widget, props_of, render_layout, renderer

Qt = QtCore.Qt
SCALE = 3
HANDLE = 6  # resize handle, screen pixels
APP_ID = "rt82-plasma"


class Bridge(QtCore.QObject):
    """Moves controller callbacks (worker thread) onto the GUI thread."""

    log = Signal(str)
    state = Signal(str)
    job_done = Signal(object, object, object)  # callback, error, result


# -- screen canvas -------------------------------------------------------------


class Canvas(QtWidgets.QWidget):
    selected = Signal(int)
    geometry_changed = Signal()

    def __init__(self, window: MainWindow):
        super().__init__()
        self.win = window
        self.sel: int | None = None
        self._drag: tuple[str, QtCore.QPointF, dict] | None = None
        self.setFixedSize(WIDTH * SCALE + 2, HEIGHT * SCALE + 2)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip(
            "Click a widget to select it; drag to move it, drag its bottom-right corner to resize.\n"
            "Arrow keys move by 1 px (with Shift: resize). Delete removes it."
        )

    def paintEvent(self, e):
        img = render_layout(self.win.layout, Context(time.time(), self.win.hub.snapshot(), preview=True), self.sel)
        p = QtGui.QPainter(self)
        p.fillRect(self.rect(), QtGui.QColor("#000000"))
        p.drawImage(QtCore.QRectF(1, 1, WIDTH * SCALE, HEIGHT * SCALE), img, QtCore.QRectF(0, 0, WIDTH, HEIGHT))
        p.setPen(QtGui.QColor("#555555"))
        p.drawRect(self.rect().adjusted(0, 0, -1, -1))
        p.end()

    def _screen_pos(self, e) -> QtCore.QPointF:
        pos = e.position()
        return QtCore.QPointF((pos.x() - 1) / SCALE, (pos.y() - 1) / SCALE)

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
        if mode == "move":
            w["x"] = max(-orig["w"] + 4, min(WIDTH - 4, orig["x"] + dx))
            w["y"] = max(-orig["h"] + 4, min(HEIGHT - 4, orig["y"] + dy))
        else:
            w["w"] = max(4, orig["w"] + dx)
            w["h"] = max(4, orig["h"] + dy)
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
        self.index: int | None = None
        self.geo: dict[str, QtWidgets.QSpinBox] = {}
        self.show_widget(None)

    def show_widget(self, index: int | None) -> None:
        self.index = index
        body = QtWidgets.QWidget()
        form = QtWidgets.QFormLayout(body)
        self.geo = {}
        if index is None:
            self._background_form(form)
        else:
            w = self.win.layout["widgets"][index]
            t = TYPES[w["type"]]
            title = QtWidgets.QLabel(f"<b>{t.title}</b>")
            form.addRow(title)
            for key, label, hi in (("x", "X", WIDTH), ("y", "Y", HEIGHT), ("w", "Width", 400), ("h", "Height", 400)):
                sb = QtWidgets.QSpinBox()
                sb.setRange(-400 if key in "xy" else 1, hi)
                sb.setValue(int(w[key]))
                sb.valueChanged.connect(lambda v, key=key: self._set_geo(key, v))
                self.geo[key] = sb
                form.addRow(label, sb)
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
        self.layout = load_layout()
        self._live_layout = copy.deepcopy(self.layout)
        self.hub = DataHub()
        self.hub.configure(self.layout)
        self.hub.start()
        self.quitting = False
        self._last_state = ""
        self._told_about_tray = False

        self.bridge = Bridge()
        self.bridge.log.connect(self._append_log)
        self.bridge.state.connect(self._on_state)
        self.bridge.job_done.connect(lambda cb, err, res: cb(err, res))
        self.ctl = Controller(
            log=self.bridge.log.emit,
            on_state=self.bridge.state.emit,
            auto_sync=self.settings.auto_sync_time,
            resync_hours=self.settings.resync_hours,
        )

        self.setWindowTitle("RT82 Screen")
        self.setWindowIcon(app_icon())
        self._build()
        self._build_tray()

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
        tb = self.addToolBar("Main")
        tb.setObjectName("main")
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

        self.add_menu = QtWidgets.QMenu(self)
        for name, t in TYPES.items():
            self.add_menu.addAction(t.title, lambda name=name: self.add_widget(name))
        add_btn = QtWidgets.QToolButton()
        add_btn.setText("Add widget")
        add_btn.setIcon(QtGui.QIcon.fromTheme("list-add"))
        add_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        add_btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        add_btn.setMenu(self.add_menu)
        tb.addWidget(add_btn)
        tb.addSeparator()

        self.live_action = QtGui.QAction(
            QtGui.QIcon.fromTheme("media-playback-start"), "Live mode", self, checkable=True
        )
        self.live_action.setToolTip("Keep the widgets on the keyboard screen up to date")
        self.live_action.toggled.connect(self.set_live)
        tb.addAction(self.live_action)
        send = tb.addAction(QtGui.QIcon.fromTheme("document-send"), "Send snapshot", self.send_snapshot)
        send.setToolTip("Render the layout once and store it on the keyboard as a picture")
        tb.addAction(QtGui.QIcon.fromTheme("folder-pictures"), "Pictures/GIFs…", self.upload_media)
        tb.addAction(QtGui.QIcon.fromTheme("chronometer"), "Set clock", self.sync_time)
        self.pause_action = QtGui.QAction(QtGui.QIcon.fromTheme("media-playback-pause"), "Pause", self, checkable=True)
        self.pause_action.setToolTip("Release the keyboard, e.g. to use Epomaker Hub in a browser")
        self.pause_action.toggled.connect(self.ctl.set_paused)
        tb.addAction(self.pause_action)

        menu = self.menuBar().addMenu("&File")
        menu.addAction("Open layout…", self.open_layout)
        menu.addAction("Save layout as…", self.save_layout_as)
        menu.addAction("Export preview PNG…", self.export_png)
        menu.addSeparator()
        menu.addAction(QtGui.QIcon.fromTheme("configure"), "Settings…", self.edit_settings)
        menu.addSeparator()
        menu.addAction(QtGui.QIcon.fromTheme("application-exit"), "Quit", self.quit)
        helpm = self.menuBar().addMenu("&Help")
        helpm.addAction("About", self.about)

        self.canvas = Canvas(self)
        self.canvas.selected.connect(lambda i: self._select(None if i < 0 else i, from_canvas=True))
        self.canvas.geometry_changed.connect(lambda: self.props.refresh_geometry())

        self.list = QtWidgets.QListWidget()
        self.list.currentRowChanged.connect(lambda r: self._select(r if r >= 0 else None))
        btns = QtWidgets.QHBoxLayout()
        for icon, tip, fn in (
            ("go-up", "Raise (draw on top)", lambda: self.move_widget(1)),
            ("go-down", "Lower", lambda: self.move_widget(-1)),
            ("edit-copy", "Duplicate", self.duplicate_widget),
            ("list-remove", "Remove", self.remove_widget),
            ("edit-select-none", "Screen background", lambda: self._select(None)),
        ):
            b = QtWidgets.QToolButton()
            b.setIcon(QtGui.QIcon.fromTheme(icon))
            b.setToolTip(tip)
            b.clicked.connect(fn)
            btns.addWidget(b)
        btns.addStretch()

        self.props = PropertyPanel(self)

        left = QtWidgets.QVBoxLayout()
        left.addWidget(self.canvas, 0, Qt.AlignmentFlag.AlignTop)
        hint = QtWidgets.QLabel(
            "On the keyboard: Fn + Right Shift shows the GIF page, where widgets and pictures "
            "appear; Fn + Enter cycles the screen pages."
        )
        hint.setWordWrap(True)
        hint.setMaximumWidth(WIDTH * SCALE)
        left.addWidget(hint)
        self.log_view = QtWidgets.QPlainTextEdit(readOnly=True)
        self.log_view.setMaximumBlockCount(500)
        self.log_view.setPlaceholderText("Log")
        left.addWidget(self.log_view, 1)

        right = QtWidgets.QVBoxLayout()
        right.addWidget(QtWidgets.QLabel("Widgets (topmost first):"))
        right.addWidget(self.list, 1)
        right.addLayout(btns)
        right.addWidget(self.props, 2)

        central = QtWidgets.QWidget()
        h = QtWidgets.QHBoxLayout(central)
        h.addLayout(left)
        rw = QtWidgets.QWidget()
        rw.setLayout(right)
        rw.setMinimumWidth(320)
        h.addWidget(rw, 1)
        self.setCentralWidget(central)
        self.status = QtWidgets.QLabel("…")
        self.statusBar().addPermanentWidget(self.status)
        self._refresh_list()
        self.resize(1180, 760)

    def _build_tray(self) -> None:
        self.tray = QtWidgets.QSystemTrayIcon(app_icon(), self)
        m = QtWidgets.QMenu()
        m.addAction("Show window", self.show_window)
        m.addAction(self.live_action)
        m.addAction("Set keyboard clock", self.sync_time)
        m.addAction("Send layout snapshot", self.send_snapshot)
        m.addAction(self.pause_action)
        m.addSeparator()
        m.addAction("Quit", self.quit)
        self.tray.setContextMenu(m)
        self.tray.activated.connect(
            lambda reason: self.show_window() if reason == QtWidgets.QSystemTrayIcon.ActivationReason.Trigger else None
        )
        self.tray.setToolTip("RT82 Screen")
        self.tray.show()

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
            if w.get("hidden"):
                item.setForeground(QtGui.QColor("#888888"))
            self.list.addItem(item)
        if select is not None:
            self.list.setCurrentRow(len(widgets) - 1 - select)
        self.list.blockSignals(False)

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

    def changed(self) -> None:
        """Called after every edit: preview, autosave, and live mode pick it up."""
        self.canvas.update()
        self._save_timer.start()
        self._live_layout = copy.deepcopy(self.layout)  # live render thread reads this reference
        self.hub.configure(self.layout)
        if self.canvas.sel is not None:
            self._refresh_list(self.canvas.sel)

    def _save(self) -> None:
        try:
            save_layout(self.layout)
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
        self.ctl.set_live(on, lambda: renderer(lambda: self._live_layout, self.hub.snapshot), self.settings.live_config)
        if on:
            self._append_log("live mode on (the screen is erased once at start, which takes a few seconds)")

    def _stop_live_for_upload(self) -> None:
        if self.live_action.isChecked():
            self.live_action.setChecked(False)

    def send_snapshot(self) -> None:
        self._stop_live_for_upload()
        img = render_layout(self.layout, Context(time.time(), self.hub.snapshot()))
        data = qgif.encode([to_rgb(img)], WIDTH, QGIF_HEIGHT, 1)
        self.ctl.submit("snapshot upload", lambda s: s.upload([data]), self._done("Snapshot sent to the keyboard"))

    def upload_media(self) -> None:
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self,
            f"Up to {MAX_SLOTS} pictures or GIFs",
            str(Path.home()),
            "Images and animations (*.gif *.png *.jpg *.jpeg *.webp *.bmp *.qgif)",
        )
        if not paths:
            return
        if len(paths) > MAX_SLOTS:
            QtWidgets.QMessageBox.warning(
                self, "RT82", f"The keyboard stores up to {MAX_SLOTS} screens; the first {MAX_SLOTS} will be uploaded."
            )
            paths = paths[:MAX_SLOTS]
        fit, ok = QtWidgets.QInputDialog.getItem(
            self, "Scaling", "How to fit into 240×135:", list(FIT_MODES.values()), 0, False
        )
        if not ok:
            return
        mode = next(k for k, v in FIT_MODES.items() if v == fit)
        images = []
        try:
            QtWidgets.QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)
            for p in paths:
                if p.endswith(".qgif"):
                    images.append(Path(p).read_bytes())
                    continue
                frames, fps = load_frames(p, mode, MAX_FRAMES if self.settings.limit_frames else 0)
                images.append(qgif.encode([to_rgb(f) for f in frames], WIDTH, QGIF_HEIGHT, fps))
                self._append_log(f"{Path(p).name}: {len(frames)} frame(s), {fps} fps, {len(images[-1]) // 1024} KB")
        except (OSError, ValueError) as e:
            QtWidgets.QMessageBox.critical(self, "RT82", str(e))
            return
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        total = sum(map(len, images))
        if total > VERIFIED_UPLOAD_BYTES:
            answer = QtWidgets.QMessageBox.question(
                self,
                "RT82",
                f"{total // 1024} KB is more than has been tried on an RT82. "
                "If the screen shows garbage, use fewer frames. Upload anyway?",
            )
            if answer != QtWidgets.QMessageBox.StandardButton.Yes:
                return
        self._stop_live_for_upload()
        self.ctl.submit("picture upload", lambda s: s.upload(images), self._done("Pictures uploaded"))

    def sync_time(self) -> None:
        self.ctl.sync_now(self._done("Keyboard clock set"))

    # misc -------------------------------------------------------------------

    def _append_log(self, msg: str) -> None:
        self.log_view.appendPlainText(f"{dt.datetime.now():%H:%M:%S}  {msg}")

    def _on_state(self, s: str) -> None:
        self.status.setText(f"Keyboard: {s}")
        self.tray.setToolTip(f"RT82 Screen — {s}")
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
            self.tray.showMessage("RT82 Screen", text, icon, 4000)
        elif error:
            self.statusBar().showMessage(text, 8000)

    def open_layout(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Open layout", str(Path.home()), "RT82 layout (*.json)")
        if path:
            self.layout = load_layout(Path(path))
            self._refresh_list()
            self._select(None)
            self.changed()

    def save_layout_as(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save layout", str(Path.home() / "rt82-layout.json"), "RT82 layout (*.json)"
        )
        if path:
            save_layout(self.layout, Path(path))

    def export_png(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Export", str(Path.home() / "rt82.png"), "PNG (*.png)")
        if path:
            render_layout(self.layout, Context(time.time(), self.hub.snapshot())).copy(0, 0, WIDTH, HEIGHT).save(path)

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
        limit = QtWidgets.QCheckBox(f"Thin out animations to {MAX_FRAMES} frames (Epomaker Hub's limit)")
        limit.setChecked(self.settings.limit_frames)
        lc = self.settings.live_config
        fps = QtWidgets.QSpinBox(minimum=1, maximum=10, value=lc.fps)
        fps.setToolTip("Frames per second in live mode. 1 is gentlest on the flash; 2+ scrolls text more smoothly.")
        area = QtWidgets.QSpinBox(minimum=4, maximum=28, value=lc.area_blocks, suffix=" × 64 KB")
        area.setToolTip("Flash used for the stream. More means fewer erases (less wear).")
        seg = QtWidgets.QSpinBox(minimum=30, maximum=3600, value=int(lc.segment_seconds), suffix=" s")
        seg.setToolTip("How often to start a fresh stream, which resets the screen's playback drift.")
        f.addRow(tray)
        f.addRow(sync)
        f.addRow("Set the clock again every", hours)
        f.addRow(limit)
        f.addRow(QtWidgets.QLabel("<b>Live mode</b>"))
        f.addRow("Frames per second", fps)
        f.addRow("Flash area", area)
        f.addRow("New stream every", seg)
        bb = QtWidgets.QDialogButtonBox(
            QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel
        )
        bb.accepted.connect(d.accept)
        bb.rejected.connect(d.reject)
        f.addRow(bb)
        if d.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
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
        self.settings.save()
        self.ctl.auto_sync = self.settings.auto_sync_time
        self.ctl.resync_s = self.settings.resync_hours * 3600
        if self.live_action.isChecked():
            self.set_live(True)  # restart with the new parameters

    def about(self) -> None:
        QtWidgets.QMessageBox.about(
            self,
            "RT82 Screen",
            f"<b>RT82 Screen {__version__}</b><br>The Epomaker RT82 keyboard screen on Linux, without a browser.<br><br>"
            "Protocol research by guysoft/rt82display and wyattades/keyscreen.",
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
                "RT82 Screen",
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
    QtWidgets.QApplication.setApplicationDisplayName("RT82 Screen")
    QtWidgets.QApplication.setDesktopFileName(APP_ID)
    app = QtWidgets.QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    win = MainWindow(app)
    if "--tray" not in sys.argv or not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
        win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
