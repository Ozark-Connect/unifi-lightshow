/*
 * etherlightd_udp — Lightweight UDP-driven LED controller for UniFi switches.
 *
 * Uses libubus C API directly — no shell/fork overhead.
 * Receives JSON color frames via UDP, calls ubus_invoke for each changed port.
 *
 * Port stats: every STATS_INTERVAL_MS it reads /proc/port/<N> and sends one
 * line back to the address and port the last frame came from:
 *   S1 <seq> <ms> <link>:<speed>:<rx_byte>:<tx_byte> ... (one field per port)
 * ms is CLOCK_MONOTONIC in milliseconds; speed is in Mbps; counters are
 * raw 64-bit byte counts. Replying to the frame's own source keeps the flow
 * inside the conntrack entry the frames created, so no firewall rule is needed.
 * Stats stop when no frame has arrived for STATS_IDLE_MS.
 *
 * Build:
 *   /tmp/mips-linux-muslsf-cross/bin/mips-linux-muslsf-gcc \
 *       -O2 -Wl,--dynamic-linker=/lib/ld-musl-mips-sf.so.1 \
 *       -Wl,--unresolved-symbols=ignore-all \
 *       -o etherlightd_udp etherlightd_udp.c -Wl,-rpath,/lib
 *
 * Run:
 *   LD_PRELOAD="/lib/libubus.so.20231128 /lib/libubox.so.20240329 \
 *     /lib/libblobmsg_jansson.so.20240329 /lib/libjansson.so.4 \
 *     /lib/libz.so.1" ./etherlightd_udp [port] [stats]
 *
 * stats: 1 (default) sends port stats, 0 disables them.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <time.h>
#include <sys/syscall.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <sys/time.h>
#include <stdint.h>

#define MAX_PORTS 10
#define BUF_SIZE 2048
#define DEFAULT_PORT 9200
#define COLOR_THRESHOLD 4  /* skip port update if R/G/B each changed by less than this */
#define STATS_INTERVAL_MS 250
#define STATS_IDLE_MS 10000

/* ── Minimal libubox/libubus ABI declarations ──────────────────────────
 *
 * blob_buf is opaque to us. We treat it as a 256-byte buffer.
 * The actual struct is ~32 bytes but we give extra room.
 * All we need: blob_buf_init, blobmsg_add_field, and the head pointer.
 *
 * blobmsg field types (from blobmsg.h):
 *   BLOBMSG_TYPE_STRING = 3
 */

#define BLOBMSG_TYPE_STRING 3

struct blob_buf {
    unsigned char opaque[256];
};

struct blob_attr;
struct ubus_context;

/* libubox */
extern int blob_buf_init(struct blob_buf *buf, int id);
extern int blobmsg_add_field(struct blob_buf *buf, int type,
                             const char *name, const void *data, unsigned int len);

/* libubus */
extern struct ubus_context *ubus_connect(const char *path);
extern void ubus_free(struct ubus_context *ctx);
extern int ubus_lookup_id(struct ubus_context *ctx, const char *path, unsigned int *id);
/* ubus_invoke_fd — the actual exported symbol (ubus_invoke is an inline wrapper) */
extern int ubus_invoke_fd(struct ubus_context *ctx, unsigned int obj,
                          const char *method, struct blob_attr *msg,
                          void *cb, void *priv, int timeout, int fd);

/*
 * blob_buf_init returns 0 on success. After adding fields, the message
 * payload (blob_attr *) is at buf->head. In libubox's blob_buf struct,
 * the 'head' pointer is the first member.
 */
static inline struct blob_attr *bbuf_head(struct blob_buf *buf) {
    struct blob_attr **head_ptr = (struct blob_attr **)buf->opaque;
    return *head_ptr;
}

/* ── Color state ─────────────────────────────────────────────────────── */

static unsigned char cur_r[MAX_PORTS], cur_g[MAX_PORTS], cur_b[MAX_PORTS];
static unsigned char prev_r[MAX_PORTS], prev_g[MAX_PORTS], prev_b[MAX_PORTS];
static int prev_brightness = -1;

static struct ubus_context *ctx = NULL;
static unsigned int mcu_id = 0;
static struct blob_buf bbuf;

static void ubus_set(const char *key, const char *val) {
    blob_buf_init(&bbuf, 0);
    blobmsg_add_field(&bbuf, BLOBMSG_TYPE_STRING, key, val, strlen(val) + 1);
    ubus_invoke_fd(ctx, mcu_id, "set", bbuf_head(&bbuf), NULL, NULL, 500, -1);
}

static void set_port_rgb(int port, unsigned char r, unsigned char g, unsigned char b, int brightness) {
    char val[32];
    snprintf(val, sizeof(val), "%d %02x%02x%02x %d", port, r, g, b, brightness);
    ubus_set("port_rgb", val);
}

/* ── JSON parser ─────────────────────────────────────────────────────── */

static int parse_frame(const char *buf, int *brightness) {
    const char *p;
    int port = 0;

    *brightness = 100;
    p = strstr(buf, "\"brightness\"");
    if (p) {
        p = strchr(p, ':');
        if (p) *brightness = atoi(p + 1);
    }

    p = strstr(buf, "\"ports\"");
    if (!p) return 0;
    p = strchr(p, '[');
    if (!p) return 0;
    p++;

    while (port < MAX_PORTS && *p) {
        while (*p && *p != '[') {
            if (*p == ']') goto done;
            p++;
        }
        if (!*p) break;
        p++;

        int vals[4] = {0, 0, 0, 0};
        int vi = 0;
        while (*p && *p != ']' && vi < 4) {
            while (*p == ' ' || *p == ',') p++;
            if (*p >= '0' && *p <= '9') {
                vals[vi++] = atoi(p);
                while (*p >= '0' && *p <= '9') p++;
            } else {
                p++;
            }
        }
        if (*p == ']') p++;

        cur_r[port] = (unsigned char)vals[0];
        cur_g[port] = (unsigned char)vals[1];
        cur_b[port] = (unsigned char)vals[2];
        port++;
    }
done:
    return port;
}

/* ── Port stats ──────────────────────────────────────────────────────── */

/* ── 32-bit time ABI ─────────────────────────────────────────────────────
 *
 * The switch's libc is musl older than 1.2, with 32-bit time_t. A current
 * musl toolchain builds against 64-bit time_t: clock_gettime links to
 * __clock_gettime64, which the switch's libc lacks, and SO_RCVTIMEO becomes
 * the 64-bit variant, which the switch's kernel (4.4) rejects. So time
 * structs here are declared with 32-bit fields and passed straight to the
 * kernel (raw syscall, and a setsockopt that the old libc passes through).
 */

struct timespec32 { long tv_sec; long tv_nsec; };
struct timeval32 { long tv_sec; long tv_usec; };

/* musl 1.2 renames the 32-bit syscall; older headers use the plain name */
#ifdef SYS_clock_gettime32
#define CLOCK_GETTIME32 SYS_clock_gettime32
#else
#define CLOCK_GETTIME32 SYS_clock_gettime
#endif

/* musl 1.2 defines SO_RCVTIMEO as 66 (SO_RCVTIMEO_NEW, 64-bit timeval),
 * which kernel 4.4 rejects. Use the kernel's original option number:
 * 0x1006 on MIPS, 20 elsewhere. */
#if defined(__mips__)
#define RCVTIMEO_OPT 0x1006
#else
#define RCVTIMEO_OPT 20
#endif

static long long now_ms(void) {
    struct timespec32 ts = {0, 0};
    syscall(CLOCK_GETTIME32, CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

/* Value of "key=" in a /proc/port line, or NULL. Keys are matched at the
 * start of the line or after a comma, so "rx_byte" never matches "trx_byte". */
static const char *field(const char *line, const char *key) {
    size_t klen = strlen(key);
    const char *p = line;
    while (p && *p) {
        if (strncmp(p, key, klen) == 0 && p[klen] == '=')
            return p + klen + 1;
        p = strchr(p, ',');
        if (p) p++;
    }
    return NULL;
}

/* Append "<link>:<speed>:<rx>:<tx>" for one port to out. Returns bytes written. */
static int read_port(int n, char *out, size_t cap) {
    char path[32], line[1024];
    snprintf(path, sizeof(path), "/proc/port/%d", n);
    int fd = open(path, O_RDONLY);
    if (fd < 0) return snprintf(out, cap, " 0:0:0:0");
    ssize_t len = read(fd, line, sizeof(line) - 1);
    close(fd);
    if (len <= 0) return snprintf(out, cap, " 0:0:0:0");
    line[len] = '\0';

    const char *v;
    int link = (v = field(line, "link")) && strncmp(v, "up", 2) == 0;
    int speed = (v = field(line, "speed")) ? atoi(v) : 0;
    unsigned long long rx = (v = field(line, "rx_byte")) ? strtoull(v, NULL, 10) : 0;
    unsigned long long tx = (v = field(line, "tx_byte")) ? strtoull(v, NULL, 10) : 0;
    return snprintf(out, cap, " %d:%d:%llu:%llu", link, speed, rx, tx);
}

static void send_stats(int sock, const struct sockaddr_in *to, unsigned int seq, int num_ports) {
    char pkt[BUF_SIZE];
    int len = snprintf(pkt, sizeof(pkt), "S1 %u %lld", seq, now_ms());
    for (int i = 1; i <= num_ports && len < (int)sizeof(pkt); i++)
        len += read_port(i, pkt + len, sizeof(pkt) - len);
    if (len >= (int)sizeof(pkt)) return;
    sendto(sock, pkt, len, 0, (const struct sockaddr *)to, sizeof(*to));
}

/* ── Main ────────────────────────────────────────────────────────────── */

int main(int argc, char *argv[]) {
    int port = DEFAULT_PORT;
    if (argc > 1) port = atoi(argv[1]);
    int stats_enabled = argc > 2 ? atoi(argv[2]) != 0 : 1;

    /* Connect to ubus */
    ctx = ubus_connect(NULL);
    if (!ctx) {
        fprintf(stderr, "ubus_connect failed\n");
        return 1;
    }
    printf("ubus connected\n");

    /* Look up etherlight.mcu */
    if (ubus_lookup_id(ctx, "etherlight.mcu", &mcu_id) != 0) {
        fprintf(stderr, "etherlight.mcu not found\n");
        ubus_free(ctx);
        return 1;
    }
    printf("etherlight.mcu id: %u\n", mcu_id);

    /* UDP socket */
    int sock = socket(AF_INET, SOCK_DGRAM, 0);
    if (sock < 0) { perror("socket"); return 1; }

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    addr.sin_addr.s_addr = INADDR_ANY;

    if (bind(sock, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        perror("bind"); return 1;
    }

    printf("etherlightd_udp listening on :%d\n", port);
    fflush(stdout);

    /* Set steady behavior */
    ubus_set("behavior", "steady");

    /* Force first frame to send all */
    memset(prev_r, 0xFF, sizeof(prev_r));
    memset(prev_g, 0xFF, sizeof(prev_g));
    memset(prev_b, 0xFF, sizeof(prev_b));

    char buf[BUF_SIZE];

    /* Use non-blocking recvfrom with a timeout so we can periodically
     * re-assert steady behavior to prevent etherlightd from taking over */
    struct timeval32 tv = {0, 100000}; /* 100ms timeout */
    if (setsockopt(sock, SOL_SOCKET, RCVTIMEO_OPT, &tv, sizeof(tv)) != 0)
        perror("setsockopt SO_RCVTIMEO");

    int steady_counter = 0;
    #define STEADY_INTERVAL 20 /* re-assert every ~2 seconds (20 * 100ms) */

    struct sockaddr_in peer;
    int have_peer = 0;
    long long last_frame_ms = 0, last_stats_ms = 0;
    unsigned int stats_seq = 0;

    while (1) {
        struct sockaddr_in from;
        socklen_t fromlen = sizeof(from);
        ssize_t n = recvfrom(sock, buf, BUF_SIZE - 1, 0, (struct sockaddr *)&from, &fromlen);
        long long now = now_ms();

        /* Periodically re-assert steady to suppress etherlightd animations */
        steady_counter++;
        if (steady_counter >= STEADY_INTERVAL) {
            ubus_set("behavior", "steady");
            steady_counter = 0;
        }

        /* Stats go to whoever sent the last frame, while frames keep coming */
        if (stats_enabled && have_peer && now - last_frame_ms < STATS_IDLE_MS &&
            now - last_stats_ms >= STATS_INTERVAL_MS) {
            send_stats(sock, &peer, stats_seq++, MAX_PORTS);
            last_stats_ms = now;
        }

        if (n <= 0) continue;
        buf[n] = '\0';

        int brightness = 100;
        const char *bp = strstr(buf, "\"brightness\"");
        if (bp) { bp = strchr(bp, ':'); if (bp) brightness = atoi(bp + 1); }

        int num_ports = parse_frame(buf, &brightness);
        if (num_ports == 0) continue;

        peer = from;
        have_peer = 1;
        last_frame_ms = now;

        /* "force": true writes every port, so the coordinator's periodic
         * refresh can repaint ports that etherlightd changed behind our back */
        int force = 0;
        const char *fp = strstr(buf, "\"force\"");
        if (fp) {
            fp = strchr(fp, ':');
            if (fp) {
                fp++;
                while (*fp == ' ') fp++;
                force = (*fp == 't');
            }
        }

        for (int i = 0; i < num_ports && i < MAX_PORTS; i++) {
            int dr = cur_r[i] > prev_r[i] ? cur_r[i] - prev_r[i] : prev_r[i] - cur_r[i];
            int dg = cur_g[i] > prev_g[i] ? cur_g[i] - prev_g[i] : prev_g[i] - cur_g[i];
            int db = cur_b[i] > prev_b[i] ? cur_b[i] - prev_b[i] : prev_b[i] - cur_b[i];

            if (force || dr >= COLOR_THRESHOLD || dg >= COLOR_THRESHOLD || db >= COLOR_THRESHOLD ||
                brightness != prev_brightness) {
                set_port_rgb(i + 1, cur_r[i], cur_g[i], cur_b[i], brightness);
                prev_r[i] = cur_r[i];
                prev_g[i] = cur_g[i];
                prev_b[i] = cur_b[i];
            }
        }
        prev_brightness = brightness;
    }

    ubus_free(ctx);
    close(sock);
    return 0;
}
