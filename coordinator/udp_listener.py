"""UDP listener for SignalRGB color frames."""

import asyncio
import json
import logging

from frame_coalescer import FrameCoalescer, SOURCE_UDP
from config import Config

log = logging.getLogger(__name__)


class UDPProtocol(asyncio.DatagramProtocol):
    def __init__(self, coalescer: FrameCoalescer, config: Config):
        self._coalescer = coalescer
        self._config = config

    def datagram_received(self, data: bytes, addr: tuple[str, int]):
        try:
            msg = json.loads(data)
            brightness = msg.get("brightness")

            if "frames" in msg:
                # Multi-switch: {"frames": {"switch-name": [[R,G,B,W], ...], ...}}
                frames = {}
                for name, colors in msg["frames"].items():
                    frames[name] = [tuple(c) for c in colors]
                self._coalescer.set_all_frames(frames, brightness, source=SOURCE_UDP)

            elif "ports" in msg:
                # Single-switch shorthand: {"ports": [[R,G,B,W], ...]}
                # Sends to the first switch
                colors = [tuple(c) for c in msg["ports"]]
                switch_name = msg.get("switch", self._config.switches[0].name)
                self._coalescer.set_switch_frame(switch_name, colors, brightness, source=SOURCE_UDP)

        except (json.JSONDecodeError, TypeError, ValueError, IndexError) as e:
            log.warning("Bad UDP frame from %s: %s", addr, e)

    def error_received(self, exc: Exception):
        log.warning("UDP error: %s", exc)


async def start_udp_listener(coalescer: FrameCoalescer, config: Config):
    """Start the UDP listener for SignalRGB frames."""
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: UDPProtocol(coalescer, config),
        local_addr=("0.0.0.0", config.udp_port),
    )
    log.info("UDP listener on :%d", config.udp_port)

    try:
        await asyncio.Future()  # run forever
    finally:
        transport.close()
