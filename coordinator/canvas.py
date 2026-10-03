"""Spatial canvas — maps a virtual 2D color field onto physical switch ports."""

import math
from dataclasses import dataclass

from config import SwitchConfig, OpenRGBDeviceConfig, GoveeDeviceConfig, Config


def _point_along(points: list[tuple[float, float]], dist: float) -> tuple[float, float]:
    """Return the point at arc length dist along a polyline."""
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        seg = math.hypot(bx - ax, by - ay)
        if dist <= seg and seg > 0:
            f = dist / seg
            return (ax + (bx - ax) * f, ay + (by - ay) * f)
        dist -= seg
    return points[-1]


def _polyline_length(points: list[tuple[float, float]]) -> float:
    return sum(math.hypot(bx - ax, by - ay) for (ax, ay), (bx, by) in zip(points, points[1:]))


def _loop_corners(gv: GoveeDeviceConfig) -> list[tuple[float, float]]:
    """Closed polyline around gv.rect, starting at the start of gv.start_edge.

    y increases downward. Facing the device, ccw runs bottom left-to-right,
    right side upward, top right-to-left, left side downward.
    """
    x0, y0, x1, y1 = gv.rect
    if gv.direction == "ccw":
        edges = [("bottom", (x0, y1)), ("right", (x1, y1)), ("top", (x1, y0)), ("left", (x0, y0))]
    else:
        edges = [("bottom", (x1, y1)), ("left", (x0, y1)), ("top", (x0, y0)), ("right", (x1, y0))]
    names = [n for n, _ in edges]
    i = names.index(gv.start_edge)
    corners = [c for _, c in edges[i:] + edges[:i]]
    return corners + [corners[0]]


def govee_positions(gv: GoveeDeviceConfig) -> list[tuple[float, float]]:
    """Canvas (x, y) of every segment of a Govee device, in segment order."""
    n = gv.num_segments

    if gv.shape == "path":
        length = _polyline_length(gv.path)
        if n == 1:
            return [gv.path[0]]
        return [_point_along(gv.path, length * i / (n - 1)) for i in range(n)]

    if gv.shape == "loop":
        corners = _loop_corners(gv)
        perimeter = _polyline_length(corners)
        first_edge = math.hypot(corners[1][0] - corners[0][0], corners[1][1] - corners[0][1])
        d0 = gv.start * first_edge
        return [_point_along(corners, (d0 + perimeter * i / n) % perimeter) for i in range(n)]

    # strip
    rad = math.radians(gv.rotation)
    cos_r = math.cos(rad)
    sin_r = math.sin(rad)
    return [
        (gv.x + i * gv.led_spacing * cos_r, gv.y + i * gv.led_spacing * sin_r)
        for i in range(n)
    ]


@dataclass
class CanvasPixel:
    """A pixel on the virtual canvas, mapped to a physical port."""
    switch_name: str
    port: int  # 1-indexed
    x: float
    y: float


class Canvas:
    """Maps switch ports to 2D positions for spatially-aware effects.

    Each switch's ports are laid out as a horizontal strip at the switch's
    (x, y) position, rotated by its rotation angle, optionally mirrored.
    Effects can then sample colors by (x, y) coordinate and the canvas
    resolves which physical port that maps to.
    """

    def __init__(self, config: Config):
        self._config = config
        self._pixels: list[CanvasPixel] = []
        self._build()

    def _build(self):
        """Compute the (x, y) position of every port across all switches."""
        self._pixels.clear()
        for sw in self._config.switches:
            rad = math.radians(sw.rotation)
            cos_r = math.cos(rad)
            sin_r = math.sin(rad)

            for p in range(sw.num_ports):
                idx = p if not sw.mirror else (sw.num_ports - 1 - p)
                # Local position: ports laid out along the x-axis
                lx = idx * sw.port_spacing
                ly = 0.0
                # Rotate around the switch origin
                wx = sw.x + lx * cos_r - ly * sin_r
                wy = sw.y + lx * sin_r + ly * cos_r

                self._pixels.append(CanvasPixel(
                    switch_name=sw.name,
                    port=p + 1,
                    x=wx,
                    y=wy,
                ))

        # Add OpenRGB LEDs
        if self._config.openrgb_enabled:
            orgb = self._config.openrgb
            rad = math.radians(orgb.rotation)
            cos_r = math.cos(rad)
            sin_r = math.sin(rad)

            for p in range(orgb.num_leds):
                lx = p * orgb.led_spacing
                ly = 0.0
                wx = orgb.x + lx * cos_r - ly * sin_r
                wy = orgb.y + lx * sin_r + ly * cos_r

                self._pixels.append(CanvasPixel(
                    switch_name=orgb.name,
                    port=p + 1,
                    x=wx,
                    y=wy,
                ))

        # Add Govee segments
        for gv in self._config.govee:
            for p, (wx, wy) in enumerate(govee_positions(gv)):
                self._pixels.append(CanvasPixel(
                    switch_name=gv.name,
                    port=p + 1,
                    x=wx,
                    y=wy,
                ))

    @property
    def pixels(self) -> list[CanvasPixel]:
        return self._pixels

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        """Return (min_x, min_y, max_x, max_y) bounding box."""
        if not self._pixels:
            return (0, 0, 0, 0)
        xs = [p.x for p in self._pixels]
        ys = [p.y for p in self._pixels]
        return (min(xs), min(ys), max(xs), max(ys))

    @property
    def width(self) -> float:
        b = self.bounds
        return b[2] - b[0]

    @property
    def height(self) -> float:
        b = self.bounds
        return b[3] - b[1]

    def normalized_positions(self) -> list[tuple[str, int, float, float]]:
        """Return (switch_name, port, nx, ny) with positions normalized to 0..1."""
        min_x, min_y, max_x, max_y = self.bounds
        w = max_x - min_x if max_x != min_x else 1.0
        h = max_y - min_y if max_y != min_y else 1.0
        return [
            (p.switch_name, p.port, (p.x - min_x) / w, (p.y - min_y) / h)
            for p in self._pixels
        ]

    def pixels_for_switch(self, switch_name: str) -> list[CanvasPixel]:
        return [p for p in self._pixels if p.switch_name == switch_name]
