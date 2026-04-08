# UniFi Etherlighting Internals - USW-Pro-XG-8-PoE

## Hardware
- **Board**: USW-Pro-XG-8-PoE (sysid 0xed76, shortname USPXG8P)
- **OS**: Linux 4.4.153 (MIPS, musl libc)
- **MCU board_id**: 13
- **Addressable LED ports**: 10 (ports 1-10: 8x PoE + 2x SFP+)
- **LED type**: RGBW (4 channels per port + brightness level)
- **LED driver**: Dedicated MCU communicated via I2C (`i2c_dev_init`, `etherlightd_i2c_xfer`)
- **Kernel module**: `ubnt-etherlight.ko` (loaded via `/usr/etc/modules.d/60-ubnt-etherlight`)
- **MCU firmware**: `/usr/share/firmware/port_led_fw.bin` (127KB, ARM Thumb)

## Control Architecture

```
UniFi Controller (cloud/local)
  --> system.cfg (pushed via adopt/inform)
    --> etherlightd (userspace daemon, PID ~874)
      --> ubus IPC (etherlight.mcu service)
        --> I2C bus --> LED MCU --> RGBW LEDs per port
```

## ubus API (`etherlight.mcu`)

### Key SET commands

| Command | Format | Example |
|---------|--------|---------|
| `port_pwm` | `"port R G B W level"` | `"1 255 0 0 0 100"` (port 1, red, full brightness) |
| `brightness` | integer 0-100 | `100` |
| `behavior` | `"steady"` or see below | `"steady"` for solid color |
| `led_mode` | string | `"speed"`, `"network"`, `"poe"`, `"device_type"`, `"port_locate"`, `"port_locate_unset"` |
| `led_stop` | `"stop"` / `"start"` | `"stop"` freezes etherlightd updates |
| `mode_color` | varies by mode | `"speed 10 0 ff0000"`, `"network ff0000 00ff00 0000ff"` |
| `info_mode` | boolean | Toggles info display mode |
| `info_brightness` | integer | Info panel brightness |
| `calibration` | boolean | Toggle calibration |
| `reset` | string | MCU reset |

### Behavior values
- `steady` - solid/static color
- `breath_1s` / `breath_2s` / `breath_3s` - pulsing at different speeds
- `gradient_1s` / `gradient_2s` / `gradient_3s` - gradient transitions

### port_pwm format
```
"<port> <red> <green> <blue> <white> <level>"
```
- port: 1-10
- R/G/B/W: 0-255
- level: 0-100 (brightness percentage)

### LED mode values
- `speed` - color by link speed (default)
- `network` - color by network/VLAN
- `poe` - color by PoE status
- `device_type` - color by connected device type
- `port_locate` / `port_locate_unset` - locate blinking

### CRITICAL: Use `port_rgb`, NOT `port_pwm`

`port_pwm` only controls the SFP ports. The RJ45 ports are driven by a separate
MCU code path that `port_pwm` does not reach.

**`port_rgb`** is the correct command for all ports (RJ45 + SFP):
```
"port hexcolor level"   e.g.  "1 ff0000 100"
```
- port: 1-10 (1-based)
- hexcolor: 6-char hex RGB
- level: 0-100 brightness

### Do NOT use `led_stop`

`led_stop: "stop"` kills the LED display output entirely — all ports go dark.
Neither `port_pwm` nor `port_rgb` can turn them back on while stopped.
`led_stop: "start"` re-enables the display.

Instead, just write `port_rgb` commands while etherlightd is running. They
override the current colors without stopping the display.

### Set `behavior` to `steady`

The default `breath` animation fights with custom color updates. Set
`behavior: "steady"` on startup to disable the pulsing.

### Do NOT use `reset: "cold"`

Cold resetting the MCU breaks I2C communication. The MCU enters its boot
rainbow animation and etherlightd loses contact. Recovery requires killing
and restarting etherlightd. Avoid at all costs.

### Do NOT touch `mode_color` or `led_mode`

These corrupt the MCU's internal color lookup tables. Recovery requires
manually re-setting each color through the UniFi console or a reprovision.

### `behavior: steady` must be re-asserted

etherlightd periodically re-applies its animation config. Setting `behavior: steady`
once is not enough — it must be re-sent every ~2 seconds or the breath animation
returns and fights with custom colors (causes flickering).

## On-Switch UDP Daemon (`etherlightd_udp`)

The most efficient approach for custom LED control. A tiny C binary (148KB) that
runs on the switch, receives UDP color frames, and calls the ubus C API directly.

### Architecture
```
Coordinator (NAS)  --UDP:9200-->  etherlightd_udp (switch)  --ubus IPC-->  etherlightd  --I2C-->  MCU
```

### Performance (8fps, 10 ports)
| Approach | CPU Idle | Load Avg |
|----------|----------|----------|
| SSH + system() @ 3fps | 36% | 1.6 |
| libubus C daemon @ 8fps | 90% | 0.86 |

### Build
```bash
# On NAS (musl cross-compiler from https://musl.cc/mips-linux-muslsf-cross.tgz)
/tmp/mips-linux-muslsf-cross/bin/mips-linux-muslsf-gcc \
    -O2 -Wl,--dynamic-linker=/lib/ld-musl-mips-sf.so.1 \
    -Wl,--unresolved-symbols=ignore-all \
    -o etherlightd_udp etherlightd_udp.c -Wl,-rpath,/lib
```

### Run
```bash
LD_PRELOAD="/lib/libubus.so.20231128 /lib/libubox.so.20240329 \
  /lib/libblobmsg_jansson.so.20240329 /lib/libjansson.so.4 \
  /lib/libz.so.1" ./etherlightd_udp 9200
```

### Key details
- I2C bus: `/dev/i2c-104`, MCU address `0x66`
- I2C bus 201 also open (SFP-related)
- `ubnt_etherlight_all_color_set(r, g, b, brightness, 0, 0)` in libubnt.so works for solid all-ports color
- Per-port control requires `port_rgb` via ubus (etherlightd does the I2C internally)
- Delta updates: only changed ports are sent each frame
- Heartbeat: `behavior: steady` re-sent every ~2s to suppress etherlightd animations

## Baseline Speed-Mode Colors (from UniFi Console)

| Speed   | Hex       | RGB             |
|---------|-----------|-----------------|
| FE      | `#05FF7F` | (5, 255, 127)   |
| GbE     | `#055AFF` | (5, 90, 255)    |
| 2.5 GbE | `#05DAFF` | (5, 218, 255)   |
| 5 GbE   | `#7905FF` | (121, 5, 255)   |
| 10 GbE  | `#E705FF` | (231, 5, 255)   |

## system.cfg Etherlight Config
```ini
switch.etherlight.behavior=breath
switch.etherlight.brightness=100
switch.etherlight.mode=speed
switch.etherlight.mode.1.name=speed
switch.etherlight.mode.1.color.1.type=100GbE
switch.etherlight.mode.1.color.1.code=32ffd9
# ... etc
switch.etherlight.mode.2.name=network
switch.etherlight.mode.2.color.1.id=64
switch.etherlight.mode.2.color.1.code=5eff14
# ... etc
```

## Integration Strategy

### For Home Assistant
1. SSH command execution via `shell_command` or custom component
2. Call `ubus call etherlight.mcu set '{"led_stop":"stop"}'` to take control
3. Set per-port colors with `port_pwm` commands
4. Call `led_stop: start` to return control to etherlightd

### For SignalRGB
1. Create a SignalRGB plugin that sends SSH commands
2. Map the 10 ports as a 10-pixel LED strip
3. Each pixel = RGBW + brightness

### Key Considerations
- Colors are **not persistent** across reboots (etherlightd reinitializes from system.cfg)
- `led_stop: stop` must be called first or etherlightd will overwrite custom colors
- The MCU communicates via I2C, so there's inherent latency (~5-20ms per command)
- Rapid-fire ubus calls work but each is a separate I2C transaction
- For smooth animations, batch all 10 port updates quickly then sleep
