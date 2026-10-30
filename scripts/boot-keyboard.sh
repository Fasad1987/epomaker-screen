#!/bin/bash
# Makes an Epomaker (or any USB) keyboard usable at the disk-encryption password
# prompt, by putting its drivers into the initramfs and loading them early.
#
#   su -c 'scripts/boot-keyboard.sh'           show the plan, ask, apply, rebuild the initrd
#   su -c 'scripts/boot-keyboard.sh --yes'     no questions
#   su -c 'scripts/boot-keyboard.sh --check'   only report what is in the current initrd
#   su -c 'scripts/boot-keyboard.sh --undo'    remove the changes and rebuild
# (or the same with sudo)
#
# Works with make-initrd (ALT Linux), dracut (Fedora, openSUSE), initramfs-tools
# (Debian/Ubuntu) and mkinitcpio (Arch). The GRUB menu/password prompt is not
# affected by the initramfs: see the advice printed at the end.
set -euo pipefail

# su -c keeps the user's PATH, which may lack the sbin directories (ALT Linux).
PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"

MARK_BEGIN="# >>> rt82-boot-keyboard"
MARK_END="# <<< rt82-boot-keyboard"
KERNEL="$(uname -r)"
YES=0 MODE=apply
for a in "$@"; do
	case "$a" in
		--yes|-y) YES=1 ;;
		--check) MODE=check ;;
		--undo) MODE=undo ;;
		-h|--help) sed -n '2,14p' "$0"; exit 0 ;;
		*) echo "unknown option: $a" >&2; exit 2 ;;
	esac
done

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
warn() { printf '\033[33m!!  %s\033[0m\n' "$*"; }
ask() { [ "$YES" = 1 ] && return 0; read -r -p "$1 [y/N] " r; [ "$r" = y ] || [ "$r" = Y ]; }

if [ "$(id -u)" != 0 ]; then
	echo "Root is required: su -c '$0 $*' (or sudo $0 $*)" >&2
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

command -v modinfo >/dev/null || { echo "modinfo is required (package kmod)" >&2; exit 1; }
is_loadable() { modinfo -k "$KERNEL" -F filename "$1" 2>/dev/null | grep -qv '^(builtin)$\|^$'; }

EPOMAKER_PRESENT=0
grep -qsiE '^PRODUCT=(36b0|3151)/' /sys/bus/usb/devices/*/uevent && EPOMAKER_PRESENT=1

BASE="usbhid hid_generic hid xhci_pci xhci_hcd ehci_pci ehci_hcd ohci_pci ohci_hcd uhci_hcd xhci_pci_renesas"
LIVE="$(live_modules | tr '-' '_')"
MODS=""
for m in $LIVE $BASE; do
	m="${m//-/_}"
	case " $MODS " in *" $m "*) continue ;; esac
	is_loadable "$m" && MODS="$MODS $m"
done
MODS="${MODS# }"
# Loaded before udev starts: the HID drivers and the drivers this keyboard is
# actually attached through (e.g. xhci_pci), so it works even if udev in the
# initramfs never gets to autoload them. The other host drivers stay on-demand.
PRELOAD=""
for m in usbhid hid_generic $LIVE; do
	case " $PRELOAD " in *" $m "*) continue ;; esac
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
		warn "no initrd image for kernel $KERNEL in /boot"
		return
	fi
	listing="$(list_image "$img" 2>/dev/null || true)"
	if [ -z "$listing" ]; then
		warn "cannot list $img (no initrd-ls/lsinitrd/lsinitramfs)"
		return
	fi
	for m in $MODS; do
		grep -Eq "/(${m}|${m//_/-})\.ko" <<<"$listing" || missing="$missing $m"
	done
	if [ -n "$missing" ]; then
		warn "$img lacks the modules:$missing"
	else
		say "$img has all keyboard modules: $MODS"
	fi
}

strip_block() { # file
	[ -e "$1" ] || return 0
	sed -i "/^$MARK_BEGIN\$/,/^$MARK_END\$/d" "$1"
}

rebuild() {
	say "Rebuilding the initrd for $KERNEL"
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
				echo "# Keyboard drivers for the disk password prompt (epomaker-screen scripts/boot-keyboard.sh)"
				echo "MODULES_TRY_ADD += $MODS"
				[ -n "$PRELOAD" ] && echo "MODULES_PRELOAD += $PRELOAD"
				echo "$MARK_END"
			} >> /etc/initrd.mk
			;;
		dracut)
			cat > /etc/dracut.conf.d/90-rt82-keyboard.conf <<-EOF
				$MARK_BEGIN
				# Keyboard drivers for the disk password prompt (epomaker-screen scripts/boot-keyboard.sh)
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
			warn "mkinitcpio: add the keyboard hook before encrypt/sd-encrypt to HOOKS in /etc/mkinitcpio.conf"
			warn "and the modules to MODULES=($MODS), then run: mkinitcpio -P"
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
	say "Boot loader (GRUB menu and password)"
	echo "   GRUB runs before Linux: its keyboard is handled by the firmware (UEFI/BIOS), not by initrd drivers."
	for f in /etc/sysconfig/grub2 /etc/default/grub; do
		[ -r "$f" ] || continue
		val="$(sed -n 's/^GRUB_TERMINAL_INPUT=//p' "$f" | tr -d '"'"'")"
		if [ -n "$val" ]; then
			echo "   $f: GRUB_TERMINAL_INPUT=$val"
			if [ "$efi" = 1 ] && grep -Eq 'usb_keyboard|at_keyboard' <<<"$val"; then
				warn "on UEFI use GRUB_TERMINAL_INPUT=\"console\" (input through the firmware), then run update-grub or grub2-mkconfig -o <path to grub.cfg>"
			fi
		fi
	done
	echo "   If the keyboard does not type in GRUB:"
	echo "    1. Connect the keyboard with a cable, directly to a port on the mainboard (USB 2.0 if there is one),"
	echo "       not through a hub or the front panel."
	echo "    2. In the UEFI setup, turn off Fast Boot (it skips USB initialisation) and turn on"
	echo "       USB Legacy Support / USB Keyboard Support and XHCI Hand-off."
	echo "    3. Check that the keyboard types in the UEFI setup itself (Del/F2). If it does not, the firmware"
	echo "       cannot handle this keyboard (some, like the RT82, have a USB hub inside); use the 2.4 GHz dongle or"
	echo "       another keyboard for this step."
}

# -- main -----------------------------------------------------------------------

echo "Kernel: $KERNEL; initrd generator: ${GEN:-not found}; Epomaker keyboard connected: $([ $EPOMAKER_PRESENT = 1 ] && echo yes || echo no)"
[ "$EPOMAKER_PRESENT" = 1 ] || warn "connect the keyboard with a cable first, so that its exact drivers are found"
echo "Keyboard modules: ${MODS:-(all built into the kernel)}"
echo "Load early:       ${PRELOAD:--}"

if [ -z "$GEN" ]; then
	warn "no initrd generator found"
	grub_advice
	exit 1
fi

case "$MODE" in
	check)
		check_image
		grub_advice
		;;
	undo)
		ask "Remove the changes and rebuild the initrd?" || exit 0
		undo_config
		rebuild
		;;
	apply)
		check_image
		if [ -z "$MODS" ]; then
			say "All keyboard drivers are built into the kernel: nothing to add to the initrd."
		else
			ask "Add these modules to the initrd ($GEN) and rebuild it?" || exit 0
			if apply_config; then
				rebuild
				check_image
				say "Done. Reboot and try typing the disk password on the keyboard."
				echo "   To undo: su -c '$0 --undo'"
			fi
		fi
		grub_advice
		;;
esac
