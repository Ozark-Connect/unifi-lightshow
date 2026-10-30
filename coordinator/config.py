"""Configuration for the Etherlighting Coordinator."""

import argparse
import json
import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class SwitchConfig:
    """Configuration for a single UniFi switch."""
    name: str
    host: str
    user: str = "tjvc4"
    num_ports: int = 10
    x: float = 0.0
    y: float = 0.0
    rotation: float = 0.0
    mirror: bool = False
    port_spacing: float = 1.0


@dataclass
class OpenRGBDeviceConfig:
    """Configuration for the OpenRGB connection."""
    host: str = "127.0.0.1"
    port: int = 6742
    name: str = "openrgb"
    num_leds: int = 8  # will be auto-detected on connect, this is for canvas layout
    x: float = 0.0
    y: float = 2.0  # offset below the switches on the canvas
    rotation: float = 0.0
    led_spacing: float = 1.0


GOVEE_SHAPES = ("strip", "path", "loop")
GOVEE_PROTOCOLS = ("dreamview", "razer")
LOOP_EDGES = ("bottom", "right", "top", "left")


@dataclass
class GoveeDeviceConfig:
    """Configuration for a Govee light driven over the LAN API.

    The device stretches whatever segment count it receives across its
    physical LEDs, so num_segments sets canvas resolution, not a hardware
    count. Segment positions on the canvas come from one of three shapes:

    - strip: a straight line from (x, y), like a switch or OpenRGB device
    - path:  segments spaced evenly by arc length along a polyline, first
             segment on the first point, last segment on the last point
    - loop:  segments spaced evenly around the perimeter of a rectangle,
             starting at a fraction along one edge (TV backlights)
    """
    name: str
    ip: str
    num_segments: int = 15
    protocol: str = "dreamview"
    brightness: int = 100  # percent of the coordinator brightness
    shape: str = "strip"
    # strip
    x: float = 0.0
    y: float = 0.0
    rotation: float = 0.0
    led_spacing: float = 1.0
    # path
    path: list[tuple[float, float]] = field(default_factory=list)
    # loop: rect is (x0, y0, x1, y1) with y increasing downward, so "bottom"
    # is the y1 edge. start is the fraction along start_edge, in the travel
    # direction, where segment 0 sits. direction is as seen facing the device.
    rect: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)
    start_edge: str = "bottom"
    start: float = 0.0
    direction: str = "ccw"


@dataclass
class Config:
    switches: list[SwitchConfig] = field(default_factory=list)
    openrgb: OpenRGBDeviceConfig = field(default_factory=OpenRGBDeviceConfig)
    openrgb_enabled: bool = False
    # Govee devices are effect-only outputs: they are not part of total_ports,
    # so adding one never changes the LED count SignalRGB sees over WLED.
    govee: list[GoveeDeviceConfig] = field(default_factory=list)
    govee_keepalive: float = 1.0
    govee_off_resend: float = 3.0
    http_port: int = 9199
    udp_port: int = 9200
    max_fps: float = 10.0
    switch_refresh: float = 5.0
    default_brightness: int = 100
    udp_timeout: float = 5.0
    ssh_keepalive: int = 15
    ssh_known_hosts: str | None = None
    default_effect: str | None = "plasma"
    wled_bind_ip: str = "0.0.0.0"
    wled_bind_port: int = 80

    @property
    def total_ports(self) -> int:
        total = sum(s.num_ports for s in self.switches)
        if self.openrgb_enabled:
            total += self.openrgb.num_leds
        return total

    @classmethod
    def from_args(cls) -> "Config":
        parser = argparse.ArgumentParser(description="UniFi Etherlighting Coordinator")
        parser.add_argument("--config", type=str, help="Path to config JSON file")
        parser.add_argument("--switch-host", default=os.environ.get("ETHERLIGHT_SWITCH_HOST"))
        parser.add_argument("--switch-user", default=os.environ.get("ETHERLIGHT_SWITCH_USER", "tjvc4"))
        parser.add_argument("--http-port", type=int, default=int(os.environ.get("ETHERLIGHT_HTTP_PORT", 9199)))
        parser.add_argument("--udp-port", type=int, default=int(os.environ.get("ETHERLIGHT_UDP_PORT", 9200)))
        parser.add_argument("--max-fps", type=float, default=float(os.environ.get("ETHERLIGHT_MAX_FPS", 10.0)))
        parser.add_argument("--brightness", type=int, default=int(os.environ.get("ETHERLIGHT_BRIGHTNESS", 100)))
        parser.add_argument("--udp-timeout", type=float, default=float(os.environ.get("ETHERLIGHT_UDP_TIMEOUT", 0.5)))
        args = parser.parse_args()

        cfg = cls(
            http_port=args.http_port,
            udp_port=args.udp_port,
            max_fps=args.max_fps,
            default_brightness=args.brightness,
            udp_timeout=args.udp_timeout,
        )

        if args.config:
            cfg._load_config_file(args.config)
        elif args.switch_host:
            cfg.switches.append(SwitchConfig(
                name=args.switch_host,
                host=args.switch_host,
                user=args.switch_user,
            ))
        else:
            cfg.switches.append(SwitchConfig(
                name="switch-tiny-home-1",
                host="switch-tiny-home-1",
            ))

        return cfg

    def _load_config_file(self, path: str):
        data = json.loads(Path(path).read_text())

        for sw in data.get("switches", []):
            self.switches.append(SwitchConfig(
                name=sw["name"],
                host=sw["host"],
                user=sw.get("user", "tjvc4"),
                num_ports=sw.get("num_ports", 10),
                x=sw.get("x", 0.0),
                y=sw.get("y", 0.0),
                rotation=sw.get("rotation", 0.0),
                mirror=sw.get("mirror", False),
                port_spacing=sw.get("port_spacing", 1.0),
            ))

        if "openrgb" in data:
            orgb = data["openrgb"]
            self.openrgb_enabled = True
            self.openrgb = OpenRGBDeviceConfig(
                host=orgb.get("host", "127.0.0.1"),
                port=orgb.get("port", 6742),
                name=orgb.get("name", "openrgb"),
                num_leds=orgb.get("num_leds", 8),
                x=orgb.get("x", 0.0),
                y=orgb.get("y", 2.0),
                rotation=orgb.get("rotation", 0.0),
                led_spacing=orgb.get("led_spacing", 1.0),
            )

        for gv in data.get("govee", []):
            dev = GoveeDeviceConfig(
                name=gv["name"],
                ip=gv["ip"],
                num_segments=gv.get("num_segments", 15),
                protocol=gv.get("protocol", "dreamview"),
                brightness=gv.get("brightness", 100),
                shape=gv.get("shape", "strip"),
                x=gv.get("x", 0.0),
                y=gv.get("y", 0.0),
                rotation=gv.get("rotation", 0.0),
                led_spacing=gv.get("led_spacing", 1.0),
                path=[tuple(p) for p in gv.get("path", [])],
                rect=tuple(gv.get("rect", (0.0, 0.0, 1.0, 1.0))),
                start_edge=gv.get("start_edge", "bottom"),
                start=gv.get("start", 0.0),
                direction=gv.get("direction", "ccw"),
            )
            if dev.shape not in GOVEE_SHAPES:
                raise ValueError(f"govee {dev.name}: shape must be one of {GOVEE_SHAPES}")
            if dev.protocol not in GOVEE_PROTOCOLS:
                raise ValueError(f"govee {dev.name}: protocol must be one of {GOVEE_PROTOCOLS}")
            if dev.shape == "path" and len(dev.path) < 2:
                raise ValueError(f"govee {dev.name}: path shape needs at least 2 points")
            if dev.shape == "loop":
                if len(dev.rect) != 4:
                    raise ValueError(f"govee {dev.name}: rect must be [x0, y0, x1, y1]")
                if dev.start_edge not in LOOP_EDGES:
                    raise ValueError(f"govee {dev.name}: start_edge must be one of {LOOP_EDGES}")
                if dev.direction not in ("cw", "ccw"):
                    raise ValueError(f"govee {dev.name}: direction must be cw or ccw")
            if not 0 <= dev.brightness <= 100:
                raise ValueError(f"govee {dev.name}: brightness must be 0-100")
            if not 1 <= dev.num_segments <= 255:
                raise ValueError(f"govee {dev.name}: num_segments must be 1-255")
            self.govee.append(dev)

        if "govee_keepalive" in data:
            self.govee_keepalive = data["govee_keepalive"]
        if "govee_off_resend" in data:
            self.govee_off_resend = data["govee_off_resend"]

        if "http_port" in data:
            self.http_port = data["http_port"]
        if "udp_port" in data:
            self.udp_port = data["udp_port"]
        if "max_fps" in data:
            self.max_fps = data["max_fps"]
        if "switch_refresh" in data:
            self.switch_refresh = data["switch_refresh"]
        if "brightness" in data:
            self.default_brightness = data["brightness"]
        if "udp_timeout" in data:
            self.udp_timeout = data["udp_timeout"]
        if "default_effect" in data:
            self.default_effect = data["default_effect"]
        if "wled_bind_ip" in data:
            self.wled_bind_ip = data["wled_bind_ip"]
        if "wled_bind_port" in data:
            self.wled_bind_port = data["wled_bind_port"]
