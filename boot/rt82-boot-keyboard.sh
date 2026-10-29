#!/bin/bash
# Makes the RT82 (or any USB keyboard) usable at the disk-encryption password
# prompt, by putting its drivers into the initramfs and loading them early.
#
#   sudo boot/rt82-boot-keyboard.sh           show the plan, ask, apply, rebuild initrd
#   sudo boot/rt82-boot-keyboard.sh --yes     no questions
#   sudo boot/rt82-boot-keyboard.sh --check   only report what is in the current initrd
#   sudo boot/rt82-boot-keyboard.sh --undo    remove the changes and rebuild
#
# Works with make-initrd (ALT Linux), dracut (Fedora, openSUSE), initramfs-tools
# (Debian/Ubuntu) and mkinitcpio (Arch). The GRUB menu/password prompt is not
# affected by the initramfs: see the advice printed at the end.
set -euo pipefail

MARK_BEGIN="# >>> rt82-boot-keyboard"
MARK_END="# <<< rt82-boot-keyboard"
KERNEL="$(uname -r)"
YES=0 MODE=apply
for a in "$@"; do
	case "$a" in
		--yes|-y) YES=1 ;;
		--check) MODE=check ;;
		--undo) MODE=undo ;;
		-h|--help) sed -n '2,13p' "$0"; exit 0 ;;
		*) echo "неизвестный параметр: $a" >&2; exit 2 ;;
	esac
done

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!!  %s\033[0m\n' "$*"; }
ask() { [ "$YES" = 1 ] && return 0; read -r -p "$1 [y/N] " r; [ "$r" = y ] || [ "$r" = Y ] || [ "$r" = д ] || [ "$r" = Д ]; }

if [ "$(id -u)" != 0 ]; then
	echo "Нужны права root: sudo $0 $*" >&2
	exit 1
fi

# -- which modules does the keyboard need? ------------------------------------

# Every driver between the keyboard's input devices and the PCI host controller,
# found by walking sysfs from the live system (e.g. usbhid, hid_generic, xhci_pci).
live_modules() {
	local ev dev p drv mod
	for ev in /sys/class/input/input*; do
		[ -e "$ev/device" ] || continue
		dev="$(readlink -f "$ev")"
		case "$dev" in */usb*) ;; *) continue ;; esac
		# keyboards are the input devices with LEDs (Caps Lock), as make-initrd's own guess does
		[ "$(cat "$ev/capabilities/led" 2>/dev/null || echo 0)" != 0 ] || continue
		p="$dev"
		while [ "$p" != "/sys/devices" ] && [ "$p" != "/" ]; do
			if [ -L "$p/driver" ]; then
				drv="$(readlink -f "$p/driver")"
				if [ -L "$drv/module" ]; then
					mod="$(basename "$(readlink -f "$drv/module")")"
					echo "$mod"
				fi
			fi
			p="${p%/*}"
		done
	done | sort -u
}

command -v modinfo >/dev/null || { echo "нужна утилита modinfo (пакет kmod)" >&2; exit 1; }
is_loadable() { modinfo -k "$KERNEL" -F filename "$1" 2>/dev/null | grep -qv '^(builtin)$\|^$'; }

RT82_PRESENT=0
grep -qsi '^PRODUCT=36b0/30a3' /sys/bus/usb/devices/*/uevent && RT82_PRESENT=1

BASE="usbhid hid_generic hid xhci_pci xhci_hcd ehci_pci ehci_hcd ohci_pci ohci_hcd uhci_hcd xhci_pci_renesas"
MODS=""
for m in $(live_modules) $BASE; do
	m="${m//-/_}"
	case " $MODS " in *" $m "*) continue ;; esac
	is_loadable "$m" && MODS="$MODS $m"
done
MODS="${MODS# }"
# Loaded before udev starts: the HID drivers, so the keyboard works even if udev
# in the initramfs never gets to autoload them.
PRELOAD=""
for m in usbhid hid_generic; do
	case " $MODS " in *" $m "*) PRELOAD="$PRELOAD $m" ;; esac
done
PRELOAD="${PRELOAD# }"

# -- initramfs generator ------------------------------------------------------

GEN=""
if command -v make-initrd >/dev/null && [ -e /etc/initrd.mk ]; then GEN=make-initrd
elif command -v dracut >/dev/null; then GEN=dracut
elif command -v update-initramfs >/dev/null; then GEN=initramfs-tools
elif command -v mkinitcpio >/dev/null; then GEN=mkinitcpio
fi

image_for() {
	local f
	for f in "/boot/initrd-$KERNEL.img" "/boot/initramfs-$KERNEL.img" "/boot/initrd.img-$KERNEL" "/boot/initramfs-linux.img"; do
		[ -e "$f" ] && { echo "$f"; return 0; }
	done
	return 0
}

list_image() {
	local img="$1"
	if command -v initrd-ls >/dev/null; then initrd-ls "$img"
	elif command -v lsinitrd >/dev/null; then lsinitrd "$img"
	elif command -v lsinitramfs >/dev/null; then lsinitramfs "$img"
	elif command -v lsinitcpio >/dev/null; then lsinitcpio "$img"
	fi
}

check_image() {
	local img listing m missing=""
	img="$(image_for)"
	if [ -z "$img" ]; then
		warn "не нашёл образ initrd для ядра $KERNEL в /boot"
		return
	fi
	listing="$(list_image "$img" 2>/dev/null || true)"
	if [ -z "$listing" ]; then
		warn "не умею просматривать $img (нет initrd-ls/lsinitrd/lsinitramfs)"
		return
	fi
	for m in $MODS; do
		echo "$listing" | grep -Eq "/(${m}|${m//_/-})\.ko" || missing="$missing $m"
	done
	if [ -n "$missing" ]; then
		warn "в $img нет модулей:$missing"
	else
		say "в $img есть все модули клавиатуры: $MODS"
	fi
}

strip_block() { # file
	[ -e "$1" ] || return 0
	sed -i "/^$MARK_BEGIN\$/,/^$MARK_END\$/d" "$1"
}

rebuild() {
	say "Пересобираю initrd для $KERNEL"
	case "$GEN" in
		make-initrd) make-initrd -k "$KERNEL" ;;
		dracut) dracut -f --kver "$KERNEL" ;;
		initramfs-tools) update-initramfs -u -k "$KERNEL" ;;
		mkinitcpio) mkinitcpio -P ;;
	esac
}

apply_config() {
	case "$GEN" in
		make-initrd)
			# Drop-ins in /etc/initrd.mk.d are separate images in make-initrd,
			# so the settings go into /etc/initrd.mk itself, between markers.
			cp -a /etc/initrd.mk "/etc/initrd.mk.rt82-backup.$(date +%Y%m%d%H%M%S)"
			strip_block /etc/initrd.mk
			{
				echo "$MARK_BEGIN"
				echo "# Keyboard drivers for the disk password prompt (rt82-boot-keyboard.sh)"
				echo "MODULES_TRY_ADD += $MODS"
				[ -n "$PRELOAD" ] && echo "MODULES_PRELOAD += $PRELOAD"
				echo "$MARK_END"
			} >> /etc/initrd.mk
			;;
		dracut)
			cat > /etc/dracut.conf.d/90-rt82-keyboard.conf <<-EOF
				$MARK_BEGIN
				# Keyboard drivers for the disk password prompt (rt82-boot-keyboard.sh)
				add_drivers+=" $MODS "
				force_drivers+=" $PRELOAD "
				$MARK_END
			EOF
			;;
		initramfs-tools)
			strip_block /etc/initramfs-tools/modules
			{ echo "$MARK_BEGIN"; for m in $MODS; do echo "$m"; done; echo "$MARK_END"; } >> /etc/initramfs-tools/modules
			;;
		mkinitcpio)
			warn "mkinitcpio: добавьте хук keyboard перед encrypt/sd-encrypt в HOOKS в /etc/mkinitcpio.conf"
			warn "и модули в MODULES=($MODS), затем: mkinitcpio -P"
			return 1
			;;
	esac
}

undo_config() {
	case "$GEN" in
		make-initrd) strip_block /etc/initrd.mk ;;
		dracut) rm -f /etc/dracut.conf.d/90-rt82-keyboard.conf ;;
		initramfs-tools) strip_block /etc/initramfs-tools/modules ;;
	esac
}

# -- GRUB / firmware advice ---------------------------------------------------

grub_advice() {
	local f val efi=0
	[ -d /sys/firmware/efi ] && efi=1
	echo
	say "Загрузчик (меню GRUB и его пароль)"
	echo "   GRUB работает до Linux: клавиатуру в нём обслуживает прошивка (UEFI/BIOS), а не драйверы initrd."
	for f in /etc/sysconfig/grub2 /etc/default/grub; do
		[ -r "$f" ] || continue
		val="$(sed -n 's/^GRUB_TERMINAL_INPUT=//p' "$f" | tr -d '"'"'")"
		if [ -n "$val" ]; then
			echo "   $f: GRUB_TERMINAL_INPUT=$val"
			if [ "$efi" = 1 ] && echo "$val" | grep -Eq 'usb_keyboard|at_keyboard'; then
				warn "на UEFI лучше GRUB_TERMINAL_INPUT=\"console\" (ввод через прошивку); после правки: update-grub или grub2-mkconfig -o <путь к grub.cfg>"
			fi
		fi
	done
	echo "   Если в GRUB клавиатура не печатает:"
	echo "    1. Подключите RT82 кабелем (переключатель режима — в положение проводного/USB режима)"
	echo "       напрямую в порт материнской платы сзади (лучше USB 2.0), не через хаб или переднюю панель."
	echo "    2. В настройках UEFI: выключите Fast Boot / «Быстрая загрузка» (она пропускает"
	echo "       инициализацию USB), включите USB Legacy Support / «USB Keyboard Support» и XHCI Hand-off."
	echo "    3. Проверьте, печатает ли клавиатура в самом меню UEFI (Del/F2). Если нет — это ограничение"
	echo "       прошивки ПК с этой клавиатурой (у RT82 внутри USB-хаб); помогает донгл 2.4 ГГц или"
	echo "       другая клавиатура только для этого шага."
	echo "   В Fedora /boot обычно не зашифрован и пароль в GRUB не спрашивается, поэтому там"
	echo "   этот шаг просто не встречается; пароль диска там вводится уже в initrd."
}

# -- main -----------------------------------------------------------------------

echo "Ядро: $KERNEL; генератор initrd: ${GEN:-не найден}; RT82 подключена: $([ $RT82_PRESENT = 1 ] && echo да || echo нет)"
[ "$RT82_PRESENT" = 1 ] || warn "подключите RT82 кабелем перед запуском, чтобы найти её драйверы точно"
echo "Модули клавиатуры: ${MODS:-(все встроены в ядро)}"
echo "Загружать заранее:  ${PRELOAD:-—}"

if [ -z "$GEN" ]; then
	warn "не найден генератор initrd"
	grub_advice
	exit 1
fi

case "$MODE" in
	check)
		check_image
		grub_advice
		;;
	undo)
		ask "Убрать изменения и пересобрать initrd?" || exit 0
		undo_config
		rebuild
		;;
	apply)
		check_image
		if [ -z "$MODS" ]; then
			say "Все драйверы клавиатуры встроены в ядро — в initrd добавлять нечего."
		else
			ask "Добавить эти модули в initrd ($GEN) и пересобрать его?" || exit 0
			if apply_config; then
				rebuild
				check_image
				say "Готово. Перезагрузитесь и проверьте ввод пароля диска с RT82."
				echo "   Отменить: sudo $0 --undo"
			fi
		fi
		grub_advice
		;;
esac
