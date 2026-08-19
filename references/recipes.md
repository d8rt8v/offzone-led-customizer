# Building animations with LedSequence

The builder lives in `scripts/badge_led.py`. Importing it pulls in pyserial,
so run Python through uv with the dependency available:

```python
# file: anim.py in the repo root — run with:  uv run --with pyserial anim.py
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).parent / "scripts"))
from badge_led import LedSequence, decode, upload, dim, RED, BLUE, BLACK
```

```python
from badge_led import LedSequence, decode, RED, BLUE, BLACK

seq = LedSequence()
with seq.frame(0):                    # things that happen at frame 0
    seq.select(0, 1, 2, 3)            # pick LEDs (0..3) for the next command
    seq.blink_rgb(RED, length=12, period=3)
with seq.frame(12):
    seq.select(0)
    seq.animate_rgb(BLACK, BLUE, length=3)   # fade in
data = seq.compile()                  # bytes; total frames auto-computed
print(decode(data))                   # sanity-check the timeline
```

## API

- `seq.frame(tick)` — context manager; instructions inside fire at `tick`.
- `seq.select(*leds)` — select up to 4 LEDs; calling it again inside the same
  frame auto-emits RESET_SELECTION first (separate command group).
- `set_color_rgb(rgb, length)` / `set_color_hsv(h, s, v, length)`
- `animate_rgb(rgb1, rgb2, length)` / `animate_hsv(...)` — smooth fade
- `blink_rgb(rgb, length, period)` / `blink_hsv(...)` — color <-> black
- `compile(total_frames=None)` — bytes; asserts 6..1024 byte budget.
  Auto total = last frame any instruction touches.
- `decode(bytes)` — pretty-print any sequence (works on `led show sequence`
  output too).

## Proven patterns

- One frame block per tick (unique ticks). Two groups at the same tick go in
  ONE block: `select(a); cmd; select(b); cmd` — the builder inserts the reset.
- Never overlap two instructions on the same LED; end one before the next
  starts (adjacent is fine: blink ends at t=20, next set at t=20 works).
- Prefer ANIMATE over BLINK for "natural" motion; keep brightness low
  (head ~120, glow ~30 of 255) — full 255 is harsh on the eyes.

## Rainbow serpent (default preset, fps 30)

Head glides ping-pong; its hue rotates through the full color wheel over the
loop; every LED it passes keeps that hue as a soft glow. One loop = 8 passes
= one hue revolution (~6.8 s at 30 fps):

```python
def build_rainbow():
    seq = LedSequence()
    passes, per_pass, stagger = 8, 24, 5
    for p in range(passes):
        leds = (0, 1, 2, 3) if p % 2 == 0 else (3, 2, 1, 0)
        for k, led in enumerate(leds):
            hue = round((p * 4 + k) * 360 / (passes * 4)) % 360
            t = p * per_pass + k * stagger
            with seq.frame(t):
                seq.select(led)
                seq.animate_hsv(hue, 0, 0, hue, 100, 90, length=3)      # head
            with seq.frame(t + 3):
                seq.select(led)
                seq.animate_hsv(hue, 100, 90, hue, 85, 22, length=18)   # glow tail
    return seq
```

Tuning: `stagger`/`per_pass` (speed), head `v=90` and tail `v=22`
(brightness), `length=18` (trail persistence), `passes` (palette cycle —
keep `passes*4` touches under the 1024-byte budget: 8 passes = 898 bytes).

## Comet snake (preset "comet", fps 10)

```python
def build_snake():
    HEAD_B, TAIL_B = (0, 0, 120), (0, 0, 30)
    HEAD_R, TAIL_R = (120, 0, 0), (30, 0, 0)
    seq = LedSequence()

    def glide(t0, head, tail, leds, stagger=6, attack=2, decay=12):
        for led in leds:
            with seq.frame(t0):
                seq.select(led)
                seq.animate_rgb(BLACK, head, length=attack)   # head arrives
            with seq.frame(t0 + attack):
                seq.select(led)
                seq.animate_rgb(head, tail, length=decay)     # fading tail
            t0 += stagger
        return t0

    with seq.frame(0):                       # soft red intro
        seq.select(0, 1, 2, 3)
        seq.blink_rgb((90, 0, 0), length=12, period=3)
    t = glide(12, HEAD_B, TAIL_B, (0, 1, 2, 3))          # blue pass 0->3
    with seq.frame(t):
        seq.select(0, 1, 2, 3)
        seq.set_color_rgb(TAIL_B, length=4)
    t = glide(t + 4, HEAD_R, TAIL_R, (3, 2, 1, 0))       # red pass 3->0
    with seq.frame(t):
        seq.select(0, 1, 2, 3)
        seq.set_color_rgb((50, 0, 0), length=4)
    return seq
```

Tuning knobs: `stagger` (frames between LEDs), `attack` (head sharpness),
`decay` (tail length), head/tail RGB values, `fps` (5..100, set after upload).
At fps 10 the loop above is ~7 s.

## Brightness

No hardware/global brightness exists — bake it into colors:

- RGB: `dim(rgb, fraction)` from the script, e.g. `dim(RED, 0.4)` = (102,0,0).
- HSV: the `v` argument (0..100), e.g. `seq.animate_hsv(h, 0, 0, h, 100, 12, ...)`.
- Perception is non-linear (roughly quadratic), so halving the value looks
  much dimmer than half. Working reference points on this badge:
  255 = harsh, 120 (~0.47) = comfortable full, 50 (~0.2) = soft,
  30 (~0.12) = glow, 12 (~0.05) = faint hint in a lit room.
- Fade-ins/outs double as brightness ramps: `animate_hsv(h, 100, 90, h, 85, 22)`
  fades both saturation and value down while keeping the hue.

## Uploading

```python
from badge_led import upload
data = build_rainbow().compile()
upload(data, fps=30, port=None)   # port=None auto-detects; verifies exact
                                  # readback and that no fallback replaced it
```

`upload()` fails loudly on readback mismatch or if the fallback animation
replaces the sequence — never ignore those errors, retry instead.
