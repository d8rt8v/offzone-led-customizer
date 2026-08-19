# Badge LED bytecode format

Encoding mirrors the official `led_instr.h` macro definitions distributed
with the badge docs. Verified byte-for-byte on real 2026 hardware.

## Container

```
u16le  total_frames        # playback length; player loops back at this point
block: u16le tick | instructions... | 0x00   # 0x00 = FRAME_END
```

- A block's instructions fire at frame `tick`.
- **Keep ticks unique across blocks.** Overlapping frame blocks are legal per
  the official README, but were part of the first sequence the badge
  rejected — play it safe and merge same-tick groups into
  one block with `02` (RESET_SELECTION) between them.
- `total_frames` should equal `max(tick + length)` over all instructions,
  otherwise the tail is cut or the loop stalls before restarting.

## Opcodes

| op | encoding                          | meaning                            |
|----|-----------------------------------|------------------------------------|
| 00 | `00`                              | FRAME_END                          |
| 01 | `01 <led>`                        | SELECT led (0..3), accumulates a mask, chainable |
| 02 | `02`                              | RESET_SELECTION                    |
| 03 | `03 <r> <g> <b> <u16 len>`        | SET_COLOR_RGB on selected LEDs     |
| 04 | `04 <r1 g1 b1> <r2 g2 b2> <u16 len>` | ANIMATE_RGB: fade over len frames |
| 05 | `05 <h> <s> <v> <u16 len>`        | SET_COLOR_HSV                      |
| 06 | `06 <hsv1> <hsv2> <u16 len>`      | ANIMATE_HSV                        |
| 07 | `07 <r> <g> <b> <u16 len> <u8 period>` | BLINK_RGB (color <-> black, toggles every `period` frames) |
| 08 | `08 <h> <s> <v> <u16 len> <u8 period>` | BLINK_HSV                   |

HSV byte conversion (identical to the C macros):
`h_byte = h / 1.40625` (h in 0..359), `s_byte = s * 2.55`,
`v_byte = v * 2.55` (s, v in 0..100).

## Runtime behavior (observed on the device)

- 4 independent LED channels; a color command on an already-busy channel
  preempts the running instruction.
- When an instruction's length expires the channel idles and **keeps its last
  color** — you don't need to re-assert unchanged LEDs.
- Sequence length: 6..1024 bytes (`led load` rejects anything else).
- The loader reads 2 hex chars per byte, echoing each char; Ctrl+C (0x03)
  aborts. On success it prints nothing and returns to the prompt.
- If the player hits a malformed stream mid-playback it silently loads a
  built-in fallback animation: all 4 LEDs slowly blinking red
  (`blink RED, 60 frames, period 30`). Seeing that pattern means corruption,
  not a working upload.

## Factory sequence (known-good decode)

`F401000001000101010201030600FEFEFFFEFEF40100` =
500 frames, one block at tick 0: SELECT 0..3, ANIMATE_HSV over 500 frames —
a full hue sweep on all LEDs.
