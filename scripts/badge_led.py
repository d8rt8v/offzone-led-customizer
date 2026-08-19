# /// script
# requires-python = ">=3.11"
# dependencies = ["pyserial"]
# ///
"""OFFZONE badge LED toolkit: console access + sequence compiler in one file.

Run with uv (pyserial is declared inline):
    uv run badge_led.py                          # self-test + build rainbow serpent
    uv run badge_led.py --upload --fps 30        # upload to the badge (port auto-detected)
    uv run badge_led.py --console "led show fps" "led show sequence"
    uv run badge_led.py --decode <HEXSTRING>     # pretty-print badge bytecode
    uv run badge_led.py --reset                  # restore factory animation

Console quirks this script handles (learned the hard way, keep them):
- input sent within ~1 s of port open is silently eaten -> settle, then wake
  with a bare CR and sync on the green prompt "\\x1b[32m> \\x1b[0m"
- commands must end with \\r ONLY: a trailing \\n is consumed by `led load`
  as data (strtol skips whitespace), shifting the whole hex stream
- `led load N` then exactly 2*N uppercase hex chars, no newline; it echoes
  every char and prints nothing on success
- a corrupted sequence gets silently replaced by a fallback animation
  (slow red blink) a few seconds into playback -> always verify the
  readback of `led show sequence` matches the compiled hex exactly

Bytecode format (official led_instr.h macros):
    u16le total_frames | blocks: u16le tick | instructions... | 0x00
    01 <led 0..3>              SELECT (accumulate mask, up to 4 per block)
    02                         RESET_SELECTION
    03 <r> <g> <b> <u16 len>   SET_COLOR_RGB
    04 <rgb1> <rgb2> <u16 len> ANIMATE_RGB (fade over len frames)
    05/06                      same as 03/04 in HSV (h/1.40625, s*2.55, v*2.55)
    07 <r> <g> <b> <u16 len> <u8 period>   BLINK_RGB (color <-> black)
    08                         BLINK_HSV
"""
from __future__ import annotations

import argparse
import re
import struct
import time
from contextlib import contextmanager

import serial

PORT = None  # None = auto-detect the badge (Windows, Linux, macOS)
VID_PID = (0x0483, 0x5740)  # STMicroelectronics Virtual COM Port
BAUD = 115200
PROMPT = b"\x1b[32m> \x1b[0m"
ANSI = re.compile(rb"\x1b\[[0-9;]*m")

LEDS_AMOUNT = 4
OP_FRAME_END = 0
OP_SELECT = 1
OP_RESET_SELECTION = 2
OP_SET_COLOR_RGB = 3
OP_ANIMATE_RGB = 4
OP_SET_COLOR_HSV = 5
OP_ANIMATE_HSV = 6
OP_BLINK_RGB = 7
OP_BLINK_HSV = 8

RED = (255, 0, 0)
GREEN = (0, 255, 0)
BLUE = (0, 0, 255)
WHITE = (255, 255, 255)
BLACK = (0, 0, 0)


# ---------------------------------------------------------------- console --
def detect_port() -> str:
    """Find the badge: an STM32 virtual COM port (USB CDC). COM<n> on
    Windows, /dev/ttyACM<n> on Linux, /dev/cu.usbmodem<n> on macOS."""
    from serial.tools import list_ports

    for p in list_ports.comports():
        if (p.vid, p.pid) == VID_PID:
            return p.device
    raise SystemExit(
        "badge not found — is it plugged in? Pass the port explicitly "
        "(COM4 on Windows, /dev/ttyACM0 on Linux)")


def dim(rgb: tuple[int, int, int], fraction: float) -> tuple[int, int, int]:
    """Scale an RGB color to a brightness fraction (0.0..1.0).

    The badge has no global brightness command; dim colors by scaling RGB
    values (or use HSV's v, 0..100). Eyes perceive PWM non-linearly, so
    0.5 already looks clearly dim; 0.12 (the serpent's tail glow) is a faint
    hint of color in a lit room."""
    if not 0.0 <= fraction <= 1.0:
        raise ValueError("fraction must be 0.0..1.0")
    return tuple(round(c * fraction) for c in rgb)


class Badge:
    """Prompt-synced console session over the badge's USB CDC port."""

    def __init__(self, port: str = PORT, baud: int = BAUD):
        self.ser = serial.Serial(port, baud, timeout=0.1, write_timeout=2.0)
        time.sleep(1.0)  # console eats input right after port open
        for _ in range(5):
            self.ser.write(b"\r")
            self.ser.flush()
            if self._wait_prompt(0.6):
                break
        time.sleep(0.2)
        self.ser.reset_input_buffer()

    def _wait_prompt(self, timeout: float) -> bool:
        deadline = time.time() + timeout
        buf = bytearray()
        while time.time() < deadline:
            buf.extend(self.ser.read(4096))
            if PROMPT in buf:
                self.ser.reset_input_buffer()
                return True
        return False

    def command(self, command: str, timeout: float = 2.0) -> str:
        """Run a shell command, return clean text output."""
        self.ser.reset_input_buffer()
        # \r only — never \r\n (see module docstring)
        self.ser.write(command.encode() + b"\r")
        self.ser.flush()
        out = bytearray()
        deadline = time.time() + timeout
        while time.time() < deadline:
            chunk = self.ser.read(4096)
            if chunk:
                out.extend(chunk)
                if PROMPT in out:
                    break
        body = out.split(PROMPT)[0]
        body = ANSI.sub(b"", body).decode("ascii", errors="replace")
        lines = body.splitlines()
        if lines and lines[0].strip() == command:
            lines = lines[1:]
        return "\n".join(lines).strip()

    def write_raw(self, data: bytes, timeout: float = 2.0) -> bytes:
        self.ser.reset_input_buffer()
        self.ser.write(data)
        self.ser.flush()
        out = bytearray()
        deadline = time.time() + timeout
        while time.time() < deadline:
            chunk = self.ser.read(4096)
            if chunk:
                out.extend(chunk)
                if PROMPT in out:
                    break
        return bytes(out)

    def close(self) -> None:
        self.ser.close()

    def __enter__(self) -> "Badge":
        return self

    def __exit__(self, *exc) -> None:
        self.close()


# --------------------------------------------------------------- compiler --
def _u16(n: int) -> list[int]:
    if not 0 <= n <= 0xFFFF:
        raise ValueError(f"length {n} out of u16 range")
    return [n & 0xFF, (n >> 8) & 0xFF]


def _rgb(c: tuple[int, int, int], what: str = "color") -> list[int]:
    for ch in c:
        if not 0 <= ch <= 255:
            raise ValueError(f"{what} channel {ch} out of 0..255")
    return list(c)


def _hsv(h: int, s: int, v: int) -> list[int]:
    for name, val, hi in (("h", h, 359), ("s", s, 100), ("v", v, 100)):
        if not 0 <= val <= hi:
            raise ValueError(f"{name}={val} out of 0..{hi}")
    return [int(h / 1.40625), int(s * 2.55), int(v * 2.55)]


class LedSequence:
    """Accumulates frame blocks and compiles them to badge bytecode."""

    def __init__(self) -> None:
        self._blocks: list[tuple[list[int], list[int]]] = []
        self._cur: list[int] | None = None
        self._selected = False

    @contextmanager
    def frame(self, tick: int):
        """Instructions inside run at frame `tick`. Keep ticks unique across
        blocks (2026-safe); use reset_selection() for a second command group
        at the same tick."""
        self._blocks.append((_u16(tick), []))
        self._cur = self._blocks[-1][1]
        self._selected = False
        try:
            yield self
        finally:
            self._cur.append(OP_FRAME_END)
            self._cur = None

    def select(self, *leds: int) -> None:
        if not leds:
            raise ValueError("select at least one LED")
        if len(leds) > LEDS_AMOUNT:
            raise ValueError(f"at most {LEDS_AMOUNT} LEDs per selection")
        for led in leds:
            if not 0 <= led < LEDS_AMOUNT:
                raise ValueError(f"LED index {led} out of 0..{LEDS_AMOUNT - 1}")
        if self._selected:  # re-selection inside a frame: reset mask first
            self._cur.append(OP_RESET_SELECTION)
        for led in leds:
            self._cur.extend((OP_SELECT, led))
        self._selected = True

    def reset_selection(self) -> None:
        self._cur.append(OP_RESET_SELECTION)
        self._selected = False

    def _need_selection(self) -> None:
        if not self._selected:
            raise RuntimeError("call select(...) before a color command")

    def set_color_rgb(self, rgb: tuple[int, int, int], length: int) -> None:
        self._need_selection()
        self._cur.extend((OP_SET_COLOR_RGB, *_rgb(rgb), *_u16(length)))

    def set_color_hsv(self, h: int, s: int, v: int, length: int) -> None:
        self._need_selection()
        self._cur.extend((OP_SET_COLOR_HSV, *_hsv(h, s, v), *_u16(length)))

    def animate_rgb(self, rgb1: tuple[int, int, int], rgb2: tuple[int, int, int], length: int) -> None:
        self._need_selection()
        self._cur.extend((OP_ANIMATE_RGB, *_rgb(rgb1), *_rgb(rgb2), *_u16(length)))

    def animate_hsv(self, h1: int, s1: int, v1: int, h2: int, s2: int, v2: int, length: int) -> None:
        self._need_selection()
        self._cur.extend((OP_ANIMATE_HSV, *_hsv(h1, s1, v1), *_hsv(h2, s2, v2), *_u16(length)))

    def blink_rgb(self, rgb: tuple[int, int, int], length: int, period: int) -> None:
        self._need_selection()
        if not 1 <= period <= 255:
            raise ValueError("period must be 1..255")
        self._cur.extend((OP_BLINK_RGB, *_rgb(rgb), *_u16(length), period))

    def blink_hsv(self, h: int, s: int, v: int, length: int, period: int) -> None:
        self._need_selection()
        if not 1 <= period <= 255:
            raise ValueError("period must be 1..255")
        self._cur.extend((OP_BLINK_HSV, *_hsv(h, s, v), *_u16(length), period))

    def compile(self, total_frames: int | None = None) -> bytes:
        """Serialize; total frames defaults to the last frame any command
        touches (tick + length)."""
        if self._cur is not None:
            raise RuntimeError("compile() called inside a frame block")
        if total_frames is None:
            total_frames = self._last_frame()
        out = bytearray(_u16(total_frames))
        for head, body in self._blocks:
            out.extend(head)
            out.extend(body)
        if not 6 <= len(out) <= 1024:
            raise ValueError(f"sequence is {len(out)} bytes, badge accepts 6..1024")
        return bytes(out)

    def _last_frame(self) -> int:
        end = 0
        for head, body in self._blocks:
            tick = head[0] | (head[1] << 8)
            i = 0
            while i < len(body):
                op = body[i]
                if op == OP_SELECT:
                    i += 2
                elif op in (OP_SET_COLOR_RGB, OP_SET_COLOR_HSV, OP_BLINK_RGB, OP_BLINK_HSV):
                    end = max(end, tick + (body[i + 4] | (body[i + 5] << 8)))
                    i += 6 if op in (OP_SET_COLOR_RGB, OP_SET_COLOR_HSV) else 7
                elif op in (OP_ANIMATE_RGB, OP_ANIMATE_HSV):
                    end = max(end, tick + (body[i + 7] | (body[i + 8] << 8)))
                    i += 9
                else:  # RESET_SELECTION / FRAME_END
                    i += 1
        return end


def decode(data: bytes) -> str:
    """Pretty-print badge bytecode (inverse of compile)."""
    total = struct.unpack_from("<H", data, 0)[0]
    lines = [f"total frames: {total} ({len(data)} bytes)"]
    i = 2
    while i < len(data):
        tick = struct.unpack_from("<H", data, i)[0]
        i += 2
        lines.append(f"  frame @{tick}:")
        while i < len(data):
            op = data[i]
            i += 1
            if op == OP_FRAME_END:
                break
            if op == OP_SELECT:
                lines.append(f"    select led {data[i]}")
                i += 1
            elif op == OP_RESET_SELECTION:
                lines.append("    reset selection")
            elif op in (OP_SET_COLOR_RGB, OP_SET_COLOR_HSV, OP_BLINK_RGB, OP_BLINK_HSV):
                c = tuple(data[i:i + 3])
                ln = struct.unpack_from("<H", data, i + 3)[0]
                i += 5
                if op in (OP_SET_COLOR_RGB, OP_SET_COLOR_HSV):
                    kind = "set_hsv" if op == OP_SET_COLOR_HSV else "set_rgb"
                    lines.append(f"    {kind} {c} len={ln}")
                else:
                    lines.append(f"    {'blink_hsv' if op == OP_BLINK_HSV else 'blink_rgb'} {c} len={ln} period={data[i]}")
                    i += 1
            elif op in (OP_ANIMATE_RGB, OP_ANIMATE_HSV):
                c1, c2 = tuple(data[i:i + 3]), tuple(data[i + 3:i + 6])
                ln = struct.unpack_from("<H", data, i + 6)[0]
                i += 8
                kind = "animate_hsv" if op == OP_ANIMATE_HSV else "animate_rgb"
                lines.append(f"    {kind} {c1} -> {c2} len={ln}")
            else:
                lines.append(f"    ?op {op:#04x}")
    return "\n".join(lines)


# -------------------------------------------------------------- animation --
PALETTES = (                     # one cycle = one family of shades
    (15, 30, 45, 60),            # sunset: red -> amber -> yellow
    (120, 135, 150, 165),        # mint:   green -> teal
    (200, 215, 230, 245),        # ocean:  cyan -> blue
    (285, 300, 315, 330),        # neon:   violet -> magenta -> pink
)


def build_palette() -> LedSequence:
    """Four shade themes take turns every cycle (sunset, mint, ocean, neon):
    each LED sways gently around its own hue, then everything crossfades
    smoothly into the next family. Soft pastel brightness, seamless loop
    (~8 s at 30 fps, ~2 s per theme)."""
    seq = LedSequence()
    S, V, sway = 75, 28, 14
    drift, fade = 20, 20
    for k, pal in enumerate(PALETTES):
        nxt = PALETTES[(k + 1) % len(PALETTES)]
        t = k * 3 * (drift + fade)
        with seq.frame(t):                       # sway up around the hue
            for led, hue in enumerate(pal):
                seq.select(led)
                seq.animate_hsv(hue - sway, S, V, hue + sway, S, V, length=drift)
        with seq.frame(t + drift):               # sway back
            for led, hue in enumerate(pal):
                seq.select(led)
                seq.animate_hsv(hue + sway, S, V, hue - sway, S, V, length=drift)
        with seq.frame(t + 2 * drift):           # crossfade into next theme
            for led, hue in enumerate(pal):
                seq.select(led)
                seq.animate_hsv(hue - sway, S, V, nxt[led] - sway, S, V, length=fade)
    return seq


def build_flow() -> LedSequence:
    """Soft flowing rainbow ("переливание"): every LED slowly rotates through
    the hue wheel with a constant 90-degree offset between neighbours — a
    perpetual gradient wash, no heads or flashes. Gentle pastel brightness.
    The last segment of each LED ends at hue 359 (~0), so the loop restart
    is invisible. ~1.6 s per hue revolution at 30 fps."""
    seq = LedSequence()
    segments, seg_len, step = 8, 6, 45
    S, V = 70, 28                     # pastel and dim — easy on the eyes
    for s in range(segments):
        with seq.frame(s * seg_len):
            for led in range(4):
                base = led * 90
                h0 = (base + s * step) % 360
                raw = (base + (s + 1) * step) % 360
                h1 = 359 if raw == 0 else raw
                seq.select(led)
                seq.animate_hsv(h0, S, V, h1, S, V, length=seg_len)
    return seq


def build_rainbow() -> LedSequence:
    """Rainbow serpent: the head glides ping-pong across the LEDs while its
    hue rotates around the color wheel; every LED it passes keeps that hue
    as a soft glow. One loop (8 passes) = one full hue revolution.
    ~6.8 s at 30 fps."""
    seq = LedSequence()
    passes, per_pass, stagger = 8, 24, 5
    touches = passes * 4
    for p in range(passes):
        leds = (0, 1, 2, 3) if p % 2 == 0 else (3, 2, 1, 0)
        for k, led in enumerate(leds):
            hue = round((p * 4 + k) * 360 / touches) % 360
            t = p * per_pass + k * stagger
            with seq.frame(t):
                seq.select(led)
                seq.animate_hsv(hue, 0, 0, hue, 100, 90, length=3)      # head
            with seq.frame(t + 3):
                seq.select(led)
                seq.animate_hsv(hue, 100, 90, hue, 85, 22, length=18)   # glow tail
    return seq


def build_snake() -> LedSequence:
    """Dim comet snake: a bright head glides onto each LED (fast fade-in),
    decays to a soft glow over a long fading tail. Blue pass converts
    everyone 0->3, red pass glides back 3->0. Loops."""
    HEAD_B, TAIL_B = (0, 0, 120), (0, 0, 30)
    HEAD_R, TAIL_R = (120, 0, 0), (30, 0, 0)
    seq = LedSequence()

    def glide(t0: int, head, tail, leds, stagger=6, attack=2, decay=12) -> int:
        for led in leds:
            with seq.frame(t0):
                seq.select(led)
                seq.animate_rgb(BLACK, head, length=attack)   # head arrives
            with seq.frame(t0 + attack):
                seq.select(led)
                seq.animate_rgb(head, tail, length=decay)     # long fading tail
            t0 += stagger
        return t0

    with seq.frame(0):                                   # soft red intro
        seq.select(0, 1, 2, 3)
        seq.blink_rgb((90, 0, 0), length=12, period=3)

    t = glide(12, HEAD_B, TAIL_B, (0, 1, 2, 3))          # blue pass

    with seq.frame(t):                                   # all-blue rest
        seq.select(0, 1, 2, 3)
        seq.set_color_rgb(TAIL_B, length=4)

    t = glide(t + 4, HEAD_R, TAIL_R, (3, 2, 1, 0))       # red pass back

    with seq.frame(t):                                   # settle dim red
        seq.select(0, 1, 2, 3)
        seq.set_color_rgb((50, 0, 0), length=4)
    return seq


# reference sequence that ran correctly on a real 2026 badge (regression anchor)
KNOWN_GOOD = (
    "300000000100010107FF00000C0002000C0001020103070000FF0C00020018000100"
    "010107FF00000800010201020103070000FF0800010020000100010103FF00000300"
    "00230001020103030000FF03000026000100010103FF0000030000290001020103"
    "030000FF0300002C000100010107FF00000400010201020103070000FF04000100")


def self_test() -> None:
    """Rebuild the known-good stock sequence byte-for-byte."""
    seq = LedSequence()

    def blink(tick, leds, rgb, ln, period):
        with seq.frame(tick):
            seq.select(*leds)
            seq.blink_rgb(rgb, ln, period)

    blink(0, (0, 1), RED, 12, 2)
    blink(12, (2, 3), BLUE, 12, 2)
    with seq.frame(24):
        seq.select(0, 1)
        seq.blink_rgb(RED, 8, 1)
        seq.select(2, 3)
        seq.blink_rgb(BLUE, 8, 1)
    for tick, leds, rgb in ((32, (0, 1), RED), (35, (2, 3), BLUE),
                            (38, (0, 1), RED), (41, (2, 3), BLUE)):
        with seq.frame(tick):
            seq.select(*leds)
            seq.set_color_rgb(rgb, 3)
    with seq.frame(44):
        seq.select(0, 1)
        seq.blink_rgb(RED, 4, 1)
        seq.select(2, 3)
        seq.blink_rgb(BLUE, 4, 1)
    got = seq.compile()
    want = bytes.fromhex(KNOWN_GOOD)
    assert got == want, f"self-test failed:\n got {got.hex().upper()}\nwant {want.hex().upper()}"
    print(f"self-test OK: rebuilt known-good sequence byte-for-byte ({len(want)} bytes)")


# ----------------------------------------------------------------- upload --
def upload(data: bytes, fps: int, port: str | None) -> None:
    hexstr = data.hex().upper()
    with Badge(port or detect_port()) as b:
        ack = b.command(f"led load {len(data)}", 3.0)
        if "Warning" not in ack and ack:
            print("load ack:", ack)
        b.write_raw(hexstr.encode(), 10.0)
        time.sleep(0.3)
        b.write_raw(b"\r", 1.0)               # flush stray echo from the line
        stored = b.command("led show sequence").split(":")[-1].strip()
        if stored != hexstr:
            raise RuntimeError(
                f"readback mismatch: stored {len(stored)} chars, "
                f"expected {len(hexstr)} — sequence is corrupted, retry")
        print(f"uploaded {len(data)} bytes, readback exact")
        time.sleep(6)                          # fallback swap happens within ~6 s
        if b.command("led show sequence").split(":")[-1].strip() != hexstr:
            raise RuntimeError("sequence was replaced by the fallback animation "
                               "(slow red blink) — bytecode rejected by the player")
        print("still playing after 6 s (no fallback)")
        print("set fps:", b.command(f"led set fps {fps}", 3.0))


def main() -> int:
    ap = argparse.ArgumentParser(description="OFFZONE badge LED toolkit")
    ap.add_argument("--upload", nargs="?", const="", metavar="PORT",
                    help="upload animation; without a value the badge port is "
                         "auto-detected (COM<n> / /dev/ttyACM<n>)")
    ap.add_argument("--preset", choices=("palette", "flow", "rainbow", "comet"),
                    default="palette", help="animation to build/upload (default palette)")
    ap.add_argument("--fps", type=int, default=30, help="fps to set after upload (5..100)")
    ap.add_argument("--decode", help="hex string to decode instead of building")
    ap.add_argument("--console", nargs="+", metavar="CMD",
                    help="run raw badge console commands")
    ap.add_argument("--reset", action="store_true", help="restore factory animation")
    args = ap.parse_args()

    if args.decode:
        print(decode(bytes.fromhex(args.decode.replace(" ", ""))))
        return 0

    if args.console:
        with Badge(detect_port()) as b:
            for c in args.console:
                print(f"$ {c}")
                print(b.command(c))
        return 0

    if args.reset:
        with Badge(detect_port()) as b:
            print(b.command("led reset"))
        return 0

    self_test()
    builders = {"palette": build_palette, "flow": build_flow,
                "rainbow": build_rainbow, "comet": build_snake}
    data = builders[args.preset]().compile()
    print(f"led load {len(data)}")
    print(data.hex().upper())
    if args.upload is not None:
        upload(data, args.fps, args.upload or None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
