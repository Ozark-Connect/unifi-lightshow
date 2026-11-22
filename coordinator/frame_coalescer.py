"""Frame coalescer with source priority, rate limiting, and multi-output support."""

import asyncio
import logging
import time

from ssh_transport import SSHTransport
from openrgb_output import OpenRGBOutput
from govee_output import GoveeOutput
from config import Config
from traffic import TrafficMonitor, apply_layer

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
                 govee: GoveeOutput | None, config: Config,
                 traffic: TrafficMonitor | None = None):
        self._transport = transport
        self._openrgb = openrgb
        self._govee = govee
        self._config = config
        self._traffic = traffic
        if traffic:
            traffic.on_change = self.mark_dirty
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
        # SignalRGB presence, from its WLED HTTP requests and UDP frames
        self._signalrgb_claim_until = 0.0
        self._signalrgb_last_contact = 0.0
        self._signalrgb_streamed = False  # a UDP frame arrived since the last exit
        self._signalrgb_exited = False    # the exit POST arrived, no new session yet
        self._signalrgb_dropped_logged = False
        self.ping_presence = None  # PingPresence for the SignalRGB PC, set by main

        self._brightness: int = config.default_brightness
        self._dirty = False
        self._last_sent: dict[str, list[tuple[int, int, int, int]]] = {}
        self._switch_last_send = 0.0

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

    def note_signalrgb_contact(self, post: bool = False):
        """SignalRGB made a WLED HTTP request. Track whether it is running.

        SignalRGB's Govee addon drives the lights directly, so the
        coordinator only sees SignalRGB through its WLED traffic. Two gaps
        in that traffic leave both senders driving the lights, which strobes:

        - Startup: the first UDP frame arrives 2-6 s after the first HTTP
          request. A request while UDP is idle claims the lights for
          govee_claim_hold seconds.
        - Stream drop: the UDP stream can stop for 10-120 s while SignalRGB
          keeps running and polling. On exit SignalRGB sends POST
          /json/state 0-5 s before its stream stops; a drop sends nothing.
          Without that POST, the coordinator keeps yielding until SignalRGB
          is silent for govee_presence_hold seconds.

        A POST while UDP is active is taken as the exit. Any other request
        starts a new session.
        """
        now = time.monotonic()
        if post and self._udp_active():
            if not self._signalrgb_exited:
                log.info("SignalRGB exit request: releasing Govee when its stream stops")
            self._signalrgb_exited = True
            self._signalrgb_streamed = False
            return
        self._signalrgb_exited = False
        self._signalrgb_last_contact = now
        hold = self._config.govee_claim_hold
        if hold <= 0 or self._udp_active():
            return
        if now >= self._signalrgb_claim_until:
            log.info("SignalRGB contact: yielding Govee for %.0fs", hold)
        self._signalrgb_claim_until = now + hold

    def _note_signalrgb_frame(self):
        self._udp_last_seen = time.monotonic()
        self._signalrgb_last_contact = self._udp_last_seen
        self._signalrgb_claim_until = 0.0  # the stream it waited for has started
        if not self._signalrgb_exited:
            self._signalrgb_streamed = True
            self._signalrgb_dropped_logged = False

    def signalrgb_session_active(self) -> bool:
        """SignalRGB is streaming, or held through a drop: the PC is known to be on."""
        return self._udp_active() or self._signalrgb_present(time.monotonic())

    def _signalrgb_present(self, now: float) -> bool:
        """SignalRGB streamed this session, has not exited, and was heard from recently."""
        hold = self._config.govee_presence_hold
        if self._signalrgb_streamed and now - self._signalrgb_last_contact >= hold:
            # Silent too long: the session is over, so a later stray request
            # gets only the startup claim, not this hold.
            self._signalrgb_streamed = False
        return hold > 0 and self._signalrgb_streamed and not self._signalrgb_exited

    def mark_dirty(self):
        """Recompute the output on the next tick, e.g. when traffic changed."""
        self._dirty = True

    def _output_snapshot(self) -> dict[str, list[tuple[int, int, int, int]]]:
        """Copy of the current frames with the traffic layer applied to switch frames.

        Copy before awaiting: the effect task can replace self._frames
        mid-send, and that newer frame must not be recorded as sent.
        """
        frames = {k: list(v) for k, v in self._frames.items()}
        t = self._traffic
        if not t or not t.cfg.enabled or self.active_source_name not in t.cfg.sources:
            return frames
        for sw in self._config.switches:
            utils = t.utils(sw.name)
            if utils is not None and sw.name in frames:
                frames[sw.name] = apply_layer(frames[sw.name], utils, t.cfg)
        return frames

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

        if self.ping_presence is not None and self.ping_presence.present:
            # The SignalRGB PC is on: its Govee addon may drive the lights
            # at any moment, before any WLED traffic reaches us.
            want = GOVEE_YIELDED
        elif self._udp_active() or now < self._signalrgb_claim_until:
            want = GOVEE_YIELDED
        elif self._signalrgb_present(now):
            if not self._signalrgb_dropped_logged:
                log.info("SignalRGB stream stopped without exit: keeping Govee yielded "
                         "until SignalRGB is silent for %.0fs", self._config.govee_presence_hold)
                self._signalrgb_dropped_logged = True
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
            self._note_signalrgb_frame()

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
            self._note_signalrgb_frame()

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
            # Resend the current frame now and then, with force set, even when
            # it hasn't changed. A static frame (off, solid) is otherwise sent
            # once, so a lost packet, a daemon restart, or etherlightd
            # repainting its defaults after a reboot would stick.
            refresh_due = (self._config.switch_refresh > 0 and
                           time.monotonic() - self._switch_last_send >= self._config.switch_refresh)
            # _last_sent holds what went out, after the traffic layer, so a
            # traffic change alone is a change worth sending.
            snapshot = None
            if self._dirty:
                self._dirty = False
                snapshot = self._output_snapshot()
            changed = snapshot is not None and snapshot != self._last_sent
            if changed or (refresh_due and self._last_sent):
                try:
                    if snapshot is None:
                        snapshot = self._output_snapshot()

                    # Split frames into switch frames and OpenRGB frames
                    switch_frames = {}
                    openrgb_frame = None
                    orgb_name = self._config.openrgb.name if self._config.openrgb_enabled else None

                    for name, colors in snapshot.items():
                        if name == orgb_name:
                            openrgb_frame = colors
                        elif name not in self._govee_names:
                            switch_frames[name] = colors

                    # Send to both outputs concurrently
                    tasks = []
                    if switch_frames:
                        tasks.append(self._transport.send_frames(switch_frames, self._brightness,
                                                                 force=refresh_due))
                        if refresh_due:
                            self._switch_last_send = time.monotonic()
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
