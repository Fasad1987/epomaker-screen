# Hardware testing

Every model in `src/epomaker_screen/models.py` that is not marked `verified` needs a first run on a real
keyboard. This page is the checklist; please report results in an issue (with `epomakerctl probe --identify`).

## Safety

- The TFT driver can only send identify, clock, system info, language and picture commands. It cannot reset,
  erase or reflash a keyboard.
- Close Epomaker Hub / the vendor app first: two programs talking to a keyboard at once can confuse it.
- The RT82 family's screen controller can freeze until a power cycle if misused; the `lab` experiments refuse
  that family.

## RT100 Pro (and the rest of the TFT family)

Connect the keyboard with a cable and run, in order:

```bash
epomakerctl list                 # the 3151:5002 keyboard, interface 2 with collections=FFFF:0002, access=yes
epomakerctl probe --identify     # internal id 3152 = RT100 Pro; please paste the whole output into the issue
epomakerctl sync-time            # does the keyboard clock show the right time?
epomakerctl sysinfo              # do CPU/RAM values appear on the keyboard's system page?
epomakerctl lab pattern          # colours, orientation and byte order (see below)
epomakerctl lab banks            # what the five banks are and how the keyboard switches between them
epomakerctl lab banks --pause 3  # the same with a pause between pictures, if some bank failed
epomakerctl lab speed            # how fast pixel packets can go (asks after each run)
epomakerctl lab partial          # whether a strip or a small box updates only part of the picture
epomakerctl lab flow             # replies during a transfer: is there a progress or busy signal?
epomakerctl lab frames           # numbered frames: how many fit, and where the picture banks are
epomakerctl lab animation        # four colours at 4 fps, looping
epomakerctl upload some.gif      # a real animation
```

`lab pattern` draws colour bars (red, green, blue, yellow, cyan, magenta, white, black from left to right) on
the top half, a white square in the top-left corner and a gradient below. If the colours are swapped, the image
is rotated or mirrored, or it is noise, the pixel format in docs/protocols/tft.md is wrong for this model; say
exactly what you see (a photo helps).

Also check, and report:

- After unplugging and replugging: are the pictures still there?
- Which keys switch the screen to the picture page, the clock and the system page? (RT100 Pro: Fn + PgUp/PgDn.)
- With an animation stored, the RT100 Pro goes back to it by itself after a while (firmware timer, checked with no
  program running). After how long, on your model?
- In `lab speed`: the fastest delay that still gives complete pictures.
- In `lab banks`: the timings it prints, and on which attempt each bank was accepted.

### Results so far

`reset-screen --erase` (`AC`) on the RT100 Pro removed all pictures and the animation in 0.5 s and kept the clock
and system pages. On other models, please report what it does.

RT100 Pro, firmware 0x0109: identify, clock, system page and `lab pattern` work; `lab speed` gave complete
pictures down to no delay (0.77 s per picture), but with no delay pictures in a row and a long animation came out
corrupted. A 56-frame GIF came out right at 2 ms and at 1 ms, now the default. Stills and the animation share
memory: after the GIF, two picture pages showed single frames of it, and the end of each loop showed parts of
other screens. `lab frames` measured the map: 6 MiB, frames 48–52 are the banks, so the limit is 47 frames. `lab animation` plays and survives unplugging,
with a visible wipe on each frame. A partial box is drawn on black, replacing the picture. `lab banks` had a
picture refused (bank 3 in one run, bank 5 in another) with the vendor's 2 s wait; the driver now waits longer.

## RT82 family

Verified on the RT82. For the QK108 and RT98: `epomakerctl list`, `sync-time`, then `upload` one picture.
