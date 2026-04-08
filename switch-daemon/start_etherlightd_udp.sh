#!/bin/sh
# Start etherlightd_udp if not already running, and ensure cron is set up.
# Deploy to /etc/persistent/ on the switch.
# Bootstrap: ssh switch "nohup /etc/persistent/start_etherlightd_udp.sh &"
# After first run, cron keeps it alive every minute.

DAEMON=/etc/persistent/etherlightd_udp
PORT=9200
PRELOAD="/lib/libubus.so.20231128 /lib/libubox.so.20240329 /lib/libblobmsg_jansson.so.20240329 /lib/libjansson.so.4 /lib/libz.so.1"
CRON_DIR=/etc/crontabs
CRON_FILE=$CRON_DIR/root
SELF=/etc/persistent/start_etherlightd_udp.sh

# Ensure cron is set up
if [ ! -f "$CRON_FILE" ] || ! grep -q etherlightd_udp "$CRON_FILE" 2>/dev/null; then
    mkdir -p "$CRON_DIR"
    echo "* * * * * $SELF" > "$CRON_FILE"
    pidof crond > /dev/null 2>&1 || crond -c "$CRON_DIR"
fi

# Start daemon if not running
if ! pidof etherlightd_udp > /dev/null 2>&1; then
    LD_PRELOAD="$PRELOAD" $DAEMON $PORT > /dev/null 2>&1 &
fi
