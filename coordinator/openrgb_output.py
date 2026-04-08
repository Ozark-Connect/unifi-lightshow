"""OpenRGB output — drives ARGB LEDs on the NAS via OpenRGB."""

import asyncio
import logging

from openrgb import OpenRGBClient
from openrgb.utils import RGBColor

from config import Config, OpenRGBDeviceConfig

log = logging.getLogger(__name__)


class OpenRGBOutput:
    """Manages the OpenRGB connection and pushes color frames to ARGB devices."""

    def __init__(self, config: Config):
        self._config = config
        self._client: OpenRGBClient | None = None
        self._devices: list = []
        self._connected = False
        self._total_leds = 0

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def total_leds(self) -> int:
        return self._total_leds

    async def connect(self):
        """Connect to the OpenRGB server (blocking call run in executor)."""
        loop = asyncio.get_running_loop()
        try:
            self._client = await loop.run_in_executor(None, self._do_connect)
            self._connected = True
        except Exception as e:
            self._connected = False
            log.error("OpenRGB connect failed: %s", e)
            raise

    def _do_connect(self) -> OpenRGBClient:
        cfg = self._config.openrgb
        client = OpenRGBClient(cfg.host, cfg.port)

        self._devices = []
        self._total_leds = 0

        for device in client.devices:
            led_count = len(device.leds)
            if led_count == 0:
                continue

            # Set to direct mode
            try:
                device.set_mode("Direct")
            except Exception:
                for mode in device.modes:
                    if "direct" in mode.name.lower():
                        device.set_mode(mode.name)
                        break

            self._devices.append(device)
            self._total_leds += led_count
            log.info("OpenRGB device: %s (%d LEDs)", device.name, led_count)

        log.info("OpenRGB connected: %d devices, %d total LEDs", len(self._devices), self._total_leds)
        return client

    async def send_frame(self, colors: list[tuple[int, int, int, int]], brightness: int = 100):
        """Push colors to all OpenRGB devices. Colors is a flat list for all LEDs."""
        if not self._connected or not self._devices:
            return

        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._do_send, colors, brightness)

    def _do_send(self, colors: list[tuple[int, int, int, int]], brightness: int):
        scale = brightness / 100.0
        offset = 0
        for device in self._devices:
            led_count = len(device.leds)
            device_colors = []
            for i in range(led_count):
                if offset + i < len(colors):
                    r, g, b, _w = colors[offset + i]
                else:
                    r, g, b = 0, 0, 0
                device_colors.append(RGBColor(
                    int(r * scale),
                    int(g * scale),
                    int(b * scale),
                ))
            try:
                device.set_colors(device_colors)
            except Exception as e:
                log.warning("OpenRGB send failed for %s: %s", device.name, e)
            offset += led_count

    async def close(self):
        if self._client:
            # Set all LEDs to black
            try:
                await self.send_frame([(0, 0, 0, 0)] * self._total_leds, 100)
            except Exception:
                pass
            self._client = None
            self._connected = False
            log.info("OpenRGB connection closed")
