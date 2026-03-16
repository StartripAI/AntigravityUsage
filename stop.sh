#!/bin/bash
# Antigravity Token Tracker — Stop Script (v2)
# Stops mitmdump and restores system proxy settings
set -e

LOGDIR="$HOME/.config/anti-tracker"
PIDFILE="$LOGDIR/mitmdump.pid"

echo "🛑 Stopping Antigravity Token Tracker..."

# Stop mitmdump
if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    if kill -0 "$PID" 2>/dev/null; then
        kill "$PID"
        echo "✅ Stopped mitmdump (PID $PID)"
    else
        echo "ℹ️  mitmdump was not running"
    fi
    rm -f "$PIDFILE"
else
    echo "ℹ️  No PID file found"
    pkill -f "mitmdump.*tracker_addon" 2>/dev/null && echo "✅ Killed lingering mitmdump" || true
fi

# Restore system proxy settings
echo "🌐 Restoring system proxy settings..."
if [ -f "$LOGDIR/network_interface.txt" ]; then
    while IFS= read -r iface; do
        if [ -n "$iface" ]; then
            networksetup -setsecurewebproxystate "$iface" off 2>/dev/null && \
                echo "   ✅ Disabled HTTPS proxy for: $iface" || true
            networksetup -setwebproxystate "$iface" off 2>/dev/null && \
                echo "   ✅ Disabled HTTP proxy for: $iface" || true
        fi
    done < "$LOGDIR/network_interface.txt"
else
    # Fallback: try common interfaces
    for iface in "Wi-Fi" "Ethernet"; do
        networksetup -setsecurewebproxystate "$iface" off 2>/dev/null || true
        networksetup -setwebproxystate "$iface" off 2>/dev/null || true
    done
    echo "   ✅ Disabled proxies for common interfaces"
fi

# Log stats
if [ -f "$LOGDIR/usage.jsonl" ]; then
    LINES=$(wc -l < "$LOGDIR/usage.jsonl" | tr -d ' ')
    echo ""
    echo "📊 Captured $LINES requests"
    echo "   Log: $LOGDIR/usage.jsonl"
fi

echo ""
echo "✅ Tracker stopped. Network is back to normal."
