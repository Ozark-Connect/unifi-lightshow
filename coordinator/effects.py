"""Built-in lighting effects with spatial awareness across multiple devices."""

import asyncio
import math
import logging
from datetime import datetime

from frame_coalescer import FrameCoalescer, SOURCE_EFFECT
from canvas import Canvas
from config import Config

log = logging.getLogger(__name__)


def hsv_to_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    """Convert HSV (0-360, 0-1, 0-1) to RGB (0-255)."""
    h = h % 360
    c = v * s
    x = c * (1 - abs((h / 60) % 2 - 1))
    m = v - c
    if h < 60:
        r, g, b = c, x, 0
    elif h < 120:
        r, g, b = x, c, 0
    elif h < 180:
        r, g, b = 0, c, x
    elif h < 240:
        r, g, b = 0, x, c
    elif h < 300:
        r, g, b = x, 0, c
    else:
        r, g, b = c, 0, x
    return int((r + m) * 255), int((g + m) * 255), int((b + m) * 255)


# ── Seasonal color schemes (ported from rgb-plasma.py) ───────────────────

SEASONS = [
    # (month_start, day_start, month_end, day_end, color1, color2, name)
    (10, 1,  11, 1,  (255, 40, 0),    (128, 0, 128),   "Halloween"),
    (11, 29, 12, 26, (0, 255, 0),     (255, 0, 0),     "Christmas"),
    (12, 27, 1,  2,  (255, 255, 255), (0, 100, 255),   "New Years"),
    (2,  7,  2,  14, (255, 0, 50),    (255, 105, 180), "Valentines Day"),
    (3,  14, 3,  17, (0, 200, 0),     (0, 255, 100),   "St Patricks Day"),
]

DEFAULT_SEASON = ((0, 229, 144), (200, 0, 255), "Default")  # Teal & Violet


def get_seasonal_colors() -> tuple[tuple[int, int, int], tuple[int, int, int], str]:
    """Return (color1, color2, season_name) based on current date."""
    now = datetime.now()
    m, d = now.month, now.day

    for ms, ds, me, de, c1, c2, name in SEASONS:
        # Handle year-wrapping ranges (e.g., Dec 27 - Jan 2)
        if ms > me:
            if (m == ms and d >= ds) or (m == me and d <= de) or (ms < m or m < me):
                return (c1, c2, name)
        else:
            if (m == ms and d >= ds and m <= me) or (ms < m < me) or (m == me and d <= de):
                return (c1, c2, name)

    return DEFAULT_SEASON


# ── Time-of-day brightness (ported from rgb-plasma.py) ───────────────────

TOD_SCHEDULE = [
    # (start_hour, end_hour, brightness_pct)
    (0.5,  7.0,  0),     # 00:30 - 07:00: OFF
    (7.0,  8.0,  50),    # 07:00 - 08:00: 50%
    (8.0,  20.25, 100),  # 08:00 - 20:15: 100%
    (20.25, 23.0, 75),   # 20:15 - 23:00: 75%
    (23.0, 24.0, 50),    # 23:00 - 00:00: 50%
    (0.0,  0.5,  50),    # 00:00 - 00:30: 50%
]


def get_tod_brightness() -> float:
    """Return brightness factor 0.0-1.0 based on time of day."""
    now = datetime.now()
    t = now.hour + now.minute / 60.0
    for start, end, pct in TOD_SCHEDULE:
        if start <= t < end:
            return pct / 100.0
    return 0.0


# ── UniFi baseline palette ──────────────────────────────────────────────

UNIFI_PALETTE = [
    (5, 255, 127),    # FE — green
    (5, 90, 255),     # GbE — blue
    (5, 218, 255),    # 2.5 GbE — cyan
    (121, 5, 255),    # 5 GbE — purple
    (231, 5, 255),    # 10 GbE — magenta
]


# ── Effect Engine ────────────────────────────────────────────────────────

class EffectEngine:
    def __init__(self, coalescer: FrameCoalescer, canvas: Canvas, config: Config):
        self._coalescer = coalescer
        self._canvas = canvas
        self._config = config
        self._task: asyncio.Task | None = None
        self._active_effect: str | None = None

    @property
    def active_effect(self) -> str | None:
        return self._active_effect

    def start(self, effect: str, **params):
        """Start a named effect, stopping any running one."""
        self.stop()
        self._active_effect = effect

        effects = {
            "rainbow": self._rainbow_cycle,
            "rainbow_static": self._rainbow_static,
            "solid": self._solid,
            "chase": self._chase,
            "breathe": self._breathe,
            "sweep": self._sweep,
            "palette_cycle": self._palette_cycle,
            "palette_sweep": self._palette_sweep,
            "plasma": self._plasma,
            "color_cycle": self._color_cycle,
            "off": self._off,
        }

        fn = effects.get(effect)
        if fn is None:
            log.warning("Unknown effect: %s", effect)
            return

        self._task = asyncio.create_task(fn(**params))
        log.info("Started effect: %s %s", effect, params or "")

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()
        self._task = None
        self._active_effect = None

    def _emit(self, pixel_colors: list[tuple[str, int, int, int, int, int]], brightness: int | None = None):
        """Convert a list of (device_name, port, r, g, b, w) into per-device frames."""
        frames: dict[str, list[tuple[int, int, int, int]]] = {}
        for sw in self._config.switches:
            frames[sw.name] = [(0, 0, 0, 0)] * sw.num_ports
        if self._config.openrgb_enabled:
            frames[self._config.openrgb.name] = [(0, 0, 0, 0)] * self._config.openrgb.num_leds

        for device_name, port, r, g, b, w in pixel_colors:
            if device_name in frames:
                frames[device_name][port - 1] = (r, g, b, w)

        self._coalescer.set_all_frames(frames, brightness, source=SOURCE_EFFECT)

    def _emit_from_canvas(self, color_fn, brightness: int | None = None):
        """Apply a color function to every pixel on the canvas."""
        pixels = []
        for device_name, port, nx, ny in self._canvas.normalized_positions():
            r, g, b, w = color_fn(nx, ny)
            pixels.append((device_name, port, r, g, b, w))
        self._emit(pixels, brightness)

    # ── Basic effects ────────────────────────────────────────────────────

    async def _rainbow_static(self, **_):
        def color_fn(nx, ny):
            r, g, b = hsv_to_rgb(nx * 360, 1.0, 1.0)
            return (r, g, b, 0)
        self._emit_from_canvas(color_fn)

    async def _rainbow_cycle(self, speed: float = 0.1, **_):
        offset = 0.0
        while True:
            def color_fn(nx, ny, _o=offset):
                r, g, b = hsv_to_rgb((nx * 360 + _o) % 360, 1.0, 1.0)
                return (r, g, b, 0)
            self._emit_from_canvas(color_fn)
            offset = (offset + 8) % 360
            await asyncio.sleep(speed)

    async def _sweep(self, r: int = 255, g: int = 255, b: int = 255, w: int = 0,
                     axis: str = "x", speed: float = 0.08, width: float = 0.2, **_):
        pos = -width
        direction = 1
        while True:
            def color_fn(nx, ny, _pos=pos):
                coord = nx if axis == "x" else ny
                intensity = max(0.0, 1.0 - abs(coord - _pos) / width)
                return (int(r * intensity), int(g * intensity), int(b * intensity), int(w * intensity))
            self._emit_from_canvas(color_fn)
            pos += 0.05 * direction
            if pos > 1.0 + width:
                direction = -1
            elif pos < -width:
                direction = 1
            await asyncio.sleep(speed)

    async def _solid(self, r: int = 255, g: int = 255, b: int = 255, w: int = 0, **_):
        self._emit_from_canvas(lambda nx, ny: (r, g, b, w))

    async def _chase(self, r: int = 0, g: int = 0, b: int = 255, w: int = 0, speed: float = 0.15, **_):
        positions = self._canvas.normalized_positions()
        total = len(positions)
        pos = 0
        while True:
            pixels = []
            for i, (name, port, nx, ny) in enumerate(positions):
                if i == pos % total:
                    pixels.append((name, port, r, g, b, w))
                elif i == (pos - 1) % total:
                    pixels.append((name, port, r // 4, g // 4, b // 4, w // 4))
                else:
                    pixels.append((name, port, 0, 0, 0, 0))
            self._emit(pixels)
            pos += 1
            await asyncio.sleep(speed)

    async def _breathe(self, r: int = 0, g: int = 100, b: int = 255, w: int = 0, speed: float = 0.08, **_):
        t = 0.0
        while True:
            v = (math.sin(t) + 1) / 2
            cr, cg, cb, cw = int(r * v), int(g * v), int(b * v), int(w * v)
            self._emit_from_canvas(lambda nx, ny, _r=cr, _g=cg, _b=cb, _w=cw: (_r, _g, _b, _w))
            t += 0.15
            await asyncio.sleep(speed)

    # ── UniFi palette effects ────────────────────────────────────────────

    async def _palette_cycle(self, speed: float = 0.08, **_):
        palette = UNIFI_PALETTE
        offset = 0.0
        while True:
            def color_fn(nx, ny, _o=offset):
                t = (nx + _o) % 1.0
                idx = t * len(palette)
                i0 = int(idx) % len(palette)
                i1 = (i0 + 1) % len(palette)
                f = idx - int(idx)
                r = int(palette[i0][0] * (1 - f) + palette[i1][0] * f)
                g = int(palette[i0][1] * (1 - f) + palette[i1][1] * f)
                b = int(palette[i0][2] * (1 - f) + palette[i1][2] * f)
                return (r, g, b, 0)
            self._emit_from_canvas(color_fn)
            offset = (offset + 0.02) % 1.0
            await asyncio.sleep(speed)

    async def _palette_sweep(self, speed: float = 0.06, width: float = 0.25, **_):
        palette = UNIFI_PALETTE
        color_idx = 0
        while True:
            r, g, b = palette[color_idx % len(palette)]
            pos = -width
            while pos < 1.0 + width:
                def color_fn(nx, ny, _pos=pos, _r=r, _g=g, _b=b):
                    intensity = max(0.0, 1.0 - abs(nx - _pos) / width)
                    return (int(_r * intensity), int(_g * intensity), int(_b * intensity), 0)
                self._emit_from_canvas(color_fn)
                pos += 0.05
                await asyncio.sleep(speed)
            color_idx += 1

    # ── Plasma effect (ported from rgb-plasma.py) ────────────────────────

    async def _plasma(self, speed: float = 0.08, use_tod: bool = True, **_):
        """Plasma wave with seasonal colors and time-of-day brightness.

        This is the fallback effect — what runs when SignalRGB isn't active.
        Ported from rgb-plasma.py with spatial awareness.
        """
        phase = 0.0
        current_brightness = get_tod_brightness() if use_tod else 1.0
        last_season = ""

        while True:
            # Check seasonal colors
            c1, c2, season = get_seasonal_colors()
            if season != last_season:
                log.info("Plasma season: %s", season)
                last_season = season

            # Smooth TOD brightness transition
            if use_tod:
                target = get_tod_brightness()
                if abs(current_brightness - target) < 0.01:
                    current_brightness = target
                elif current_brightness < target:
                    current_brightness = min(current_brightness + 0.005, target)
                else:
                    current_brightness = max(current_brightness - 0.005, target)

            # If brightness is 0, emit black and sleep longer
            if current_brightness == 0:
                self._emit_from_canvas(lambda nx, ny: (0, 0, 0, 0))
                await asyncio.sleep(1.0)
                continue

            br = current_brightness

            def color_fn(nx, ny, _phase=phase, _c1=c1, _c2=c2, _br=br):
                # Primary wave
                wave1 = math.sin(_phase * 3 + nx * 2 * math.pi) * 0.5 + 0.5
                # Secondary wave for variation
                wave2 = math.sin(_phase * 7 + nx * 3 * math.pi) * 0.15
                # Breathing
                breath = math.sin(_phase * 5) * 0.1 + 0.9
                # Blend
                blend = max(0.0, min(1.0, wave1 + wave2))
                r = int((_c2[0] * blend + _c1[0] * (1 - blend)) * breath * _br)
                g = int((_c2[1] * blend + _c1[1] * (1 - blend)) * breath * _br)
                b = int((_c2[2] * blend + _c1[2] * (1 - blend)) * breath * _br)
                # Clamp to max of either color
                r = min(int(max(_c1[0], _c2[0]) * _br), max(0, r))
                g = min(int(max(_c1[1], _c2[1]) * _br), max(0, g))
                b = min(int(max(_c1[2], _c2[2]) * _br), max(0, b))
                # Quantize to steps of 8 — aligns with daemon's threshold
                # so slow-moving parts of the wave skip more frames
                Q = 8
                return (r // Q * Q, g // Q * Q, b // Q * Q, 0)

            self._emit_from_canvas(color_fn)
            phase += 0.07
            await asyncio.sleep(speed)

    # ── Color cycle ────────────────────────────────────────────────────

    async def _color_cycle(self, speed: float = 0.2, use_tod: bool = True, **_):
        """Fade all LEDs between two seasonal colors.

        All ports get the same color, so the daemon's delta threshold means
        only the first frame after a color change sends ubus calls — subsequent
        identical frames are free. Quantized to steps of 8 for threshold alignment.
        """
        phase = 0.0
        current_brightness = get_tod_brightness() if use_tod else 1.0
        last_season = ""

        while True:
            c1, c2, season = get_seasonal_colors()
            if season != last_season:
                log.info("Color cycle season: %s", season)
                last_season = season

            if use_tod:
                target = get_tod_brightness()
                if abs(current_brightness - target) < 0.01:
                    current_brightness = target
                elif current_brightness < target:
                    current_brightness = min(current_brightness + 0.005, target)
                else:
                    current_brightness = max(current_brightness - 0.005, target)

            if current_brightness == 0:
                self._emit_from_canvas(lambda nx, ny: (0, 0, 0, 0))
                await asyncio.sleep(1.0)
                continue

            blend = (math.sin(phase) + 1) / 2
            br = current_brightness
            r = int((c1[0] * (1 - blend) + c2[0] * blend) * br)
            g = int((c1[1] * (1 - blend) + c2[1] * blend) * br)
            b = int((c1[2] * (1 - blend) + c2[2] * blend) * br)
            Q = 8
            r, g, b = r // Q * Q, g // Q * Q, b // Q * Q

            self._emit_from_canvas(lambda nx, ny, _r=r, _g=g, _b=b: (_r, _g, _b, 0))

            phase += 0.15
            await asyncio.sleep(speed)

    # ── Off ──────────────────────────────────────────────────────────────

    async def _off(self, **_):
        self._emit_from_canvas(lambda nx, ny: (0, 0, 0, 0))
