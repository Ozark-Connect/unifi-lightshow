"""HTTP API for Home Assistant and manual control."""

import logging
import time

from aiohttp import web

from frame_coalescer import FrameCoalescer, SOURCE_HTTP
from effects import EffectEngine
from ssh_transport import SSHTransport
from config import Config

log = logging.getLogger(__name__)


def create_app(
    coalescer: FrameCoalescer,
    effects: EffectEngine,
    transport: SSHTransport,
    config: Config,
) -> web.Application:
    app = web.Application()
    app["coalescer"] = coalescer
    app["effects"] = effects
    app["transport"] = transport
    app["config"] = config
    app["start_time"] = time.monotonic()

    app.router.add_get("/health", health)
    app.router.add_get("/state", state)
    app.router.add_post("/control", control)
    app.router.add_post("/port", port)
    app.router.add_post("/ports", ports)
    app.router.add_post("/effect", effect)

    return app


async def health(request: web.Request) -> web.Response:
    transport: SSHTransport = request.app["transport"]
    coalescer: FrameCoalescer = request.app["coalescer"]
    config: Config = request.app["config"]
    uptime = time.monotonic() - request.app["start_time"]

    switches = {}
    for name, conn in transport.connections.items():
        switches[name] = {"connected": conn.connected, "ports": conn.switch.num_ports}

    return web.json_response({
        "status": "ok",
        "uptime": round(uptime),
        "fps": round(coalescer.fps, 1),
        "active_source": coalescer.active_source_name,
        "switches": switches,
        "total_ports": config.total_ports,
    })


async def state(request: web.Request) -> web.Response:
    coalescer: FrameCoalescer = request.app["coalescer"]
    effects: EffectEngine = request.app["effects"]
    frames = {}
    for name, colors in coalescer.current_frames.items():
        frames[name] = [list(c) for c in colors]
    return web.json_response({
        "active_source": coalescer.active_source_name,
        "active_effect": effects.active_effect,
        "brightness": coalescer.brightness,
        "frames": frames,
    })


async def control(request: web.Request) -> web.Response:
    body = await request.json()
    action = body.get("action")

    if action == "stop":
        # Stop any running effect — etherlightd resumes naturally
        request.app["effects"].stop()
    else:
        return web.json_response({"error": f"unknown action: {action}"}, status=400)

    return web.json_response({"status": "ok"})


async def port(request: web.Request) -> web.Response:
    body = await request.json()
    coalescer: FrameCoalescer = request.app["coalescer"]
    config: Config = request.app["config"]

    switch_name = body.get("switch", config.switches[0].name)
    p = body["port"] - 1
    r, g, b, w = body.get("r", 0), body.get("g", 0), body.get("b", 0), body.get("w", 0)
    level = body.get("level")

    frames = coalescer.current_frames
    if switch_name in frames:
        frame = frames[switch_name]
        frame[p] = (r, g, b, w)
        coalescer.set_switch_frame(switch_name, frame, level, source=SOURCE_HTTP)

    return web.json_response({"status": "ok"})


async def ports(request: web.Request) -> web.Response:
    body = await request.json()
    coalescer: FrameCoalescer = request.app["coalescer"]
    config: Config = request.app["config"]

    switch_name = body.get("switch", config.switches[0].name)
    colors = [tuple(c) for c in body["colors"]]
    level = body.get("level")
    coalescer.set_switch_frame(switch_name, colors, level, source=SOURCE_HTTP)

    return web.json_response({"status": "ok"})


async def effect(request: web.Request) -> web.Response:
    body = await request.json()
    effects: EffectEngine = request.app["effects"]

    effect_name = body.pop("effect", None)
    if not effect_name:
        return web.json_response({"error": "missing 'effect' field"}, status=400)

    if effect_name == "stop":
        effects.stop()
    else:
        effects.start(effect_name, **body)

    return web.json_response({"status": "ok"})
