#!/bin/bash
# Collects what is needed to debug an Epomaker keyboard on this machine into one file.
#   scripts/diagnose.sh   -> epomaker-diagnose-<date>.txt in the current directory
# Run as root (su -c 'scripts/diagnose.sh' or sudo) to include the initrd contents and the GRUB config.
set -uo pipefail
if [ "$(id -u)" = 0 ]; then
	PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
else
	PATH="$PATH:/usr/local/sbin:/usr/sbin:/sbin"
fi

OUT="${1:-epomaker-diagnose-$(date +%Y%m%d-%H%M%S).txt}"
KERNEL="$(uname -r)"

section() { printf '\n===== %s =====\n' "$*"; }
run() { printf '$ %s\n' "$*"; "$@" 2>&1; }

{
	section "system"
	grep -sE '^(PRETTY_NAME|VERSION_ID)=' /etc/os-release
	echo "kernel: $KERNEL"
	echo "boot: $([ -d /sys/firmware/efi ] && echo UEFI || echo BIOS/legacy)"
	command -v mokutil >/dev/null && run mokutil --sb-state
	echo "session: ${XDG_SESSION_TYPE:-?} ${XDG_CURRENT_DESKTOP:-}"

	section "USB"
	command -v lsusb >/dev/null && { run lsusb -d 36b0:; run lsusb -d 3151:; run lsusb -d 1919:; run lsusb -t; }
	for d in /sys/bus/usb/devices/*; do
		[ -r "$d/idVendor" ] || continue
		case "$(cat "$d/idVendor")" in 36b0|3151|1919) ;; *) continue ;; esac
		echo "$d: $(cat "$d/idVendor"):$(cat "$d/idProduct") $(cat "$d/manufacturer" 2>/dev/null) / $(cat "$d/product" 2>/dev/null) speed=$(cat "$d/speed" 2>/dev/null)"
	done

	section "hidraw"
	for h in /sys/class/hidraw/hidraw*; do
		id="$(grep -s HID_ID "$h/device/uevent")"
		case "$id" in *000036B0*|*00003151*|*00001919*) ;; *) continue ;; esac
		node="/dev/${h##*/}"
		echo "$node $id $(grep -s HID_NAME "$h/device/uevent") iface=$(basename "$(dirname "$(readlink -f "$h/device")")")"
		echo "   $(ls -l "$node")"
		command -v getfacl >/dev/null && getfacl -p "$node" 2>/dev/null | grep -v '^#' | sed 's/^/   /'
		echo "   descriptor: $(od -An -tx1 -N8 "$h/device/report_descriptor" | tr -s ' ')"
	done
	ls -l /etc/udev/rules.d/*epomaker* 2>/dev/null || echo "epomaker-screen udev rule not installed"

	section "keyboard drivers (sysfs)"
	for ev in /sys/class/input/input*; do
		dev="$(readlink -f "$ev")"
		case "$dev" in */usb*) ;; *) continue ;; esac
		echo "$(cat "$ev/name" 2>/dev/null)  led=$(cat "$ev/capabilities/led" 2>/dev/null)"
		p="$dev"
		while [ "$p" != /sys/devices ] && [ "$p" != / ]; do
			[ -L "$p/driver" ] && echo "   ${p##*/}: $(basename "$(readlink -f "$p/driver")")"
			p="${p%/*}"
		done
	done
	run lsmod | grep -E '^(usbhid|hid_generic|hid|xhci|ehci|ohci|uhci|hid_)' || true

	section "initrd"
	for f in /etc/initrd.mk /etc/dracut.conf /etc/dracut.conf.d/*.conf /etc/initramfs-tools/modules /etc/mkinitcpio.conf; do
		[ -r "$f" ] && { echo "--- $f"; grep -v '^\s*#' "$f" | grep -v '^\s*$'; }
	done
	ls /etc/initrd.mk.d 2>/dev/null
	for img in "/boot/initrd-$KERNEL.img" "/boot/initramfs-$KERNEL.img" "/boot/initrd.img-$KERNEL"; do
		[ -e "$img" ] || continue
		echo "image: $img"
		for t in initrd-ls lsinitrd lsinitramfs; do
			command -v $t >/dev/null || continue
			$t "$img" 2>/dev/null | grep -E '(usbhid|hid-generic|hid_generic|xhci|ehci|ohci|uhci|/hid\.ko|cryptsetup|plymouthd)' | grep -v /themes/ | sed 's/^/   /'
			break
		done
	done
	echo "cmdline: $(cat /proc/cmdline)"

	section "GRUB"
	for f in /etc/sysconfig/grub2 /etc/default/grub; do
		[ -r "$f" ] && { echo "--- $f"; grep -E '^GRUB_(TERMINAL|ENABLE_CRYPTODISK|CMDLINE|PRELOAD)' "$f"; }
	done
	for f in /boot/grub/grub.cfg /boot/grub2/grub.cfg /boot/efi/EFI/*/grub.cfg; do
		[ -r "$f" ] && { echo "--- $f"; grep -E 'terminal_input|insmod (usb|ohci|uhci|ehci|at_keyboard|usb_keyboard|cryptodisk|luks)|password' "$f" \
			| sed -E 's/(password(_pbkdf2)?[[:space:]]+[^[:space:]]+[[:space:]]+).*/\1<hidden>/' | sort | uniq -c; }
	done

	section "kernel log (USB/HID)"
	dmesg 2>/dev/null | grep -iE '36b0|3151|1919|epomaker|usbhid|hid-generic' | tail -40 || echo "(dmesg needs root)"
} > "$OUT"

echo "Saved to $OUT (review it before posting: it lists hardware ids and the kernel command line)"
