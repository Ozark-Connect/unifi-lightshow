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

## Switch CPU Path Limit

Traffic addressed to the switch itself (SSH, UDP frames, SNMP, iperf3) reaches the CPU through
an internal port with a hardware rate limit of roughly **100 packets/s inbound**. Routed and
switched traffic never touches it; the switch chip forwards that at line rate.

Measured 2026-10-20 on a USW-Pro-XG-8-PoE (`US3.rtl93xx_7.5.15`), with a static iperf3 3.18 built
for big-endian MIPS32r2 soft-float musl, client on a host one hop away:

| Test | Result |
|---|---|
| TCP, host to switch | 1.04 Mbit/s, 79 retransmits |
| TCP, switch to host (`-R`) | 2.73 Mbit/s |
| UDP offered at 20 Mbit/s | 1.13 Mbit/s received, 94% loss, ~100 packets/s |
| Switch CPU during the UDP test | 81% idle |

The CPU was mostly idle, so the limit is a policer, not CPU exhaustion. The switch `cli` has
`show cpu input rate` (7 pps at idle) but no command to change the limit; it lives in the Realtek
SDK driver (`rtk.ko`). Inform, STP, LLDP, and SSH share this budget with anything a light show sends.

## Transport Approaches

| Approach | Result |
|---|---|
| SSH + shell `ubus call` CLI per port (`system()`) | 36% CPU idle, load 1.6, at only 3fps |
| Persistent SSH session, `ubus call` per port | Not measured. Removes connect cost, but every `ubus call` still forks the CLI and reconnects to ubusd. Over TCP, a policer drop stalls every queued command behind a retransmit (≥200 ms), and the sender has no signal that the switch is behind |
| `etherlightd_udp`: resident C process, `libubus` in-process, UDP frames | 90% CPU idle, load 0.86, at 8fps; one inbound packet per frame per switch |

UDP fits a light stream: the newest frame wins, and a lost frame is replaced by the next one (plus
the coordinator's `switch_refresh` resend for static frames). SSH is used only to bootstrap the daemon.

`port_pwm` (SFP+ only) and `led_stop` (blacks out the display) were tried and rejected; see above.

## Port Counters (`/proc/port/<N>`)

One line of comma-separated `key=value` per port, ~590 bytes. Fields used for traffic reactivity:
`link` (`up`/`down`), `speed` (Mbps; a down port reports `10`), `rx_byte`, `tx_byte`. Byte
counters are 64-bit (a 10G port read `rx_byte=4035765958502`). `/proc/port/1..10` matches LED
ports 1-10; 9 and 10 are the SFP+ ports. `/proc/port/all` and `/proc/port/cpu` also exist.
`swctrl port show counters` can read all zeros; `/proc/port` is the reliable source.

## Down Ports

The MCU blanks a port without link, RJ45 and SFP+ alike, whatever `port_rgb` writes.

Unverified, reported elsewhere: `mcu_mode: "soc_port_map steady 0 100"` (port 9) and
`"soc_port_map steady 0 200"` (port 10) put the SFP+ LEDs in a direct-output mode that lights them
without link. Not used here. `mcu_mode` is untested, and `led_mode` / `mode_color` in the same
API corrupt MCU state (see above).

## Notes

- Colors are **not persistent** across reboots (etherlightd reinitializes from system.cfg)
- The MCU communicates via I2C, so each `port_rgb` has latency (~5-20 ms per command)
- Each ubus call is a separate I2C transaction; the daemon sends only ports whose color changed
