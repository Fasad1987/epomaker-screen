#!/bin/bash
# Installs RT82 Screen for a user and the udev rule.
#
#   scripts/install.sh              tray app + autostart + udev rule (root via sudo, or su if there is no sudo)
#   su -c 'scripts/install.sh'      the same, started as root (ALT Linux has no sudo by default)
#   scripts/install.sh --service    headless clock sync (systemd --user) instead of the autostart entry
#   scripts/install.sh --no-udev    skip the udev rule
#   scripts/install.sh --user NAME  user to install for when run as root (default: who called su/sudo)
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/common.sh
. "$SRC/scripts/common.sh"

PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}"
LIBDIR="$PREFIX/rt82-plasma"
BINDIR="$HOME/.local/bin"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}"
SERVICE=0
UDEV=1
USER_STAGE=0  # internal: the per-user part, started by the root part below
FOR_USER=""
while [ $# -gt 0 ]; do
	case "$1" in
		--service) SERVICE=1 ;;
		--no-udev) UDEV=0 ;;
		--user) FOR_USER="${2:-}"; shift ;;
		--user=*) FOR_USER="${1#--user=}" ;;
		--user-stage) USER_STAGE=1 ;;
		-h|--help) sed -n '2,9p' "$0"; exit 0 ;;
		*) echo "неизвестный параметр: $1" >&2; exit 2 ;;
	esac
	shift
done

install_udev() {
	say "Устанавливаю udev-правило"
	as_root sh -c 'install -Dm644 "$1" /etc/udev/rules.d/70-epomaker-rt82.rules || exit 1
		{ udevadm control --reload-rules && udevadm trigger --subsystem-match=hidraw --action=add; } ||
			echo "   udev не перечитал правила — они применятся после перезагрузки"' sh "$SRC/udev/70-epomaker-rt82.rules"
}

if [ "$(id -u)" = 0 ] && [ "$USER_STAGE" = 0 ]; then
	# Started as root (su -c / sudo): system part here, the rest as the user.
	U="$(target_user "$FOR_USER")" || die "Не понял, для какого пользователя ставить. Укажите: --user ИМЯ"
	run_as_user "$U" test -r "$SRC/rt82/cli.py" ||
		die "Пользователь $U не может читать $SRC. Положите проект в его домашний каталог."
	[ "$UDEV" = 1 ] && install_udev
	say "Ставлю программу для пользователя $U"
	args=(--user-stage --no-udev)
	[ "$SERVICE" = 1 ] && args+=(--service)
	run_as_user "$U" bash "$SRC/scripts/install.sh" "${args[@]}"
	exit 0
fi
[ "$(id -u)" = 0 ] && die "Внутренняя ошибка: пользовательская часть запущена от root"

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
	echo "  ALT Linux:  su -c 'apt-get install python3-module-PyQt6'"
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
	user_systemctl daemon-reload
	user_systemctl enable --now rt82-watch.service
else
	say "Добавляю автозапуск в трей"
	install -Dm644 "$SRC/packaging/rt82-plasma-autostart.desktop" "$CONFIG/autostart/rt82-plasma.desktop"
	sed -i "s|^Exec=rt82-screen|Exec=$BINDIR/rt82-screen|" "$CONFIG/autostart/rt82-plasma.desktop"
	if systemctl --user is-enabled rt82-watch.service >/dev/null 2>&1; then
		systemctl --user disable --now rt82-watch.service || true
	fi
fi

[ "$UDEV" = 1 ] && install_udev

if [ "$USER_STAGE" = 1 ]; then
	# PATH here is root's, not the user's login PATH
	echo "Если $BINDIR нет в PATH, добавьте его (например, в ~/.bashrc: export PATH=\"\$HOME/.local/bin:\$PATH\")"
else
	case ":$PATH:" in
		*":$BINDIR:"*) ;;
		*) echo "Добавьте $BINDIR в PATH (например, в ~/.bashrc: export PATH=\"\$HOME/.local/bin:\$PATH\")" ;;
	esac
fi

say "Готово"
echo "  Переподключите клавиатуру кабелем, затем проверьте: $BINDIR/rt82ctl list"
echo "  Приложение: «Экран RT82» в меню (или $BINDIR/rt82-screen)."
