#!/bin/bash
# Removes what scripts/install.sh installed. Settings in ~/.config/epomaker-screen are kept.
#
#   scripts/uninstall.sh              as the user (root via sudo, or su without sudo)
#   su -c 'scripts/uninstall.sh'      as root; add --user NAME if it cannot tell who called su
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
# shellcheck source=scripts/common.sh
. "$ROOT/scripts/common.sh"

USER_STAGE=0
FOR_USER=""
while [ $# -gt 0 ]; do
	case "$1" in
		--user) FOR_USER="${2:-}"; shift ;;
		--user=*) FOR_USER="${1#--user=}" ;;
		--user-stage) USER_STAGE=1 ;;
		-h|--help) sed -n '2,5p' "$0"; exit 0 ;;
		*) echo "unknown option: $1" >&2; exit 2 ;;
	esac
	shift
done

remove_udev() {
	[ -e /etc/udev/rules.d/70-epomaker-screen.rules ] || [ -e /etc/udev/rules.d/70-epomaker-rt82.rules ] || return 0
	say "Removing the udev rule"
	as_root sh -c 'rm -f /etc/udev/rules.d/70-epomaker-screen.rules /etc/udev/rules.d/70-epomaker-rt82.rules || exit 1
		udevadm control --reload-rules || true'
}

if [ "$(id -u)" = 0 ] && [ "$USER_STAGE" = 0 ]; then
	U="$(target_user "$FOR_USER")" || die "Cannot tell which user to uninstall for. Pass --user NAME."
	remove_udev
	run_as_user "$U" bash "$ROOT/scripts/uninstall.sh" --user-stage
	exit 0
fi

PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}"

for unit in epomaker-screen-watch.service rt82-watch.service; do
	if systemctl --user is-enabled "$unit" >/dev/null 2>&1; then
		user_systemctl disable --now "$unit"
	fi
done
for name in epomaker-screen rt82-plasma; do # rt82-plasma: version 0.2 and older
	rm -f "$CONFIG/autostart/$name.desktop" "$PREFIX/applications/$name.desktop" \
		"$PREFIX/icons/hicolor/scalable/apps/$name.svg"
	rm -rf "${PREFIX:?}/$name"
done
rm -f "$CONFIG/systemd/user/epomaker-screen-watch.service" "$CONFIG/systemd/user/rt82-watch.service"
rm -f "$HOME/.local/bin/epomakerctl" "$HOME/.local/bin/epomaker-screen" "$HOME/.local/bin/rt82ctl" "$HOME/.local/bin/rt82-screen"

[ "$USER_STAGE" = 1 ] || remove_udev
echo "Removed. Your settings are still in $CONFIG/epomaker-screen"
