#!/bin/bash
# Removes what scripts/install.sh installed. Settings in ~/.config/rt82-plasma are kept.
#
#   scripts/uninstall.sh              as the user (root via sudo, or su if there is no sudo)
#   su -c 'scripts/uninstall.sh'      as root; --user NAME if it cannot tell who called su
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/common.sh
. "$SRC/scripts/common.sh"

USER_STAGE=0
FOR_USER=""
while [ $# -gt 0 ]; do
	case "$1" in
		--user) FOR_USER="${2:-}"; shift ;;
		--user=*) FOR_USER="${1#--user=}" ;;
		--user-stage) USER_STAGE=1 ;;
		-h|--help) sed -n '2,6p' "$0"; exit 0 ;;
		*) echo "неизвестный параметр: $1" >&2; exit 2 ;;
	esac
	shift
done

remove_udev() {
	[ -e /etc/udev/rules.d/70-epomaker-rt82.rules ] || return 0
	say "Удаляю udev-правило"
	as_root sh -c 'rm -f /etc/udev/rules.d/70-epomaker-rt82.rules || exit 1; udevadm control --reload-rules || true'
}

if [ "$(id -u)" = 0 ] && [ "$USER_STAGE" = 0 ]; then
	U="$(target_user "$FOR_USER")" || die "Не понял, у какого пользователя удалять. Укажите: --user ИМЯ"
	remove_udev
	run_as_user "$U" bash "$SRC/scripts/uninstall.sh" --user-stage
	exit 0
fi

PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}"

if systemctl --user is-enabled rt82-watch.service >/dev/null 2>&1; then
	user_systemctl disable --now rt82-watch.service
fi
rm -f "$CONFIG/systemd/user/rt82-watch.service" "$CONFIG/autostart/rt82-plasma.desktop"
rm -f "$HOME/.local/bin/rt82ctl" "$HOME/.local/bin/rt82-screen"
rm -f "$PREFIX/applications/rt82-plasma.desktop" "$PREFIX/icons/hicolor/scalable/apps/rt82-plasma.svg"
rm -rf "$PREFIX/rt82-plasma"

[ "$USER_STAGE" = 1 ] || remove_udev
echo "Удалено. Настройки остались в $CONFIG/rt82-plasma"
