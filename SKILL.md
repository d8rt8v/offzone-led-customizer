---
name: offzone-led-customizer
description: Control LEDs and run console commands on the OFFZONE 2026 conference badge (an STM32 board with a USB Virtual COM Port) over its serial console. Use whenever the user mentions the OFFZONE badge, badge LEDs or LED animations, wants to upload/decode an LED sequence via `led load`, change fps, or talks to the badge terminal ("бейдж", "светодиоды на бейдже", "анимация"). Includes a bytecode compiler for animations (set/animate/blink in RGB or HSV).
---

# OFFZONE badge LED control

The badge is an STM32 board exposing a USB Virtual COM Port: on Windows it
shows up as **USB Serial Device (COM n)**, on Linux as `/dev/ttyACM<n>`.

List candidate ports on Windows:

```powershell
Get-PnpDevice -Class Ports -Status OK | Format-List FriendlyName, InstanceId
```

Everything else runs through the bundled script from this repo's root
(self-contained, declares pyserial inline, so `uv run` just works). It
auto-detects the badge on both Windows and Linux — pass a port only to
override:

```
uv run scripts/badge_led.py --console "led show fps"
uv run scripts/badge_led.py             # self-test + compile rainbow serpent, prints hex
uv run scripts/badge_led.py --upload --fps 30
uv run scripts/badge_led.py --decode <HEX>
uv run scripts/badge_led.py --reset    # factory animation back
```

Import it as a module when building custom animations:
`from badge_led import LedSequence, decode` (add its dir to `sys.path`).

## Linux & macOS specifics

**Linux**: the badge is a USB CDC device: it appears as `/dev/ttyACM0`
(cdc_acm driver).

- **Permission denied** on open → the user needs serial port access:
  `sudo usermod -aG dialout $USER` (group is `uucp` on some distros), then
  log out/in. One-off alternative: `sudo chmod a+rw /dev/ttyACM0`.
- **ModemManager may seize the port** right after plug-in and probe it with
  AT commands (eats the wake CR, breaks the prompt sync). If commands get no
  response: `systemctl stop ModemManager`, or better, tell udev to ignore
  CDC ACM ports: `/etc/udev/rules.d/49-badge.rules` with
  ```
  SUBSYSTEM=="tty", KERNEL=="ttyACM*", \
      ENV{ID_MM_DEVICE_IGNORE}="1", MODE="0666"
  ```
  then `sudo udevadm control --reload && sudo udevadm trigger`.
- Port discovery without the script: `ls /dev/ttyACM*` (the badge is a
  plain CDC ACM device).

**macOS**: the badge mounts as `/dev/cu.usbmodem<id>` (plus a
`/dev/tty.usbmodem<id>` twin).

- Always talk to the **`/dev/cu.*`** form — opening `/dev/tty.*` can block
  waiting for carrier detect.
- Find it with `ls /dev/cu.usbmodem*`; no drivers, groups or udev rules
  needed, macOS handles USB CDC out of the box.

## Brightness

There is **no global brightness command** — dim animations inside the
bytecode: scale RGB values (`dim(rgb, 0.4)` helper in the script) or use
HSV's `v` (0..100). Perception is non-linear: 255 is harsh, ~120 reads as a
comfortable "on", ~30 is a glow, ~12 a faint hint. See
[references/recipes.md](references/recipes.md).

## Console facts

- Commands: `echo, led, clear, display, analyst, hidden_payload, musicbox, cub_3`;
  `led -h` lists actions: `show/set/load/pause/continue/reset/save`, items:
  `sequence, fps (5..100), allowKiosk`.
- `led save` persists the current sequence + fps + allowKiosk to flash;
  RAM animations are lost on reboot — mention `--reset`/`led reset` restores
  factory.

## Kiosk mode (allowKiosk)

At the conference, OFFZONE kiosks connect to visitor badges over this same
USB port and **push their own animations, overwriting whatever is playing**.
That is what the `led load` warning and the `allowKiosk` item are about:

- `allowKiosk true` (factory default) — any kiosk may replace your sequence.
- `led set allowKiosk false` — your animation is protected from kiosks.
  Persist it together with the sequence: `led set allowKiosk false` then
  `led save` (save is rate-limited; retry if it complains).
- Want the kiosk content back (quests / collectibles)? Set it `true` again.
- `led pause` / `led continue` freeze/resume playback without touching the
  stored sequence — handy when showing the badge off.

## Non-negotiable gotchas (corrupt sequences otherwise)

1. Commands end with `\r` ONLY. A trailing `\n` is eaten by `led load`'s hex
   reader (strtol skips whitespace), shifting the whole stream and silently
   corrupting the animation.
2. Input sent within ~1 s of port open is swallowed; sync on the green prompt
   `\x1b[32m> \x1b[0m` before commanding.
3. After `led load N` send exactly `2*N` uppercase hex chars with no newline,
   then a bare `\r` to flush the echo.
4. ALWAYS verify `led show sequence` equals the compiled hex, and again after
   ~6 s: a corrupted/rejected sequence is silently replaced by a fallback
   (slow red blink on all LEDs).

## Animation format in 30 seconds

Header `u16le total_frames`, then blocks: `u16le tick | instructions | 0x00`.
Ops: `01 led`(select, 4 LEDs, indices 0-3), `02`(reset selection), `03 rgb len16`
(set), `04 rgb1 rgb2 len16`(animate/fade), `05/06` HSV variants, `07 rgb len16
period8`(blink). Lengths are frames; fps sets playback speed. Sequence size
must be 6..1024 bytes.

For building animations, read [references/recipes.md](references/recipes.md) —
it has the `LedSequence` builder API, a proven working pattern (unique ticks
per block, `reset_selection()` between groups), and the comet-snake example.
For the full opcode table and pitfalls, see
[references/format.md](references/format.md).
