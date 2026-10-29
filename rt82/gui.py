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
from typing import Optional

from . import __version__, qgif
from .config import Settings, load_layout, save_layout
from .data import DataHub
from .live import LiveConfig
from .protocol import HEIGHT, MAX_SLOTS, QGIF_HEIGHT, WIDTH
from .qt import QtCore, QtGui, QtWidgets, Signal
from .render import FIT_MODES, load_frames, to_rgb
from .service import Controller
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

    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.win = window
        self.sel: Optional[int] = None
        self._drag: Optional[tuple[str, QtCore.QPointF, dict]] = None
        self.setFixedSize(WIDTH * SCALE + 2, HEIGHT * SCALE + 2)
        self.setMouseTracking(True)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip("Щёлкните виджет, чтобы выбрать; тащите — переместить, за правый нижний угол — изменить размер.\n"
                        "Стрелки сдвигают на 1 px (с Shift — размер).")

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

    def _hit(self, pt: QtCore.QPointF) -> Optional[int]:
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
        d = {Qt.Key.Key_Left: (-1, 0), Qt.Key.Key_Right: (1, 0), Qt.Key.Key_Up: (0, -1), Qt.Key.Key_Down: (0, 1)}.get(e.key())
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
        c = QtWidgets.QColorDialog.getColor(QtGui.QColor(self.color), self, "Цвет")
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
            path, _ = QtWidgets.QFileDialog.getOpenFileName(parent, "Картинка", str(Path.home()), "Изображения (*.png *.jpg *.jpeg *.gif *.webp *.svg *.bmp)")
            if path:
                edit.setText(path)
                on_change(path)

        btn.clicked.connect(browse)
    else:
        w = QtWidgets.QLineEdit(str(value))
        w.textChanged.connect(on_change)
    return w


class PropertyPanel(QtWidgets.QScrollArea):
    def __init__(self, window: "MainWindow"):
        super().__init__()
        self.win = window
        self.setWidgetResizable(True)
        self.index: Optional[int] = None
        self.geo: dict[str, QtWidgets.QSpinBox] = {}
        self.show_widget(None)

    def show_widget(self, index: Optional[int]) -> None:
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
            for key, label, hi in (("x", "X", WIDTH), ("y", "Y", HEIGHT), ("w", "Ширина", 400), ("h", "Высота", 400)):
                sb = QtWidgets.QSpinBox()
                sb.setRange(-400 if key in "xy" else 1, hi)
                sb.setValue(int(w[key]))
                sb.valueChanged.connect(lambda v, key=key: self._set_geo(key, v))
                self.geo[key] = sb
                form.addRow(label, sb)
            hidden = QtWidgets.QCheckBox("Скрыть")
            hidden.setChecked(bool(w.get("hidden")))
            hidden.toggled.connect(lambda v: self._set_raw("hidden", v))
            form.addRow(hidden)
            props = props_of(w)
            for prop in t.props:
                form.addRow(prop.label, editor_for(prop, props[prop.key], lambda v, k=prop.key: self._set_prop(k, v), self))
        self.setWidget(body)

    def _background_form(self, form: QtWidgets.QFormLayout) -> None:
        L = self.win.layout
        form.addRow(QtWidgets.QLabel("<b>Фон экрана</b><br><small>Выберите виджет на экране, чтобы изменить его.</small>"))
        form.addRow("Цвет", editor_for(Prop("background", "", "color", ""), L.get("background", "#000000"), lambda v: self._set_layout("background", v), self))
        form.addRow("Картинка", editor_for(Prop("background_image", "", "file", ""), L.get("background_image", ""), lambda v: self._set_layout("background_image", v), self))
        form.addRow("Масштаб", editor_for(Prop("background_mode", "", "choice", "cover", FIT_MODES), L.get("background_mode", "cover"), lambda v: self._set_layout("background_mode", v), self))

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
        tb = self.addToolBar("Главное")
        tb.setObjectName("main")
        tb.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)

        self.add_menu = QtWidgets.QMenu(self)
        for name, t in TYPES.items():
            self.add_menu.addAction(t.title, lambda name=name: self.add_widget(name))
        add_btn = QtWidgets.QToolButton()
        add_btn.setText("Добавить виджет")
        add_btn.setIcon(QtGui.QIcon.fromTheme("list-add"))
        add_btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
        add_btn.setPopupMode(QtWidgets.QToolButton.ToolButtonPopupMode.InstantPopup)
        add_btn.setMenu(self.add_menu)
        tb.addWidget(add_btn)
        tb.addSeparator()

        self.live_action = QtGui.QAction(QtGui.QIcon.fromTheme("media-playback-start"), "Живой режим", self, checkable=True)
        self.live_action.setToolTip("Виджеты обновляются на клавиатуре постоянно (без «Downloading»)")
        self.live_action.toggled.connect(self.set_live)
        tb.addAction(self.live_action)
        send = tb.addAction(QtGui.QIcon.fromTheme("document-send"), "Отправить снимок", self.send_snapshot)
        send.setToolTip("Отрисовать раскладку один раз и сохранить на клавиатуре картинкой")
        tb.addAction(QtGui.QIcon.fromTheme("folder-pictures"), "GIF/картинки…", self.upload_media)
        tb.addAction(QtGui.QIcon.fromTheme("chronometer"), "Время", self.sync_time)
        self.pause_action = QtGui.QAction(QtGui.QIcon.fromTheme("media-playback-pause"), "Пауза", self, checkable=True)
        self.pause_action.setToolTip("Отпустить клавиатуру, например, чтобы открыть Epomaker Hub в браузере")
        self.pause_action.toggled.connect(self.ctl.set_paused)
        tb.addAction(self.pause_action)

        menu = self.menuBar().addMenu("&Файл")
        menu.addAction("Открыть раскладку…", self.open_layout)
        menu.addAction("Сохранить раскладку как…", self.save_layout_as)
        menu.addAction("Экспорт превью PNG…", self.export_png)
        menu.addSeparator()
        menu.addAction("Настройки…", self.edit_settings)
        menu.addSeparator()
        menu.addAction(QtGui.QIcon.fromTheme("application-exit"), "Выход", self.quit)
        helpm = self.menuBar().addMenu("&Справка")
        helpm.addAction("О программе", self.about)

        self.canvas = Canvas(self)
        self.canvas.selected.connect(lambda i: self._select(None if i < 0 else i, from_canvas=True))
        self.canvas.geometry_changed.connect(lambda: self.props.refresh_geometry())

        self.list = QtWidgets.QListWidget()
        self.list.currentRowChanged.connect(lambda r: self._select(r if r >= 0 else None))
        btns = QtWidgets.QHBoxLayout()
        for icon, tip, fn in (
            ("go-up", "Выше (поверх)", lambda: self.move_widget(1)),
            ("go-down", "Ниже", lambda: self.move_widget(-1)),
            ("edit-copy", "Дублировать", self.duplicate_widget),
            ("list-remove", "Удалить", self.remove_widget),
            ("edit-select-none", "Фон экрана", lambda: self._select(None)),
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
        hint = QtWidgets.QLabel("На клавиатуре: Fn + правый Shift — страница GIF, где показываются виджеты; "
                                "Fn + Enter — переключить страницы экрана.")
        hint.setWordWrap(True)
        hint.setMaximumWidth(WIDTH * SCALE)
        left.addWidget(hint)
        self.log_view = QtWidgets.QPlainTextEdit(readOnly=True)
        self.log_view.setMaximumBlockCount(500)
        self.log_view.setPlaceholderText("Журнал")
        left.addWidget(self.log_view, 1)

        right = QtWidgets.QVBoxLayout()
        right.addWidget(QtWidgets.QLabel("Виджеты (сверху — поверх остальных):"))
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
        m.addAction("Показать окно", self.show_window)
        m.addAction(self.live_action)
        m.addAction("Синхронизировать время", self.sync_time)
        m.addAction("Отправить снимок раскладки", self.send_snapshot)
        m.addAction(self.pause_action)
        m.addSeparator()
        m.addAction("Выход", self.quit)
        self.tray.setContextMenu(m)
        self.tray.activated.connect(lambda reason: self.show_window() if reason == QtWidgets.QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.setToolTip("RT82 Screen")
        self.tray.show()

    # layout editing ---------------------------------------------------------

    def _refresh_list(self, select: Optional[int] = None) -> None:
        self.list.blockSignals(True)
        self.list.clear()
        widgets = self.layout["widgets"]
        # Show the topmost widget first.
        for i in range(len(widgets) - 1, -1, -1):
            w = widgets[i]
            t = TYPES.get(w["type"])
            p = w.get("props", {})
            detail = (p.get("text") or p.get("label") or p.get("city") or p.get("command") or METRICS.get(p.get("metric", ""))
                      or Path(p.get("path", "")).name or p.get("format"))
            item = QtWidgets.QListWidgetItem(f"{t.title if t else w['type']}{': ' + str(detail)[:30] if detail else ''}")
            if w.get("hidden"):
                item.setForeground(QtGui.QColor("#888888"))
            self.list.addItem(item)
        if select is not None:
            self.list.setCurrentRow(len(widgets) - 1 - select)
        self.list.blockSignals(False)

    def _select(self, index: Optional[int], from_canvas: bool = False) -> None:
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
            self._append_log(f"не удалось сохранить раскладку: {e}")

    # device actions -----------------------------------------------------------

    def _done(self, what: str, then=None):
        def cb(err, res):
            if err:
                self.notify(f"{what}: {err}", error=True)
            else:
                self.notify(f"{what}: готово")
            if then:
                then(err, res)

        return lambda err, res: self.bridge.job_done.emit(cb, err, res)

    def set_live(self, on: bool) -> None:
        self.settings.live = on
        self.settings.save()
        self.ctl.set_live(on, lambda: renderer(lambda: self._live_layout, self.hub.snapshot), self.settings.live_config)
        if on:
            self._append_log("живой режим включён (при запуске экран на пару секунд покажет «Downloading»)")

    def _stop_live_for_upload(self) -> None:
        if self.live_action.isChecked():
            self.live_action.setChecked(False)

    def send_snapshot(self) -> None:
        self._stop_live_for_upload()
        img = render_layout(self.layout, Context(time.time(), self.hub.snapshot()))
        data = qgif.encode([to_rgb(img)], WIDTH, QGIF_HEIGHT, 1)
        self.ctl.submit("отправка снимка", lambda s: s.upload([data]), self._done("Снимок отправлен на клавиатуру"))

    def upload_media(self) -> None:
        paths, _ = QtWidgets.QFileDialog.getOpenFileNames(
            self, f"До {MAX_SLOTS} картинок или GIF", str(Path.home()), "Изображения и анимации (*.gif *.png *.jpg *.jpeg *.webp *.bmp *.qgif)"
        )
        if not paths:
            return
        if len(paths) > MAX_SLOTS:
            QtWidgets.QMessageBox.warning(self, "RT82", f"Клавиатура хранит до {MAX_SLOTS} экранов; будут загружены первые {MAX_SLOTS}.")
            paths = paths[:MAX_SLOTS]
        fit, ok = QtWidgets.QInputDialog.getItem(self, "Масштаб", "Как вписать в 240×135:", list(FIT_MODES.values()), 0, False)
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
                frames, fps = load_frames(p, mode)
                images.append(qgif.encode([to_rgb(f) for f in frames], WIDTH, QGIF_HEIGHT, fps))
                self._append_log(f"{Path(p).name}: {len(frames)} кадр(ов), {fps} fps, {len(images[-1]) // 1024} КБ")
        except (OSError, ValueError) as e:
            QtWidgets.QMessageBox.critical(self, "RT82", str(e))
            return
        finally:
            QtWidgets.QApplication.restoreOverrideCursor()
        self._stop_live_for_upload()
        self.ctl.submit("загрузка картинок", lambda s: s.upload(images), self._done("Картинки загружены"))

    def sync_time(self) -> None:
        self.ctl.sync_now(self._done("Время синхронизировано"))

    # misc -------------------------------------------------------------------

    def _append_log(self, msg: str) -> None:
        self.log_view.appendPlainText(f"{dt.datetime.now():%H:%M:%S}  {msg}")

    def _on_state(self, s: str) -> None:
        self.status.setText(f"Клавиатура: {s}")
        self.tray.setToolTip(f"RT82 Screen — {s}")

    def notify(self, text: str, error: bool = False) -> None:
        self._append_log(text)
        if not self.isVisible():
            icon = QtWidgets.QSystemTrayIcon.MessageIcon.Warning if error else QtWidgets.QSystemTrayIcon.MessageIcon.Information
            self.tray.showMessage("RT82 Screen", text, icon, 4000)
        elif error:
            self.statusBar().showMessage(text, 8000)

    def open_layout(self) -> None:
        path, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Открыть раскладку", str(Path.home()), "Раскладка RT82 (*.json)")
        if path:
            self.layout = load_layout(Path(path))
            self._refresh_list()
            self._select(None)
            self.changed()

    def save_layout_as(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Сохранить раскладку", str(Path.home() / "rt82-layout.json"), "Раскладка RT82 (*.json)")
        if path:
            save_layout(self.layout, Path(path))

    def export_png(self) -> None:
        path, _ = QtWidgets.QFileDialog.getSaveFileName(self, "Экспорт", str(Path.home() / "rt82.png"), "PNG (*.png)")
        if path:
            render_layout(self.layout, Context(time.time(), self.hub.snapshot())).copy(0, 0, WIDTH, HEIGHT).save(path)

    def edit_settings(self) -> None:
        d = QtWidgets.QDialog(self)
        d.setWindowTitle("Настройки")
        f = QtWidgets.QFormLayout(d)
        sync = QtWidgets.QCheckBox("Синхронизировать время при подключении и после сна")
        sync.setChecked(self.settings.auto_sync_time)
        hours = QtWidgets.QDoubleSpinBox(suffix=" ч", minimum=0.25, maximum=168, value=self.settings.resync_hours)
        lc = self.settings.live_config
        fps = QtWidgets.QSpinBox(minimum=1, maximum=10, value=lc.fps)
        fps.setToolTip("Кадров в секунду в живом режиме. 1 — бережнее к flash; 2+ — плавнее бегущая строка.")
        area = QtWidgets.QSpinBox(minimum=4, maximum=28, value=lc.area_blocks, suffix=" × 64 КБ")
        area.setToolTip("Сколько flash отводится под поток. Больше — реже стирание (меньше износ).")
        seg = QtWidgets.QSpinBox(minimum=30, maximum=3600, value=int(lc.segment_seconds), suffix=" с")
        seg.setToolTip("Как часто начинать новый поток, чтобы сбросить отставание экрана.")
        f.addRow(sync)
        f.addRow("Повторная синхронизация каждые", hours)
        f.addRow(QtWidgets.QLabel("<b>Живой режим</b>"))
        f.addRow("Кадров в секунду", fps)
        f.addRow("Область flash", area)
        f.addRow("Новый поток каждые", seg)
        bb = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Ok | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(d.accept)
        bb.rejected.connect(d.reject)
        f.addRow(bb)
        if d.exec() != QtWidgets.QDialog.DialogCode.Accepted:
            return
        self.settings.auto_sync_time = sync.isChecked()
        self.settings.resync_hours = hours.value()
        self.settings.live_config = LiveConfig(fps=fps.value(), area_blocks=area.value(), segment_seconds=seg.value(),
                                               lead_frames=lc.lead_frames, slowdown=lc.slowdown)
        self.settings.save()
        self.ctl.auto_sync = self.settings.auto_sync_time
        self.ctl.resync_s = self.settings.resync_hours * 3600
        if self.live_action.isChecked():
            self.set_live(True)  # restart with the new parameters

    def about(self) -> None:
        QtWidgets.QMessageBox.about(
            self, "RT82 Screen",
            f"<b>RT82 Screen {__version__}</b><br>Экран Epomaker RT82 из Linux без браузера.<br><br>"
            "Протокол разобран в проектах guysoft/rt82display и wyattades/keyscreen.",
        )

    def show_window(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, e):
        if self.quitting or not QtWidgets.QSystemTrayIcon.isSystemTrayAvailable():
            self._shutdown()
            e.accept()
            return
        e.ignore()
        self.hide()
        self.tray.showMessage("RT82 Screen", "Приложение продолжает работать в трее.", QtWidgets.QSystemTrayIcon.MessageIcon.Information, 3000)

    def _shutdown(self) -> None:
        self._save()
        self.hub.stop()
        self.ctl.shutdown()

    def quit(self) -> None:
        self.quitting = True
        self._shutdown()
        self.app.quit()


def app_icon() -> QtGui.QIcon:
    icon = QtGui.QIcon.fromTheme(APP_ID)
    if icon.isNull():
        icon = QtGui.QIcon.fromTheme("input-keyboard")
    if icon.isNull():
        here = Path(__file__).resolve().parent / "data" / f"{APP_ID}.svg"
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
