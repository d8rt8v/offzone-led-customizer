# offzone-led-customizer

[![skills.sh](https://skills.sh/b/d8rt8v/offzone-led-customizer)](https://skills.sh/d8rt8v/offzone-led-customizer)

An agent skill (and standalone toolkit) for customizing LED animations on the
**OFFZONE 2026 conference badge** — an STM32 board with a USB Virtual COM
Port — right over its serial console, no flashing needed.

## What it does

- Talks to the badge's built-in console (`led`, `echo`, mini-games, ...)
  with all its quirks handled: prompt sync, the 1-second settle window, and
  the CR-only line ending that otherwise silently corrupts uploads.
- Compiles animations to the badge's LED bytecode (the official `led_instr.h`
  macro format): SELECT / SET / ANIMATE / BLINK in
  RGB or HSV, 4 LEDs, 6..1024-byte sequences, fps 5..100.
- Ships ready animations: a **rainbow serpent** (hue-rotating head gliding
  ping-pong with a glowing trail) and a dim **comet snake** (blue pass,
  red pass back).
- Uploads with verification — rejects corrupted sequences (the badge
  silently swaps those for a fallback slow-red-blink) — plus `led save`
  guidance to persist across reboots and `allowKiosk` control so conference
  kiosks don't overwrite your animation.
- Cross-platform: auto-detects the port on Windows (`COM<n>`), Linux
  (`/dev/ttyACM<n>`) and macOS (`/dev/cu.usbmodem<n>`), with platform
  gotchas (dialout group, ModemManager, `cu` vs `tty`) documented.

## Install as an agent skill

```
npx skills add d8rt8v/offzone-led-customizer
```

## Use it directly

The toolkit is a single self-contained script ([scripts/badge_led.py](scripts/badge_led.py))
that declares its dependencies inline — [uv](https://docs.astral.sh/uv/) runs
it as-is:

```
uv run scripts/badge_led.py             # self-test + compile the rainbow serpent
uv run scripts/badge_led.py --upload --fps 30
uv run scripts/badge_led.py --console "led show fps" "led show sequence"
uv run scripts/badge_led.py --decode <HEX>
uv run scripts/badge_led.py --reset
```

Building your own animation:

```python
# anim.py — uv run --with pyserial anim.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent / "scripts"))
from badge_led import LedSequence, upload

seq = LedSequence()
with seq.frame(0):
    seq.select(0, 1, 2, 3)
    seq.animate_hsv(0, 0, 0, 300, 80, 60, length=50)   # hue sweep

upload(seq.compile(), fps=30, port=None)               # port=None = auto
```

See [references/format.md](references/format.md) for the full opcode table
and [references/recipes.md](references/recipes.md) for builder recipes.

## Credits

- LED bytecode format: official [bi-zone/badge_led](https://github.com/bi-zone/badge_led) macros.
- Console behavior cross-checked against badge firmware dumps from
  [AV1ct0r/badges](https://github.com/AV1ct0r/badges).
