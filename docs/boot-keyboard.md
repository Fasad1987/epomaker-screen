# Typing the password at boot

A full-disk-encrypted system can ask for a password twice, and the keyboard is handled by different software
each time.

## 1. Disk password in the initramfs

Linux is already running, but only with the drivers inside the initramfs image. Generators pick keyboard drivers
in different ways:

- **make-initrd** (ALT Linux) guesses them from the keyboards present when the image is built and marks them as
  rescue-shell modules. On an ALT Linux 11 system with an RT82 the image had `hid` and `hid-generic` but not
  `usbhid` or the USB host driver (`xhci_pci`), so no USB device worked at the prompt.
- **dracut** (Fedora) includes HID and USB host drivers more broadly, which is why the same keyboard works there.

`scripts/boot-keyboard.sh` (run as root, with the RT82 connected by cable):

1. walks sysfs from every keyboard input device up to the PCI host controller and collects the drivers on the
   way (for example `hid_generic`, `usbhid`, `xhci_pci`), plus common host drivers;
2. lists which of them are missing from the current image (`initrd-ls`, `lsinitrd`, `lsinitramfs`);
3. after confirmation, adds them:
   - make-initrd: a marked block in `/etc/initrd.mk` (a backup is kept next to it) with `MODULES_TRY_ADD`, and
     `MODULES_PRELOAD` for the HID drivers and the keyboard's own host driver, so they load before udev;
   - dracut: `/etc/dracut.conf.d/90-rt82-keyboard.conf` with `add_drivers` and `force_drivers`;
   - initramfs-tools: a marked block in `/etc/initramfs-tools/modules`;
   - mkinitcpio: prints what to add to `HOOKS` and `MODULES`;
4. rebuilds the image for the running kernel and checks it again.

```bash
su -c 'scripts/boot-keyboard.sh'           # or: sudo scripts/boot-keyboard.sh
su -c 'scripts/boot-keyboard.sh --check'   # report only
su -c 'scripts/boot-keyboard.sh --undo'    # revert and rebuild
```

Images for other installed kernels are rebuilt by the distribution the next time a kernel is installed, using
the same configuration.

## 2. GRUB menu and password

GRUB runs before Linux. On UEFI systems it reads the keyboard through the firmware, so no Linux driver helps.
If the RT82 does not type there:

1. In the UEFI setup, turn off **Fast Boot**: it often skips USB initialisation. Turn on **USB Legacy Support** /
   **USB Keyboard Support** and **XHCI Hand-off** if present. (On the ALT Linux system above, turning off Fast
   Boot was enough.)
2. Connect the keyboard directly to a port on the mainboard, not through a hub or the front panel.
3. Check whether the keyboard types in the UEFI setup itself. If it does not, the firmware cannot handle it (the
   RT82 has a USB hub inside); use the 2.4 GHz dongle or another keyboard for this step.
4. `GRUB_TERMINAL_INPUT` in `/etc/sysconfig/grub2` (ALT) or `/etc/default/grub` should be `console` (or unset) on
   UEFI. The script warns if it is `usb_keyboard` or `at_keyboard`.
