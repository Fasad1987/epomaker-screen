#!/bin/bash
# Installs RT82 Screen for a user, plus the udev rule.
#
#   scripts/install.sh              tray app + autostart + udev rule (root via sudo, or su without sudo)
#   su -c 'scripts/install.sh'      the same, started as root (ALT Linux has no sudo by default)
#   scripts/install.sh --service    headless clock sync (systemd --user) instead of the autostart entry
#   scripts/install.sh --no-udev    skip the udev rule
#   scripts/install.sh --user NAME  user to install for when run as root (default: who called su/sudo)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/common.sh
. "$ROOT/scripts/common.sh"

PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}"
LIBDIR="$PREFIX/rt82-plasma"
BINDIR="$HOME/.local/bin"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}"
SERVICE=0
UDEV=1
USER_STAGE=0 # internal: the per-user part, started by the root part below
FOR_USER=""
while [ $# -gt 0 ]; do
	case "$1" in
		--service) SERVICE=1 ;;
		--no-udev) UDEV=0 ;;
		--user) FOR_USER="${2:-}"; shift ;;
		--user=*) FOR_USER="${1#--user=}" ;;
		--user-stage) USER_STAGE=1 ;;
		-h|--help) sed -n '2,8p' "$0"; exit 0 ;;
		*) echo "unknown option: $1" >&2; exit 2 ;;
	esac
	shift
done

install_udev() {
	say "Installing the udev rule"
	# shellcheck disable=SC2016 # expanded by the root shell, not here
	as_root sh -c 'install -Dm644 "$1" /etc/udev/rules.d/70-epomaker-rt82.rules || exit 1
		{ udevadm control --reload-rules && udevadm trigger --subsystem-match=hidraw --action=add; } ||
			echo "   udev did not reload its rules; they apply after a reboot"' sh "$ROOT/data/udev/70-epomaker-rt82.rules"
}

if [ "$(id -u)" = 0 ] && [ "$USER_STAGE" = 0 ]; then
	# Started as root (su -c / sudo): system part here, the rest as the user.
	U="$(target_user "$FOR_USER")" || die "Cannot tell which user to install for. Pass --user NAME."
	run_as_user "$U" test -r "$ROOT/src/rt82/cli.py" ||
		die "User $U cannot read $ROOT. Put the project in their home directory."
	[ "$UDEV" = 1 ] && install_udev
	say "Installing the app for $U"
	args=(--user-stage --no-udev)
	[ "$SERVICE" = 1 ] && args+=(--service)
	run_as_user "$U" bash "$ROOT/scripts/install.sh" "${args[@]}"
	exit 0
fi
[ "$(id -u)" = 0 ] && die "Internal error: the per-user part is running as root"

say "Checking dependencies"
python3 - <<'EOF' || {
import sys
if sys.version_info < (3, 9):
    sys.exit("Python 3.9 or newer is required")
try:
    import PyQt6.QtWidgets  # noqa: F401
except ImportError:
    import PySide6.QtWidgets  # noqa: F401
EOF
	echo
	echo "PyQt6 or PySide6 is missing. Install it:"
	echo "  ALT Linux:     su -c 'apt-get install python3-module-PyQt6'"
	echo "  Fedora:        sudo dnf install python3-pyqt6"
	echo "  Debian/Ubuntu: sudo apt install python3-pyqt6"
	exit 1
}
command -v playerctl >/dev/null || echo "   (optional) install playerctl for the \"Now playing\" widget; gdbus is used without it"

say "Copying the app to $LIBDIR"
rm -rf "$LIBDIR"
mkdir -p "$LIBDIR" "$BINDIR"
cp -r "$ROOT/src/rt82" "$LIBDIR/"
find "$LIBDIR" -name '__pycache__' -prune -exec rm -rf {} +

for pair in "rt82ctl:rt82.cli" "rt82-screen:rt82.gui"; do
	name="${pair%%:*}" mod="${pair#*:}"
	cat > "$BINDIR/$name" <<EOF
#!/bin/sh
PYTHONPATH="$LIBDIR\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m $mod "\$@"
EOF
	chmod +x "$BINDIR/$name"
done

install -Dm644 "$ROOT/src/rt82/resources/rt82-plasma.svg" "$PREFIX/icons/hicolor/scalable/apps/rt82-plasma.svg"
install -Dm644 "$ROOT/data/applications/rt82-plasma.desktop" "$PREFIX/applications/rt82-plasma.desktop"
sed -i "s|^Exec=rt82-screen|Exec=$BINDIR/rt82-screen|" "$PREFIX/applications/rt82-plasma.desktop"
if command -v update-desktop-database >/dev/null; then
	update-desktop-database "$PREFIX/applications" 2>/dev/null || true
fi

if [ "$SERVICE" = 1 ]; then
	say "Enabling the rt82-watch service (systemd --user)"
	rm -f "$CONFIG/autostart/rt82-plasma.desktop"
	install -Dm644 "$ROOT/data/systemd/rt82-watch.service" "$CONFIG/systemd/user/rt82-watch.service"
	user_systemctl daemon-reload
	user_systemctl enable --now rt82-watch.service
else
	say "Adding the tray app to autostart"
	install -Dm644 "$ROOT/data/applications/rt82-plasma-autostart.desktop" "$CONFIG/autostart/rt82-plasma.desktop"
	sed -i "s|^Exec=rt82-screen|Exec=$BINDIR/rt82-screen|" "$CONFIG/autostart/rt82-plasma.desktop"
	if systemctl --user is-enabled rt82-watch.service >/dev/null 2>&1; then
		systemctl --user disable --now rt82-watch.service || true
	fi
fi

[ "$UDEV" = 1 ] && install_udev

path_hint="add it in ~/.bashrc: export PATH=\"\$HOME/.local/bin:\$PATH\""
if [ "$USER_STAGE" = 1 ]; then
	# PATH here is root's, not the user's login PATH
	echo "If $BINDIR is not in your PATH, $path_hint"
else
	case ":$PATH:" in
		*":$BINDIR:"*) ;;
		*) echo "$BINDIR is not in your PATH; $path_hint" ;;
	esac
fi

say "Done"
echo "  Reconnect the keyboard cable, then check: $BINDIR/rt82ctl list"
echo "  The app is \"RT82 Screen\" in the menu (or $BINDIR/rt82-screen)."
