# UniFi Lightshow

Custom RGB light show controller for UniFi Etherlighting-enabled switches (USW-Pro series). Overrides the built-in Etherlighting LEDs with custom effects, SignalRGB integration, and optional OpenRGB support for ARGB devices.

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
      "host": "192.168.1.10",
      "user": "your-ssh-username",
      "num_ports": 10
    }
  ],
  "wled_bind_ip": "0.0.0.0",
  "wled_bind_port": 80,
  "max_fps": 8,
  "brightness": 100,
  "udp_timeout": 5.0,
  "default_effect": "plasma"
}
```

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
3. Use the SSH username you configured in UniFi Network as the `user` field in `switches.json`

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
```

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
