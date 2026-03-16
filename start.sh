#!/bin/bash
# Antigravity Token Tracker — Start Script (v3)
# Launches mitmproxy + restarts Antigravity with HTTPS_PROXY env var
# Go binaries respect HTTPS_PROXY, unlike macOS system proxy
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ADDON="$SCRIPT_DIR/tracker_addon.py"
LOGDIR="$HOME/.config/anti-tracker"
PIDFILE="$LOGDIR/mitmdump.pid"
PROXY_PORT=8899
CLASH_PORT=7890

mkdir -p "$LOGDIR"

# Check if already running
if [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
    echo "⚠️  Tracker already running (PID $(cat "$PIDFILE")). Use stop.sh first."
    exit 1
fi

echo "🔍 Antigravity Token Tracker v3"
echo "================================"
echo "⚠️  This will restart Antigravity to inject HTTPS_PROXY."
echo "   Your open windows/tabs will be restored automatically."
echo ""

# Step 1: Generate + trust mitmproxy CA cert
CADIR="$HOME/.mitmproxy"
if [ ! -f "$CADIR/mitmproxy-ca-cert.pem" ]; then
    echo "📝 Generating mitmproxy CA certificate..."
    mitmdump --listen-port "$PROXY_PORT" -q &
    TMPPID=$!
    sleep 2
    kill $TMPPID 2>/dev/null || true
    wait $TMPPID 2>/dev/null || true
fi

if ! security find-certificate -c "mitmproxy" /Library/Keychains/System.keychain >/dev/null 2>&1; then
    echo "🔐 Adding mitmproxy CA to system keychain (requires sudo)..."
    sudo security add-trusted-cert -d -r trustRoot \
        -k /Library/Keychains/System.keychain \
        "$CADIR/mitmproxy-ca-cert.pem"
    echo "✅ CA certificate trusted"
else
    echo "✅ CA certificate already trusted"
fi

# Step 2: Detect ClashX and configure upstream
UPSTREAM=""
if lsof -nP -iTCP:$CLASH_PORT -sTCP:LISTEN >/dev/null 2>&1; then
    echo "🔗 ClashX detected on port $CLASH_PORT, chaining through it"
    UPSTREAM="--mode upstream:http://127.0.0.1:$CLASH_PORT/"
else
    echo "ℹ️  No ClashX detected, using direct connection"
    UPSTREAM="--mode regular"
fi

# Step 3: Start mitmdump
echo "🚀 Starting mitmdump on port $PROXY_PORT..."
mitmdump \
    $UPSTREAM \
    --listen-port "$PROXY_PORT" \
    --ssl-insecure \
    -s "$ADDON" \
    --set console_eventlog_verbosity=info \
    > "$LOGDIR/mitmdump.log" 2>&1 &

MITMPID=$!
echo "$MITMPID" > "$PIDFILE"
sleep 2

if ! kill -0 "$MITMPID" 2>/dev/null; then
    echo "❌ mitmdump failed to start."
    tail -10 "$LOGDIR/mitmdump.log"
    rm -f "$PIDFILE"
    exit 1
fi
echo "✅ mitmdump running (PID $MITMPID)"

# Step 4: Restart Antigravity with HTTPS_PROXY
echo "🔄 Restarting Antigravity with HTTPS_PROXY..."

# Gracefully close Antigravity
osascript -e 'quit app "Antigravity"' 2>/dev/null || true
sleep 3

# Kill any remaining processes
pkill -f "Antigravity.app" 2>/dev/null || true
sleep 2

# Relaunch with HTTPS_PROXY and HTTP_PROXY set
echo "🚀 Launching Antigravity with proxy..."
HTTPS_PROXY="http://127.0.0.1:$PROXY_PORT" \
HTTP_PROXY="http://127.0.0.1:$PROXY_PORT" \
open -a "Antigravity"

echo ""
echo "✅ Tracker is running!"
echo "   mitmdump PID: $MITMPID"
echo "   Proxy: 127.0.0.1:$PROXY_PORT"
echo "   Log: $LOGDIR/usage.jsonl"
echo ""
echo "   Wait ~10s for Antigravity to fully start, then use it normally."
echo "   Token usage will be logged to usage.jsonl."
echo "   Run ./stop.sh to stop tracking."
