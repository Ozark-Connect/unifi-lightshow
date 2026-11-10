"""Per-port traffic reactivity.

The switch daemon reads /proc/port/<N> on the switch and sends raw counters
back on the same UDP flow the frames use (see etherlightd_udp.c). This module
turns those counters into a utilization value per port, and applies that
value as a layer on top of whatever frame the show produced.

Polling stays on the switch, so it costs nothing on the switch CPU path,
which is limited to roughly 100 packets/s inbound.
"""

import colorsys
import logging
import math
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

MODES = ("boost", "tint", "brightness", "replace")
SOURCES = ("effect", "http", "signalrgb")


@dataclass
class TrafficConfig:
    enabled: bool = False
    mode: str = "boost"
    # Sources the layer applies to (frame_coalescer source names)
    sources: list[str] = field(default_factory=lambda: list(SOURCES))
    # Utilization scale: u = log(bps / floor) / log(link speed / floor)
    floor_mbps: float = 1.0
    # Divide every port by the busiest port, while the busiest is above this
    relative: bool = False
    relative_min: float = 0.1
    # Exponential smoothing time constant for bps, seconds
    smoothing: float = 1.0
    # Stats older than this disable the layer for that switch
    stale_after: float = 3.0
    # Repaint when a port's smoothed u moves by more than this
    dirty_step: float = 0.02
    # boost: idle ports sit at these fractions of the show's S and V; full
    # utilization gives V = 1.0 and S raised toward 1.0 by saturation_boost
    idle_saturation: float = 0.7
    idle_value: float = 0.3
    saturation_boost: float = 0.5
    gamma: float = 2.2
    # brightness: idle floor as a fraction of the show's V
    brightness_floor: float = 0.15
    # tint / replace colors
    heat_color: tuple[int, int, int] = (255, 0, 0)
    idle_color: tuple[int, int, int] = (0, 0, 255)

    @classmethod
    def from_dict(cls, data: dict) -> "TrafficConfig":
        cfg = cls()
        cfg.update(data)
        return cfg

    def update(self, data: dict):
        """Apply a partial update. Nothing changes if any field is invalid."""
        trial = TrafficConfig(**self.__dict__)
        for key, value in data.items():
            if key not in trial.__dict__:
                raise ValueError(f"traffic: unknown field '{key}'")
            if key in ("heat_color", "idle_color"):
                value = tuple(int(c) for c in value)
                if len(value) != 3 or not all(0 <= c <= 255 for c in value):
                    raise ValueError(f"traffic: {key} must be [r, g, b] in 0-255")
            if key == "sources":
                value = list(value)
            setattr(trial, key, value)
        trial._validate()
        self.__dict__.update(trial.__dict__)

    def _validate(self):
        if self.mode not in MODES:
            raise ValueError(f"traffic: mode must be one of {MODES}")
        bad = [s for s in self.sources if s not in SOURCES]
        if bad:
            raise ValueError(f"traffic: unknown sources {bad}, expected {SOURCES}")
        if self.floor_mbps <= 0:
            raise ValueError("traffic: floor_mbps must be > 0")
        for key in ("idle_saturation", "idle_value", "saturation_boost", "brightness_floor"):
            if not 0.0 <= getattr(self, key) <= 1.0:
                raise ValueError(f"traffic: {key} must be 0-1")
        if self.gamma <= 0:
            raise ValueError("traffic: gamma must be > 0")

    def to_dict(self) -> dict:
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in self.__dict__.items()}


# ── Stats parsing ────────────────────────────────────────────────────────

@dataclass
class PortSample:
    link: bool
    speed_mbps: int
    rx_bytes: int
    tx_bytes: int


def parse_stats(data: bytes) -> tuple[int, int, list[PortSample]] | None:
    """Parse 'S1 <seq> <ms> <link>:<speed>:<rx>:<tx> ...'. None if malformed."""
    try:
        parts = data.decode("ascii").split()
        if len(parts) < 3 or parts[0] != "S1":
            return None
        seq, ms = int(parts[1]), int(parts[2])
        ports = []
        for f in parts[3:]:
            link, speed, rx, tx = f.split(":")
            ports.append(PortSample(link == "1", int(speed), int(rx), int(tx)))
        return seq, ms, ports
    except (UnicodeDecodeError, ValueError):
        return None


# ── Rate math ────────────────────────────────────────────────────────────

def utilization(bps: float, speed_mbps: int, floor_mbps: float) -> float:
    """Log-scaled utilization, 0 at floor_mbps, 1 at link speed."""
    if speed_mbps <= floor_mbps or bps <= floor_mbps * 1e6:
        return 0.0
    u = math.log(bps / (floor_mbps * 1e6)) / math.log(speed_mbps / floor_mbps)
    return min(1.0, max(0.0, u))


@dataclass
class _PortState:
    last_ms: int | None = None
    last_rx: int = 0
    last_tx: int = 0
    bps: float = 0.0
    link: bool = False
    speed_mbps: int = 0


class SwitchTraffic:
    """Counter state and smoothed rates for one switch."""

    def __init__(self, num_ports: int):
        self.ports = [_PortState() for _ in range(num_ports)]
        self.last_update = 0.0  # time.monotonic() of the last good packet
        self.last_seq: int | None = None

    def update(self, ms: int, samples: list[PortSample], smoothing: float):
        for st, s in zip(self.ports, samples):
            st.link, st.speed_mbps = s.link, s.speed_mbps
            if st.last_ms is not None and ms > st.last_ms:
                dt = (ms - st.last_ms) / 1000.0
                drx, dtx = s.rx_bytes - st.last_rx, s.tx_bytes - st.last_tx
                # A negative delta is a counter reset (port flap, reboot): skip it
                if drx >= 0 and dtx >= 0:
                    raw = max(drx, dtx) * 8 / dt
                    alpha = 1.0 - math.exp(-dt / smoothing) if smoothing > 0 else 1.0
                    st.bps += alpha * (raw - st.bps)
            if not s.link:
                st.bps = 0.0
            st.last_ms, st.last_rx, st.last_tx = ms, s.rx_bytes, s.tx_bytes
        self.last_update = time.monotonic()

    def utils(self, cfg: TrafficConfig) -> list[float]:
        us = [utilization(p.bps, p.speed_mbps, cfg.floor_mbps) if p.link else 0.0
              for p in self.ports]
        if cfg.relative:
            peak = max(us, default=0.0)
            if peak > cfg.relative_min:
                us = [u / peak for u in us]
        return us


class TrafficMonitor:
    """Holds traffic state for every switch and tells the coalescer when it changes."""

    def __init__(self, cfg: TrafficConfig, switches: dict[str, int]):
        self.cfg = cfg
        self._switches = {name: SwitchTraffic(n) for name, n in switches.items()}
        self._painted: dict[str, list[float]] = {}  # u values the last repaint used
        self.on_change = None  # callable, set by the coalescer
        self.packets = 0
        self.bad_packets = 0

    def feed(self, switch: str, data: bytes):
        st = self._switches.get(switch)
        parsed = parse_stats(data)
        if st is None or parsed is None:
            self.bad_packets += 1
            return
        seq, ms, samples = parsed
        st.update(ms, samples, self.cfg.smoothing)
        st.last_seq = seq
        self.packets += 1

        if self.cfg.enabled and self.on_change:
            us = st.utils(self.cfg)
            prev = self._painted.get(switch)
            if prev is None or any(abs(a - b) > self.cfg.dirty_step for a, b in zip(us, prev)):
                self._painted[switch] = us
                self.on_change()

    def fresh(self, switch: str) -> bool:
        st = self._switches.get(switch)
        return st is not None and time.monotonic() - st.last_update < self.cfg.stale_after

    def utils(self, switch: str) -> list[float] | None:
        """Current utilization per port, or None when stats are missing or stale."""
        if not self.fresh(switch):
            return None
        return self._switches[switch].utils(self.cfg)

    def snapshot(self) -> dict:
        out = {}
        for name, st in self._switches.items():
            us = st.utils(self.cfg)
            out[name] = {
                "fresh": self.fresh(name),
                "age": round(time.monotonic() - st.last_update, 1) if st.last_update else None,
                "seq": st.last_seq,
                "ports": [{"port": i + 1, "link": p.link, "speed_mbps": p.speed_mbps,
                           "mbps": round(p.bps / 1e6, 2), "u": round(u, 3)}
                          for i, (p, u) in enumerate(zip(st.ports, us))],
            }
        return out


# ── Layer ────────────────────────────────────────────────────────────────

def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _perceptual_lerp(a: float, b: float, t: float, gamma: float) -> float:
    """Interpolate light output so equal steps of t look like equal brightness steps."""
    return _lerp(a ** (1 / gamma), b ** (1 / gamma), t) ** gamma


def _rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hsv_to_rgb(h, s, v)
    return round(r * 255), round(g * 255), round(b * 255)


def apply_layer(colors: list[tuple[int, int, int, int]], utils: list[float],
                cfg: TrafficConfig) -> list[tuple[int, int, int, int]]:
    """Return a new frame with traffic applied. Ports beyond len(utils) pass through."""
    out = []
    for i, (r, g, b, w) in enumerate(colors):
        if i >= len(utils):
            out.append((r, g, b, w))
            continue
        u = utils[i]
        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)

        if cfg.mode == "boost":
            if v < 0.02:  # black has no hue to boost
                out.append((r, g, b, w))
                continue
            idle_v, full_v = v * cfg.idle_value, 1.0
            idle_s, full_s = s * cfg.idle_saturation, s + (1 - s) * cfg.saturation_boost
            nv = _perceptual_lerp(idle_v, full_v, u, cfg.gamma)
            ns = _lerp(idle_s, full_s, u)
            out.append((*_rgb(h, ns, nv), w))

        elif cfg.mode == "brightness":
            nv = _perceptual_lerp(v * cfg.brightness_floor, v, u, cfg.gamma)
            out.append((*_rgb(h, s, nv), w))

        elif cfg.mode == "tint":
            hr, hg, hb = cfg.heat_color
            out.append((round(_lerp(r, hr, u)), round(_lerp(g, hg, u)), round(_lerp(b, hb, u)), w))

        else:  # replace
            ir, ig, ib = cfg.idle_color
            hr, hg, hb = cfg.heat_color
            out.append((round(_lerp(ir, hr, u)), round(_lerp(ig, hg, u)), round(_lerp(ib, hb, u)), 0))
    return out
