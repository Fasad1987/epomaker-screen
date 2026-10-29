#!/bin/bash
# Removes what scripts/install.sh installed. Settings in ~/.config/rt82-plasma are kept.
set -euo pipefail

PREFIX="${XDG_DATA_HOME:-$HOME/.local/share}"
CONFIG="${XDG_CONFIG_HOME:-$HOME/.config}"

if systemctl --user is-enabled rt82-watch.service >/dev/null 2>&1; then
	systemctl --user disable --now rt82-watch.service || true
fi
rm -f "$CONFIG/systemd/user/rt82-watch.service" "$CONFIG/autostart/rt82-plasma.desktop"
rm -f "$HOME/.local/bin/rt82ctl" "$HOME/.local/bin/rt82-screen"
rm -f "$PREFIX/applications/rt82-plasma.desktop" "$PREFIX/icons/hicolor/scalable/apps/rt82-plasma.svg"
rm -rf "$PREFIX/rt82-plasma"

if [ -e /etc/udev/rules.d/70-epomaker-rt82.rules ]; then
	sudo rm -f /etc/udev/rules.d/70-epomaker-rt82.rules
	sudo udevadm control --reload-rules
fi
echo "Удалено. Настройки остались в $CONFIG/rt82-plasma"
