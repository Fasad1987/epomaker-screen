# Troubleshooting

## `epomakerctl list` shows `access=NO` on the vendor interface

The vendor interface is `FF60` on the RT82 family and `FFFF:0002` on TFT keyboards (RT100 Pro, ...).
The udev rule is missing or has not been applied yet. Run `scripts/install.sh` (or
`su -c 'scripts/install.sh'`), then unplug and reconnect the keyboard. The two other interfaces stay
`access=NO` on purpose: they carry your keystrokes.

## "the screen controller (1919:1919) did not appear"

- The keyboard must be connected with a cable; the screen is not reachable over Bluetooth or 2.4 GHz.
- Close Epomaker Hub and image.rdmctmzt.com tabs: they hold the keyboard's vendor interface.

## "screen not responding: power-cycle the keyboard" (RT82 family)

The screen controller stays on USB but no longer answers. Unplugging the cable alone may not help, because the
keyboard has a battery:

1. unplug the cable;
2. switch the keyboard off with its power switch;
3. wait about 10 seconds;
4. switch it on and plug the cable back in.

Epomaker Screen stops talking to a frozen controller and resumes by itself after the re-plug. Typing on the keyboard
is not affected.

Known causes: entering the controller's download mode repeatedly (versions before 0.2 did that on every upload),
two programs talking to the screen at once (Epomaker Hub plus this app), and very long erases.

## The screen shows noise or white

The screen is playing broken or erased flash. Upload a picture or restart live mode; a fresh upload replaces it.

## Large GIFs

Epomaker's software limits animations (40 frames on the RT82, 56 on the RT100 Pro, ...), and Epomaker Screen
thins longer ones out to that by default (Settings, or `epomakerctl upload --max-frames 0` to keep them all). Uploads up to about 1.7 MB have
worked on an RT82; if a bigger one shows garbage, use fewer frames.

## Epomaker Hub does not connect in Chromium

Open `chrome://device-log`. `FILE_ERROR_ACCESS_DENIED` means the udev rule is missing (see above). If Chromium
is a Flatpak, also allow device access: `flatpak override --user --device=all org.chromium.Chromium`. Press
**Pause** in Epomaker Screen first.

## Reporting a problem

Attach the output of:

```bash
su -c 'scripts/diagnose.sh'
```

and the log from the app window (or `epomakerctl` output) and `epomakerctl probe --identify`.
