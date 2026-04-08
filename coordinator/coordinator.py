"""UniFi Etherlighting Coordinator — main entry point."""

import asyncio
import logging
import signal

from aiohttp import web

from config import Config
from canvas import Canvas
from ssh_transport import SSHTransport
from openrgb_output import OpenRGBOutput
from frame_coalescer import FrameCoalescer
from effects import EffectEngine
from http_api import create_app
from udp_listener import start_udp_listener
from wled_emulator import create_wled_routes, start_wled_udp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("coordinator")


async def main():
    config = Config.from_args()
    log.info("Loaded %d switch(es): %s", len(config.switches), [s.name for s in config.switches])

    # Build spatial canvas
    canvas = Canvas(config)
    log.info("Canvas: %d total pixels, bounds %s", len(canvas.pixels), canvas.bounds)

    # Connect to all switches
    transport = SSHTransport(config)
    log.info("Connecting to switches...")
    await transport.connect_all()

    # Connect to OpenRGB if configured
    openrgb = None
    if config.openrgb_enabled:
        openrgb = OpenRGBOutput(config)
        try:
            await openrgb.connect()
            log.info("OpenRGB: %d LEDs", openrgb.total_leds)
        except Exception as e:
            log.warning("OpenRGB connect failed, continuing without it: %s", e)
            openrgb = None

    # Set up frame coalescer and effects
    coalescer = FrameCoalescer(transport, openrgb, config)
    effects = EffectEngine(coalescer, canvas, config)

    # Set up HTTP API + WLED emulation endpoints
    app = create_app(coalescer, effects, transport, config)
    create_wled_routes(app, coalescer, config)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", config.http_port)
    await site.start()
    log.info("HTTP API on :%d (includes WLED /json endpoints)", config.http_port)

    # WLED HTTP for SignalRGB (default 0.0.0.0:80)
    if config.wled_bind_port:
        wled_site = web.TCPSite(runner, config.wled_bind_ip, config.wled_bind_port)
        await wled_site.start()
        log.info("WLED HTTP on %s:%d", config.wled_bind_ip, config.wled_bind_port)

    # Graceful shutdown
    shutdown_event = asyncio.Event()

    def handle_signal():
        log.info("Shutdown signal received")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, handle_signal)

    # Run everything concurrently
    coalescer_task = asyncio.create_task(coalescer.run())
    udp_task = asyncio.create_task(start_udp_listener(coalescer, config))
    wled_task = asyncio.create_task(start_wled_udp(coalescer, config))

    log.info("Etherlighting Coordinator running — %d switches, %d total pixels",
             len(config.switches), config.total_ports)

    await shutdown_event.wait()

    # Cleanup
    log.info("Shutting down...")
    coalescer_task.cancel()
    udp_task.cancel()
    wled_task.cancel()
    effects.stop()

    try:
        await asyncio.gather(coalescer_task, udp_task, wled_task, return_exceptions=True)
    except asyncio.CancelledError:
        pass

    await runner.cleanup()
    await transport.close_all()
    if openrgb:
        await openrgb.close()
    log.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(main())
