"""Frame coalescer with source priority, rate limiting, and multi-output support."""

import asyncio
import logging
import time

from ssh_transport import SSHTransport
from openrgb_output import OpenRGBOutput
from govee_output import GoveeOutput
from config import Config

log = logging.getLogger(__name__)

# Source priority: higher number = higher priority
SOURCE_EFFECT = 0
SOURCE_HTTP = 1
SOURCE_UDP = 2

# Govee ownership states
GOVEE_YIELDED = "yielded"      # SignalRGB is streaming and drives the lights itself
GOVEE_STREAMING = "streaming"  # we stream the effect to the lights
GOVEE_OFF = "off"              # we hold the lights off


class FrameCoalescer:
    """Merges frames from multiple sources and sends to all outputs at a capped rate.

    Frames are stored per-device as dict[device_name, list[RGBW tuples]].
    Devices include UniFi switches, OpenRGB, and Govee lights.

    Govee lights are shared with SignalRGB, which drives them directly through
    its own Govee addon. While SignalRGB streams to us (UDP active), we leave
    the lights alone. Otherwise we stream the effect to them when the movie
    flag is set, and hold them off when it is not.
    """

    def __init__(self, transport: SSHTransport, openrgb: OpenRGBOutput | None,
                 govee: GoveeOutput | None, config: Config):
        self._transport = transport
        self._openrgb = openrgb
        self._govee = govee
        self._config = config
        self._interval = 1.0 / config.max_fps

        # Per-device current frame state
        self._frames: dict[str, list[tuple[int, int, int, int]]] = {}
        for sw in config.switches:
            self._frames[sw.name] = [(0, 0, 0, 0)] * sw.num_ports
        if config.openrgb_enabled:
            self._frames[config.openrgb.name] = [(0, 0, 0, 0)] * config.openrgb.num_leds
        for gv in config.govee:
            self._frames[gv.name] = [(0, 0, 0, 0)] * gv.num_segments
        self._govee_names = {gv.name for gv in config.govee}

        # Govee ownership
        self._govee_movie = False
        self._govee_state: str | None = None
        self._govee_last_send = 0.0
        self._govee_last_frames: dict[str, list[tuple[int, int, int, int]]] = {}

        self._brightness: int = config.default_brightness
        self._dirty = False
        self._last_sent: dict[str, list[tuple[int, int, int, int]]] = {}

        # Source tracking
        self._active_source: int = SOURCE_EFFECT
        self._udp_last_seen: float = 0.0

        # Stats
        self._frames_sent: int = 0
        self._start_time: float = 0.0

    @property
    def active_source_name(self) -> str:
        if self._active_source == SOURCE_UDP:
            return "signalrgb"
        elif self._active_source == SOURCE_HTTP:
            return "http"
        return "effect"

    @property
    def fps(self) -> float:
        elapsed = time.monotonic() - self._start_time
        return self._frames_sent / elapsed if elapsed > 0 else 0.0

    @property
    def current_frames(self) -> dict[str, list[tuple[int, int, int, int]]]:
        return {k: list(v) for k, v in self._frames.items()}

    @property
    def brightness(self) -> int:
        return self._brightness

    @property
    def govee_movie(self) -> bool:
        return self._govee_movie

    @property
    def govee_state(self) -> str | None:
        return self._govee_state

    def set_govee_movie(self, active: bool):
        """Set whether the Govee lights should run the effect while SignalRGB is idle."""
        if active != self._govee_movie:
            log.info("Govee movie flag: %s", active)
        self._govee_movie = active

    def _udp_active(self) -> bool:
        return (time.monotonic() - self._udp_last_seen) < self._config.udp_timeout

    def _get_device_led_count(self, name: str) -> int:
        for sw in self._config.switches:
            if sw.name == name:
                return sw.num_ports
        if self._config.openrgb_enabled and name == self._config.openrgb.name:
            return self._config.openrgb.num_leds
        for gv in self._config.govee:
            if gv.name == name:
                return gv.num_segments
        return 0

    async def _govee_tick(self):
        """Decide who owns the Govee lights and act on ownership changes."""
        now = time.monotonic()
        # Give SignalRGB one timeout window after startup to show it is
        # streaming, so a restart doesn't switch its lights off.
        if now - self._start_time < self._config.udp_timeout:
            return

        if self._udp_active():
            want = GOVEE_YIELDED
        elif self._govee_movie:
            want = GOVEE_STREAMING
        else:
            want = GOVEE_OFF

        if want != self._govee_state:
            log.info("Govee: %s -> %s", self._govee_state, want)
            self._govee_state = want
            if want == GOVEE_STREAMING:
                await self._govee.activate()
                self._govee_last_frames = {}
            elif want == GOVEE_OFF:
                await self._govee.deactivate()
                self._govee_last_send = now
            # GOVEE_YIELDED: SignalRGB already took over, send nothing

        if want == GOVEE_STREAMING:
            frames = {n: self._frames[n] for n in self._govee_names}
            # Resend unchanged frames too: the lights drop out of streaming
            # mode when frames stop arriving.
            if frames != self._govee_last_frames or now - self._govee_last_send >= self._config.govee_keepalive:
                self._govee.send_frames(frames, self._brightness)
                self._govee_last_frames = {k: list(v) for k, v in frames.items()}
                self._govee_last_send = now
        elif want == GOVEE_OFF and self._config.govee_off_resend > 0:
            # Re-assert off, in case something powered the lights back on
            if now - self._govee_last_send >= self._config.govee_off_resend:
                await self._govee.deactivate()
                self._govee_last_send = now

    async def release_govee(self):
        """On shutdown, don't leave the lights frozen in streaming mode."""
        if self._govee and self._govee_state == GOVEE_STREAMING:
            await self._govee.deactivate()

    def set_switch_frame(self, device_name: str, colors: list[tuple[int, int, int, int]],
                         brightness: int | None = None, source: int = SOURCE_HTTP):
        """Update a single device's frame."""
        if source < SOURCE_UDP and self._udp_active():
            return

        if source == SOURCE_UDP:
            self._udp_last_seen = time.monotonic()

        if device_name in self._frames:
            count = self._get_device_led_count(device_name)
            while len(colors) < count:
                colors.append((0, 0, 0, 0))
            self._frames[device_name] = colors[:count]

        if brightness is not None:
            self._brightness = brightness
        self._active_source = source
        self._dirty = True

    def set_all_frames(self, frames: dict[str, list[tuple[int, int, int, int]]],
                       brightness: int | None = None, source: int = SOURCE_HTTP):
        """Update frames for all devices at once."""
        if source < SOURCE_UDP and self._udp_active():
            return

        if source == SOURCE_UDP:
            self._udp_last_seen = time.monotonic()

        for name, colors in frames.items():
            if name in self._frames:
                count = self._get_device_led_count(name)
                while len(colors) < count:
                    colors.append((0, 0, 0, 0))
                self._frames[name] = colors[:count]

        if brightness is not None:
            self._brightness = brightness
        self._active_source = source
        self._dirty = True

    async def run(self):
        """Main loop: send frames to all outputs at the capped rate."""
        self._start_time = time.monotonic()
        log.info("Frame coalescer running at %.1f fps max", self._config.max_fps)

        while True:
            if self._dirty and self._frames != self._last_sent:
                try:
                    # Split frames into switch frames and OpenRGB frames
                    switch_frames = {}
                    openrgb_frame = None
                    orgb_name = self._config.openrgb.name if self._config.openrgb_enabled else None

                    # Snapshot before awaiting: the effect task can replace
                    # self._frames mid-send, and that newer frame must not be
                    # recorded as sent.
                    snapshot = {k: list(v) for k, v in self._frames.items()}
                    self._dirty = False

                    for name, colors in snapshot.items():
                        if name == orgb_name:
                            openrgb_frame = colors
                        elif name not in self._govee_names:
                            switch_frames[name] = colors

                    # Send to both outputs concurrently
                    tasks = []
                    if switch_frames:
                        tasks.append(self._transport.send_frames(switch_frames, self._brightness))
                    if openrgb_frame and self._openrgb:
                        tasks.append(self._openrgb.send_frame(openrgb_frame, self._brightness))

                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)

                    self._last_sent = snapshot
                    self._frames_sent += 1
                except Exception as e:
                    log.warning("Frame send failed: %s", e)
            if self._govee:
                try:
                    await self._govee_tick()
                except Exception as e:
                    log.warning("Govee update failed: %s", e)
            await asyncio.sleep(self._interval)
