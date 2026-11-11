# UniFi Lightshow

Custom RGB light show controller for UniFi Etherlighting-enabled switches (USW-Pro series). Overrides the built-in Etherlighting LEDs with custom effects, SignalRGB integration, optional OpenRGB support for ARGB devices, and optional Govee LAN lights.

## Demo

<table align="center"><tr>
<td><video src="https://github.com/user-attachments/assets/af46b556-278f-4ede-856e-adaa0f002f31" width="270"></video></td>
<td><video src="https://github.com/user-attachments/assets/ee134b23-1b6d-43b2-b326-22849dd86ca6" width="270"></video></td>
<td><video src="https://github.com/user-attachments/assets/5d08eaa3-caaa-48ac-bdc9-3a9c4611f498" width="270"></video></td>
</tr></table>

<p align="center"><a href="https://imgur.com/a/unifi-lightshow-demo-IfBiL7b">More examples</a></p>

## What It Does

- Takes over the per-port RGB LEDs on UniFi Pro switches
- Drives custom lighting effects across multiple switches as a unified spatial canvas
- Emulates a WLED device so **SignalRGB** controls it natively - no custom plugin needed
- Falls back to built-in effects (plasma waves, seasonal themes, time-of-day brightness) when SignalRGB isn't streaming
- Optionally controls OpenRGB ARGB devices (motherboard LEDs, fans, etc.) on the same canvas
- Optionally drives Govee Wi-Fi lights on the same canvas, and hands them to SignalRGB while it streams
- Optionally makes each port react to its own traffic, on top of the effect or SignalRGB scene
- Exposes an HTTP API for Home Assistant integration

## Architecture

```
SignalRGB (Windows)                    Home Assistant
    |                                       |
    WLED DNRGB UDP (:21324)                 HTTP REST API (:9199)
    |                                       |
    +--------------- NAS -------------------+
    |        Coordinator (Docker)           |
    |    Effects | Coalescer | WLED Emu     |
    |              |                        |
    |    UDP :9200 to each switch           |
    |    OpenRGB SDK :6742 (optional)       |
    |    Govee LAN API UDP :4003 (optional) |
    +---------------------------------------+
         |                    |
    Switch 1              Switch 2           ...
    etherlightd_udp       etherlightd_udp
    (libubus C daemon)    (libubus C daemon)
         |                    |
    I2C → MCU → LEDs     I2C → MCU → LEDs
```

### How it's efficient

The coordinator runs on a server/NAS and sends UDP color frames to lightweight C daemons deployed on each switch. These daemons call the `libubus` C API directly — no shell spawning, no fork/exec overhead. The daemon uses delta updates with a color threshold, so only ports whose color actually changed get updated.

**Transport comparison** (USW-Pro-XG-8-PoE, 10 ports):

| Approach | CPU over baseline | Load |
|----------|-------------------|------|
| SSH + shell commands @ 3fps | ~56% | 1.6 |
| libubus C daemon @ 8fps | **1-7%** | **0.86** |

The switch has a baseline CPU usage of ~8% from Realtek ASIC management kernel threads (port stats, link monitoring, STP, etc.). The numbers above are the additional CPU cost on top of that baseline. When idle (no UDP frames), the daemon contributes 0% CPU.

**Effect CPU impact over baseline** varies by how many LEDs change per frame:

| Source | CPU over baseline | Why |
|--------|-------------------|-----|
| SignalRGB (WLED) | ~1% | Smooth gradients — most ports unchanged between frames, delta threshold skips them |
| `color_cycle` | ~3-4% | All ports same color — changes infrequently due to quantization |
| `plasma` | ~5-6% | Every port changes every frame (continuous sine wave) |
| Stock Etherlighting | 0% | MCU runs animation autonomously, no CPU involvement |

> **Tip:** If you want minimal switch CPU impact, use `color_cycle` as your default effect or connect SignalRGB. The `plasma` effect looks great but costs more because it updates every LED every frame.
>
> **Should I worry about switch CPU?** No. The MIPS CPU in these switches handles the management plane only — controller communication, STP, LLDP, SNMP, and optionally Layer 3 routing. All Layer 2 switching and forwarding happens in the switching ASIC at line rate regardless of CPU load. Even `plasma` at ~14% total (~6% over baseline) leaves plenty of headroom for management tasks that are already very lightweight on these devices.

## Supported Hardware

Tested on **USW-Pro-XG-8-PoE** (10 addressable RGB ports: 8 RJ45 + 2 SFP+). Should work on any UniFi switch with Etherlighting that uses the `ubus etherlight.mcu` interface.

## Quick Start

### 1. Configure

Copy `coordinator/switches.example.json` to `coordinator/switches.json` and edit:

```json
{
  "switches": [
    {
      "name": "my-switch",
      "host": "10.42.1.10",
      "user": "your-ssh-username",
      "num_ports": 10
    }
  ],
  "wled_bind_ip": "0.0.0.0",
  "wled_bind_port": 80,
  "max_fps": 8,
  "switch_refresh": 5.0,
  "brightness": 100,
  "udp_timeout": 5.0,
  "default_effect": "plasma"
}
```

`switch_refresh` is the number of seconds between forced resends of the current frame to the switches. A static frame such as `off` is otherwise sent once. If that packet is lost, or etherlightd repaints its default colors (for example, after a firmware upgrade reboots the switch), the switch keeps the wrong colors. 0 disables the resend.

`wled_bind_ip` and `wled_bind_port` set where the WLED HTTP endpoints listen for SignalRGB, which expects port 80. If another service already holds port 80 on the host (a reverse proxy, for example), bind to one interface's address instead of `0.0.0.0`. The WLED UDP stream always listens on 21324.

### 2. Build the switch daemon

The on-switch daemon must be cross-compiled for MIPS soft-float (the switch's CPU architecture). Make sure this matches your particular Etherlighting switch's architecture. The research notes may be helpful in figuring that out if you're unsure.

```bash
# Download musl cross-compiler
wget -O /tmp/mips-musl.tgz 'https://musl.cc/mips-linux-muslsf-cross.tgz'
tar xzf /tmp/mips-musl.tgz -C /tmp/

# Build
/tmp/mips-linux-muslsf-cross/bin/mips-linux-muslsf-gcc \
    -O2 -Wl,--dynamic-linker=/lib/ld-musl-mips-sf.so.1 \
    -Wl,--unresolved-symbols=ignore-all \
    -o switch-daemon/etherlightd_udp switch-daemon/etherlightd_udp.c \
    -Wl,-rpath,/lib
```

### 3. Set up SSH access to switches

The coordinator SSHes into each switch on startup to bootstrap the daemon. You need to add your SSH public key to UniFi Network so the coordinator can connect:

1. In UniFi Network: **UniFi Devices → Device Updates and Settings → Device SSH Settings → SSH Keys**
2. Add the public key of the machine running the coordinator
3. Use the **Username** you configured in UniFi Network as the `user` field in `switches.json`

### 4. Start the coordinator

```bash
cd coordinator
cp switches.example.json switches.json  # edit with your switch IPs/usernames
cp ../switch-daemon/etherlightd_udp .
cp ../switch-daemon/start_etherlightd_udp.sh .
docker compose up -d --build
```

On startup, the coordinator SSHes into each switch. If the daemon binary isn't present in `/etc/persistent/`, it automatically deploys the binary and start script via SCP. Then it runs the start script, which also sets up a cron job to auto-start the daemon after reboots. After bootstrap, all communication switches to UDP.

> **Manual deploy:** If you need to redeploy manually (e.g., after rebuilding the binary), you can SCP directly:
> ```bash
> scp switch-daemon/etherlightd_udp user@switch:/etc/persistent/
> scp switch-daemon/start_etherlightd_udp.sh user@switch:/etc/persistent/
> ssh user@switch "chmod +x /etc/persistent/etherlightd_udp /etc/persistent/start_etherlightd_udp.sh"
> ssh user@switch "/etc/persistent/start_etherlightd_udp.sh"
> ```

### 5. Connect SignalRGB

In SignalRGB, go to the WLED service and add a device by IP. Enter the IP where the coordinator's WLED port 80 is bound. SignalRGB will discover it as a WLED device with your configured LED count.

> **Tip:** If you don't want to bind port 80 directly to the coordinator, you can use a reverse proxy (e.g., Traefik, Nginx, Caddy) to expose port 80 to SignalRGB and route traffic to the coordinator's WLED listener.

## Built-in Effects

When SignalRGB isn't streaming, the coordinator runs fallback effects:

| Effect | Description |
|--------|-------------|
| `plasma` | Plasma wave with seasonal colors and time-of-day brightness |
| `rainbow` | Animated rainbow cycle across all devices |
| `palette_cycle` | Smooth cycle through UniFi speed-mode colors |
| `palette_sweep` | Each color sweeps across the canvas in sequence |
| `sweep` | Band of color bounces back and forth |
| `chase` | Single lit pixel with trailing dim pixel |
| `breathe` | All ports pulse together |
| `solid` | All ports one color |
| `off` | All dark |

### Seasonal themes (plasma effect)

| Season | Dates | Colors |
|--------|-------|--------|
| Halloween | Oct 1 - Nov 1 | Orange & Purple |
| Christmas | Nov 29 - Dec 26 | Green & Red |
| New Year's | Dec 27 - Jan 2 | White & Blue |
| Valentine's | Feb 7 - Feb 14 | Red & Pink |
| St. Patrick's | Mar 14 - Mar 17 | Green & Light Green |
| Default | Rest of year | Teal & Violet |

### Time-of-day brightness (plasma effect)

| Time | Brightness |
|------|-----------|
| 00:30 - 07:00 | OFF |
| 07:00 - 08:00 | 50% |
| 08:00 - 20:15 | 100% |
| 20:15 - 23:00 | 75% |
| 23:00 - 00:30 | 50% |

## HTTP API

```
GET  /health               - status, fps, connected switches
GET  /state                - current colors, active effect, active source
POST /effect               - start an effect: {"effect": "plasma"}
POST /control              - {"action": "stop"} to stop effects
POST /port                 - set one port: {"port": 1, "r": 255, "g": 0, "b": 0}
POST /ports                - set all ports: {"colors": [[255,0,0,0], ...]}
POST /govee/movie          - Govee movie flag: {"active": true}
GET  /traffic              - per-port Mbps, utilization, and traffic layer settings
POST /traffic              - change traffic settings: {"enabled": true, "mode": "boost"} (not saved)
```

When Govee devices are configured, `/health` and `/state` include a `govee` object with the movie flag, the ownership state, and the device IPs. Without Govee devices it is `null`.

## Traffic Reactivity

Each port can react to its own traffic, as a layer on top of whatever the show is painting. The effect or SignalRGB scene keeps running.

The switch daemon reads the per-port counters in `/proc/port/<N>` on the switch every 250 ms. It sends them back to the coordinator on the same UDP flow the frames use, as a reply to the last frame's source address and port. Polling never crosses the network, and the reply needs no firewall rule. The coordinator turns the counters into a rate per port, smoothed over about 1 second.

Utilization is log-scaled against the port's own link speed: `u = log(rate / 1 Mbps) / log(link speed / 1 Mbps)`, using the larger of rx and tx. On a 10G port, 100 Mbps is `u = 0.5` and line rate is `u = 1`. A linear scale would leave normal home traffic invisible.

| Mode | What a port shows |
|---|---|
| `boost` (default) | The show's own hue. Idle ports sit dimmer and less saturated; full utilization takes the port to full brightness and a more saturated version of the same hue. Brightness steps follow a gamma curve, so magnitude reads across ports |
| `brightness` | The show's color, brightness scaled by utilization |
| `tint` | The show's color blended toward `heat_color` |
| `replace` | `idle_color` to `heat_color` by utilization; the show is ignored |

Ports without link stay dark: the LED MCU blanks them whatever color it receives.

```json
{
  "traffic": {
    "enabled": true,
    "mode": "boost",
    "sources": ["effect", "http", "signalrgb"],
    "relative": false,
    "idle_value": 0.3,
    "idle_saturation": 0.7
  }
}
```

| Field | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Apply the layer. Stats flow either way, so `GET /traffic` always works |
| `mode` | `boost` | See the table above |
| `sources` | all | Which frame sources get the layer: `effect`, `http`, `signalrgb` |
| `floor_mbps` | `1.0` | Rate that maps to `u = 0` |
| `relative` | `false` | Divide every port by the busiest port (while the busiest is above `relative_min`, 0.1), so the busiest always reaches full |
| `smoothing` | `1.0` | Rate smoothing time constant, seconds |
| `idle_value`, `idle_saturation` | `0.3`, `0.7` | `boost`: idle brightness and saturation, as fractions of the show's |
| `saturation_boost` | `0.5` | `boost`: how far full utilization pushes saturation toward 1 |
| `gamma` | `2.2` | Brightness curve for `boost` and `brightness` |
| `brightness_floor` | `0.15` | `brightness`: idle brightness as a fraction of the show's |
| `min_value` | `0.10` | `boost` and `brightness`: lowest idle brightness, so idle ports stay visible over dim scenes. Never above the show's own brightness; black stays black |
| `heat_color`, `idle_color` | red, blue | `tint` and `replace` colors |
| `stale_after` | `3.0` | Seconds without stats before a switch falls back to the plain show |

Cost on the switch: one 400-byte packet out every 250 ms, and nothing in. The switch CPU path accepts only about 100 packets/s inbound (see [RESEARCH.md](RESEARCH.md)), so a stats source that polled the switch over the network would spend that budget. Traffic changes are also color changes, which add `port_rgb` calls; the daemon's color threshold and the smoothing keep those bounded.

Set the daemon's second argument to `0` to turn stats off on a switch: `etherlightd_udp 9200 0`.

Govee lights are not switch ports, so the traffic layer never changes them.

## Multi-Switch Spatial Canvas

Multiple switches can be positioned on a 2D canvas. Effects render spatially - a rainbow sweep flows continuously across switches based on their physical position:

```json
{
  "switches": [
    {"name": "rack-top",    "host": "10.0.0.1", "x": 0,  "y": 0},
    {"name": "rack-bottom", "host": "10.0.0.2", "x": 0,  "y": 1},
    {"name": "desk",        "host": "10.0.0.3", "x": 10, "y": 0, "rotation": 180, "mirror": true}
  ]
}
```

## OpenRGB Integration

Add an `openrgb` section to your config to include ARGB devices on the same canvas:

```json
{
  "openrgb": {
    "host": "127.0.0.1",
    "port": 6742,
    "name": "motherboard",
    "num_leds": 8,
    "x": 5.0,
    "y": 2.0
  }
}
```

Requires OpenRGB running on the same host as the coordinator.

## Govee Integration

This integration is optional. Without a `govee` list in the config, the coordinator sends nothing to Govee devices and `POST /govee/movie` returns 404.

Add a `govee` list to drive Govee Wi-Fi lights over the Govee LAN API. The lights sit on the same canvas as the switches and show the current effect. The coordinator uses the same streaming mode as the SignalRGB Govee addon, so this targets lights that SignalRGB also drives directly. Tested on the H6061 (Glide Hexa panels) and H6168 (TV backlight).

The coordinator decides who owns the lights on every frame:

| SignalRGB streaming to the coordinator | Movie flag | Coordinator does to the Govee lights |
|---|---|---|
| yes | any | Nothing. SignalRGB owns them. |
| no | on | Turns them on and streams the current effect. |
| no | off | Turns them off, and re-sends off every `govee_off_resend` seconds. |

The `govee.state` field in `/health` shows the result: `yielded`, `streaming`, or `off`. It is `null` for one `udp_timeout` after startup. During that window the coordinator does not touch the lights, so a restart while SignalRGB is streaming does not switch them off.

### Movie flag

The movie flag tells the coordinator whether to run the effect on the Govee lights while SignalRGB is idle. Tie it to any condition, for example a TV or media player that is playing. Set it with `POST /govee/movie`:

```bash
curl -X POST http://<coordinator>:9199/govee/movie -d '{"active": true}'
```

`active` takes a JSON boolean, or one of the strings `"true"`, `"on"`, `"1"` (true) and anything else (false). The flag lives in memory and resets to off when the coordinator restarts, so resend it periodically.

A Home Assistant example that sends the state of a binary sensor on every change and once a minute:

```yaml
rest_command:
  lightshow_govee_movie:
    url: "http://<coordinator>:9199/govee/movie"
    method: POST
    content_type: "application/json"
    payload: '{"active": "{{ states(''binary_sensor.media_playing'') }}"}'

automation:
  - alias: Lightshow Govee movie sync
    triggers:
      - trigger: state
        entity_id: binary_sensor.media_playing
      - trigger: time_pattern
        minutes: "/1"
    actions:
      - action: rest_command.lightshow_govee_movie
```

### Govee configuration

```json
{
  "govee": [
    {
      "name": "hexa",
      "ip": "10.42.1.60",
      "num_segments": 15,
      "shape": "path",
      "path": [[2.0, 8.5], [3.2, 8.5], [4.6, 8.3], [6.2, 8.8], [6.2, 10.4]]
    },
    {
      "name": "tv",
      "ip": "10.42.1.61",
      "num_segments": 10,
      "shape": "loop",
      "rect": [6.5, 8.2, 15.5, 13.7],
      "start_edge": "bottom",
      "start": 0.4,
      "direction": "ccw"
    }
  ],
  "govee_keepalive": 1.0,
  "govee_off_resend": 3.0
}
```

| Field | Default | Meaning |
|---|---|---|
| `name`, `ip` | required | Device name on the canvas and its LAN IP. LAN Control must be enabled in the Govee app. |
| `num_segments` | 15 | Colors sent per frame. The device stretches them across its physical LEDs, so this sets canvas resolution, not a hardware count. |
| `brightness` | 100 | Percent of the coordinator brightness, to balance a bright device against the switches. |
| `protocol` | `dreamview` | `dreamview` or `razer`. Use whichever the SignalRGB Govee addon uses for the SKU. |
| `shape` | `strip` | `strip`: a straight line from `x`, `y` with `rotation` and `led_spacing`. `path`: segments spaced evenly along the `path` polyline, segment 0 on the first point. `loop`: segments spaced evenly around the `rect` perimeter (`[x0, y0, x1, y1]`, y increases downward). |
| `start_edge`, `start` | `bottom`, 0.0 | Loop only. Segment 0 sits at fraction `start` along `start_edge`, measured in the travel direction. |
| `direction` | `ccw` | Loop only. Travel direction as seen facing the device. `ccw` runs left to right along the bottom. |
| `govee_keepalive` | 1.0 | Seconds between frame resends while streaming. Govee lights leave streaming mode when frames stop. |
| `govee_off_resend` | 3.0 | Seconds between off commands while holding the lights off. Govee lights can power themselves back on (for example, when SignalRGB's addon leaves streaming mode on exit), so a short interval keeps any flash brief. 0 disables the resend. |

To find a device's segment order, light one segment at a time and note where it appears.

Govee segments are not counted in the WLED LED count, so adding a Govee device does not change what SignalRGB sees. The coordinator only sends to UDP 4003 and never binds 4002 (the Govee reply port), so it runs next to other LAN API integrations such as govee2mqtt.

## Important Notes

- **Safe commands**: Only `port_rgb` and `behavior: steady` are used. These are safe and don't corrupt the switch's MCU state.
- **Dangerous commands (avoided)**: `led_stop`, `port_pwm`, `reset: cold`, `mode_color`, `led_mode` - these can corrupt MCU state, kill LED output, or break I2C communication.
- **Auto-deploy**: On startup, the coordinator checks each switch for the daemon binary. If missing, it deploys via SCP automatically.
- **Persistence**: The switch daemon survives reboots via cron. The coordinator runs as a Docker container with `restart: unless-stopped`.
- **Firmware updates**: The daemon in `/etc/persistent/` survives firmware updates. The binary may need to be rebuilt if Ubiquiti changes the libubus ABI.

## Research

See [RESEARCH.md](RESEARCH.md) for detailed reverse-engineering findings of the Etherlighting system, I2C bus details, and the full ubus API documentation.

## See Also

If you're looking for a UniFi security and performance optimization project, check out [Network Optimizer](https://github.com/Ozark-Connect/NetworkOptimizer) - a full network auditing and optimization tool for UniFi environments.

## License

MIT

---

<sub>UniFi Lightshow is an independent project by Ozark Connect and is not affiliated with, endorsed by, or sponsored by Ubiquiti, Inc. Ubiquiti, UniFi, Etherlighting, and USW are trademarks or registered trademarks of Ubiquiti, Inc. All other trademarks are the property of their respective owners.</sub>
