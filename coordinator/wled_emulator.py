"""WLED protocol emulator — makes the coordinator appear as a WLED device to SignalRGB.

Serves:
- GET /json/info  — device info (LED count, UDP port, etc.)
- GET /json/state — current state (on/off, brightness)
- POST /json/state — change state
- UDP DNRGB packets on port 21324 (WLED default)

SignalRGB's built-in WLED service will discover this via manual IP entry
and treat it as a standard WLED LED strip.
"""

import asyncio
import logging
import struct

from aiohttp import web

from frame_coalescer import FrameCoalescer, SOURCE_UDP
from config import Config

log = logging.getLogger(__name__)

WLED_UDP_PORT = 21324  # WLED default realtime UDP port


def _build_segments(config: Config) -> list[dict]:
    """Build WLED segment list from the device topology."""
    segments = []
    offset = 0

    for i, sw in enumerate(config.switches):
        segments.append({
            "id": i,
            "start": offset,
            "stop": offset + sw.num_ports,
            "len": sw.num_ports,
            "grp": 1, "spc": 0, "of": 0,
            "on": True, "bri": 255,
            "col": [[255, 255, 255], [0, 0, 0], [0, 0, 0]],
            "fx": 0, "sx": 128, "ix": 128, "pal": 0,
            "sel": True, "rev": False, "mi": False,
            "n": sw.name,
        })
        offset += sw.num_ports

    if config.openrgb_enabled:
        segments.append({
            "id": len(config.switches),
            "start": offset,
            "stop": offset + config.openrgb.num_leds,
            "len": config.openrgb.num_leds,
            "grp": 1, "spc": 0, "of": 0,
            "on": True, "bri": 255,
            "col": [[255, 255, 255], [0, 0, 0], [0, 0, 0]],
            "fx": 0, "sx": 128, "ix": 128, "pal": 0,
            "sel": True, "rev": False, "mi": False,
            "n": config.openrgb.name,
        })

    return segments


def create_wled_routes(app: web.Application, coalescer: FrameCoalescer, config: Config):
    """Add WLED HTTP endpoints to the existing aiohttp app."""

    app["wled_on"] = True
    app["wled_bri"] = 255

    async def json_info(request: web.Request) -> web.Response:
        """WLED /json/info endpoint — tells SignalRGB about our LEDs."""
        return web.json_response({
            "ver": "0.14.0",
            "vid": 2400000,
            "leds": {
                "count": config.total_ports,
                "pwr": 0,
                "fps": 8,
                "maxpwr": 0,
                "maxseg": len(config.switches) + (1 if config.openrgb_enabled else 0),
            },
            "str": False,
            "name": "Etherlighting",
            "udpport": WLED_UDP_PORT,
            "live": True,
            "lm": "",
            "lip": "",
            "ws": -1,
            "fxcount": 0,
            "palcount": 0,
            "wifi": {
                "bssid": "00:00:00:00:00:00",
                "rssi": -50,
                "signal": 80,
                "channel": 1,
            },
            "fs": {"u": 0, "t": 0, "pmt": 0},
            "ndc": 0,
            "arch": "custom",
            "core": "etherlighting",
            "lwip": 0,
            "freeheap": 100000,
            "uptime": 0,
            "opt": 0,
            "brand": "WLED",
            "product": "Etherlighting",
            "mac": "aabbccddeeff",
            "ip": config.wled_bind_ip if config.wled_bind_ip != "0.0.0.0" else "",
        })

    async def json_state(request: web.Request) -> web.Response:
        """WLED /json/state endpoint."""
        if request.method == "POST":
            try:
                body = await request.json()
                if "on" in body:
                    app["wled_on"] = body["on"]
                if "bri" in body:
                    app["wled_bri"] = body["bri"]
            except Exception:
                pass

        return web.json_response({
            "on": app["wled_on"],
            "bri": app["wled_bri"],
            "transition": 7,
            "ps": -1,
            "pl": -1,
            "nl": {"on": False, "dur": 60, "mode": 1, "tbri": 0, "rem": -1},
            "udpn": {"send": False, "recv": True},
            "lor": 0,
            "mainseg": 0,
            "seg": _build_segments(config),
        })

    async def json_combined(request: web.Request) -> web.Response:
        """WLED /json endpoint — combined info + state."""
        info_resp = await json_info(request)
        state_resp = await json_state(request)
        import json
        info = json.loads(info_resp.body)
        state = json.loads(state_resp.body)
        return web.json_response({"info": info, "state": state})

    app.router.add_get("/json/info", json_info)
    app.router.add_get("/json/info/", json_info)
    app.router.add_get("/json/state", json_state)
    app.router.add_get("/json/state/", json_state)
    app.router.add_post("/json/state", json_state)
    app.router.add_post("/json/state/", json_state)
    app.router.add_get("/json", json_combined)
    app.router.add_get("/json/", json_combined)


class WLEDUDPProtocol(asyncio.DatagramProtocol):
    """Receives WLED DNRGB UDP packets from SignalRGB.

    DNRGB format: [0x04, timeout, startIdx_high, startIdx_low, R, G, B, R, G, B, ...]
    """

    def __init__(self, coalescer: FrameCoalescer, config: Config):
        self._coalescer = coalescer
        self._config = config

    def datagram_received(self, data: bytes, addr: tuple[str, int]):
        if len(data) < 4:
            return

        protocol = data[0]

        if protocol == 4:  # DNRGB
            start_idx = (data[2] << 8) | data[3]
            rgb_data = data[4:]

            # Parse RGB triplets
            colors = []
            for i in range(0, len(rgb_data) - 2, 3):
                r, g, b = rgb_data[i], rgb_data[i + 1], rgb_data[i + 2]
                colors.append((r, g, b, 0))

            if not colors:
                return

            # Map flat LED list onto switches + openrgb
            self._dispatch_colors(colors, start_idx)

        elif protocol == 2:  # DRGB (no start index)
            rgb_data = data[2:]
            colors = []
            for i in range(0, len(rgb_data) - 2, 3):
                r, g, b = rgb_data[i], rgb_data[i + 1], rgb_data[i + 2]
                colors.append((r, g, b, 0))

            if colors:
                self._dispatch_colors(colors, 0)

    def _dispatch_colors(self, colors: list[tuple[int, int, int, int]], start_idx: int):
        """Map a flat color array onto the multi-device topology."""
        frames: dict[str, list[tuple[int, int, int, int]]] = {}

        # Build current state from coalescer
        for name, existing in self._coalescer.current_frames.items():
            frames[name] = list(existing)

        # Map colors sequentially across devices
        offset = 0
        for sw in self._config.switches:
            for port in range(sw.num_ports):
                global_idx = offset + port
                color_idx = global_idx - start_idx
                if 0 <= color_idx < len(colors):
                    if sw.name not in frames:
                        frames[sw.name] = [(0, 0, 0, 0)] * sw.num_ports
                    frames[sw.name][port] = colors[color_idx]
            offset += sw.num_ports

        # OpenRGB LEDs come after switches
        if self._config.openrgb_enabled:
            orgb = self._config.openrgb
            if orgb.name not in frames:
                frames[orgb.name] = [(0, 0, 0, 0)] * orgb.num_leds
            for led in range(orgb.num_leds):
                global_idx = offset + led
                color_idx = global_idx - start_idx
                if 0 <= color_idx < len(colors):
                    frames[orgb.name][led] = colors[color_idx]
            offset += orgb.num_leds

        self._coalescer.set_all_frames(frames, source=SOURCE_UDP)

    def error_received(self, exc: Exception):
        log.warning("WLED UDP error: %s", exc)


async def start_wled_udp(coalescer: FrameCoalescer, config: Config):
    """Start the WLED DNRGB UDP listener."""
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: WLEDUDPProtocol(coalescer, config),
        local_addr=("0.0.0.0", WLED_UDP_PORT),
    )
    log.info("WLED UDP listener on :%d", WLED_UDP_PORT)

    try:
        await asyncio.Future()
    finally:
        transport.close()
