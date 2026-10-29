#!/bin/bash
# Installs RT82 Screen for the current user and the udev rule (asks for sudo once).
#
#   scripts/install.sh            tray app + autostart + udev rule
#   scripts/install.sh --service  headless clock sync (systemd --user) instead of the autostart entry
#   scripts/install.sh --no-udev  skip the udev rule
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}"
LIBDIR="$PREFIX/rt82-plasma"
BINDIR="$HOME/.local/bin"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}"
SERVICE=0
UDEV=1
for a in "$@"; do
	case "$a" in
		--service) SERVICE=1 ;;
		--no-udev) UDEV=0 ;;
		-h|--help) sed -n '2,7p' "$0"; exit 0 ;;
		*) echo "неизвестный параметр: $a" >&2; exit 2 ;;
	esac
done

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }

if [ "$(id -u)" = 0 ]; then
	echo "Запускайте от обычного пользователя: sudo будет запрошен только для udev-правила." >&2
	exit 1
fi

say "Проверяю зависимости"
python3 - <<'EOF' || {
import sys
if sys.version_info < (3, 9):
    sys.exit("нужен Python 3.9+")
try:
    import PyQt6.QtWidgets  # noqa
except ImportError:
    import PySide6.QtWidgets  # noqa
EOF
	echo
	echo "Не найден PyQt6/PySide6. Установите:"
	echo "  ALT Linux:  sudo apt-get install python3-module-PyQt6"
	echo "  Fedora:     sudo dnf install python3-pyqt6"
	echo "  Debian/Ubuntu: sudo apt install python3-pyqt6"
	exit 1
}
command -v playerctl >/dev/null || echo "   (необязательно) playerctl — для виджета «Сейчас играет»; без него используется gdbus"

say "Копирую программу в $LIBDIR"
rm -rf "$LIBDIR"
mkdir -p "$LIBDIR" "$BINDIR"
cp -r "$SRC/rt82" "$LIBDIR/"
find "$LIBDIR" -name '__pycache__' -prune -exec rm -rf {} +

for pair in "rt82ctl:rt82.cli" "rt82-screen:rt82.gui"; do
	name="${pair%%:*}" mod="${pair#*:}"
	cat > "$BINDIR/$name" <<EOF
#!/bin/sh
PYTHONPATH="$LIBDIR\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m $mod "\$@"
EOF
	chmod +x "$BINDIR/$name"
done

install -Dm644 "$SRC/rt82/data/rt82-plasma.svg" "$PREFIX/icons/hicolor/scalable/apps/rt82-plasma.svg"
install -Dm644 "$SRC/packaging/rt82-plasma.desktop" "$PREFIX/applications/rt82-plasma.desktop"
sed -i "s|^Exec=rt82-screen|Exec=$BINDIR/rt82-screen|" "$PREFIX/applications/rt82-plasma.desktop"
command -v update-desktop-database >/dev/null && update-desktop-database "$PREFIX/applications" 2>/dev/null || true

if [ "$SERVICE" = 1 ]; then
	say "Включаю службу rt82-watch (systemd --user)"
	rm -f "$CONFIG/autostart/rt82-plasma.desktop"
	install -Dm644 "$SRC/packaging/rt82-watch.service" "$CONFIG/systemd/user/rt82-watch.service"
	systemctl --user daemon-reload
	systemctl --user enable --now rt82-watch.service
else
	say "Добавляю автозапуск в трей"
	install -Dm644 "$SRC/packaging/rt82-plasma-autostart.desktop" "$CONFIG/autostart/rt82-plasma.desktop"
	sed -i "s|^Exec=rt82-screen|Exec=$BINDIR/rt82-screen|" "$CONFIG/autostart/rt82-plasma.desktop"
	if systemctl --user is-enabled rt82-watch.service >/dev/null 2>&1; then
		systemctl --user disable --now rt82-watch.service || true
	fi
fi

if [ "$UDEV" = 1 ]; then
	say "Устанавливаю udev-правило (нужен sudo)"
	sudo install -Dm644 "$SRC/udev/70-epomaker-rt82.rules" /etc/udev/rules.d/70-epomaker-rt82.rules
	sudo udevadm control --reload-rules
	sudo udevadm trigger --subsystem-match=hidraw --action=add
fi

case ":$PATH:" in
	*":$BINDIR:"*) ;;
	*) echo "Добавьте $BINDIR в PATH (например, в ~/.bashrc: export PATH=\"\$HOME/.local/bin:\$PATH\")" ;;
esac

say "Готово"
echo "  Переподключите клавиатуру кабелем, затем проверьте: $BINDIR/rt82ctl list"
echo "  Приложение: «Экран RT82» в меню (или $BINDIR/rt82-screen)."
