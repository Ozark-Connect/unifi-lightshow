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


@dataclass
class Config:
    switches: list[SwitchConfig] = field(default_factory=list)
    openrgb: OpenRGBDeviceConfig = field(default_factory=OpenRGBDeviceConfig)
    openrgb_enabled: bool = False
    http_port: int = 9199
    udp_port: int = 9200
    max_fps: float = 10.0
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

        if "http_port" in data:
            self.http_port = data["http_port"]
        if "udp_port" in data:
            self.udp_port = data["udp_port"]
        if "max_fps" in data:
            self.max_fps = data["max_fps"]
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
