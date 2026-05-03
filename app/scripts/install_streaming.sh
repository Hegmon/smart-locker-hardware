#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"

echo "=== Smart Locker Streaming Agent Installer ==="

# Check dependencies
echo "🔍 Checking dependencies..."

if ! command -v ffmpeg &>/dev/null; then
    echo "❌ ffmpeg not found. Install it first:"
    echo "   sudo apt-get install ffmpeg"
    exit 1
fi

if ! command -v ffprobe &>/dev/null; then
    echo "❌ ffprobe not found (part of ffmpeg). Install ffmpeg."
    exit 1
fi

echo "✅ Dependencies OK (ffmpeg, ffprobe)"

# Ensure virtualenv exists and has required deps
if [[ ! -d "$VENV_DIR" ]]; then
    echo "❌ Virtualenv not found at $VENV_DIR. Run main install.sh first."
    exit 1
fi

echo "🐍 Installing streaming deps into virtualenv..."
source "$VENV_DIR/bin/activate"
pip install --upgrade pip || true
# All needed packages already in requirements.txt (paho-mqtt, requests)
pip install --no-cache-dir -r "$PROJECT_DIR/requirements.txt" || true

# Validate /etc/qbox-device.conf exists
if [[ ! -f /etc/qbox-device.conf ]]; then
    echo "⚠️  /etc/qbox-device.conf not found!"
    echo "   Create it with your device_id, e.g.:"
    echo "   echo 'DEVICE_ID=QBOX-001' | sudo tee /etc/qbox-device.conf"
    echo ""
    echo "   You can also use JSON format:"
    echo "   echo '{\"device_id\":\"QBOX-001\"}' | sudo tee /etc/qbox-device.conf"
    echo ""
    read -p "Continue anyway? (y/N): " -n 1 -r
    echo
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        exit 1
    fi
fi

# Install systemd service
echo "⚙️ Installing systemd service..."
STREAMING_SERVICE_SRC="$PROJECT_DIR/app/systemmd/qbox-streams.service"

if [[ ! -f "$STREAMING_SERVICE_SRC" ]]; then
    echo "❌ Service file not found: $STREAMING_SERVICE_SRC"
    exit 1
fi

# Replace __PROJECT_DIR__ placeholder
STREAMING_UNIT_TMP=$(mktemp)
sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$STREAMING_SERVICE_SRC" > "$STREAMING_UNIT_TMP"

# Read optional MQTT env overrides
MQTT_HOST="${MQTT_HOST:-}"
MQTT_PORT="${MQTT_PORT:-1883}"

if [[ -n "$MQTT_HOST" ]]; then
    if grep -q "Environment=MQTT_HOST" "$STREAMING_UNIT_TMP"; then
        sed -i "s|Environment=MQTT_HOST=.*|Environment=MQTT_HOST=$MQTT_HOST|" "$STREAMING_UNIT_TMP" || true
    else
        sed -i "/Environment=PYTHONUNBUFFERED=1/a Environment=MQTT_HOST=$MQTT_HOST" "$STREAMING_UNIT_TMP" || true
    fi
fi

if [[ -n "$MQTT_PORT" ]]; then
    if grep -q "Environment=MQTT_PORT" "$STREAMING_UNIT_TMP"; then
        sed -i "s|Environment=MQTT_PORT=.*|Environment=MQTT_PORT=$MQTT_PORT|" "$STREAMING_UNIT_TMP" || true
    else
        sed -i "/Environment=MQTT_HOST=$MQTT_HOST/a Environment=MQTT_PORT=$MQTT_PORT" "$STREAMING_UNIT_TMP" || true
    fi
fi

sudo cp "$STREAMING_UNIT_TMP" /etc/systemd/system/qbox-streams.service
rm "$STREAMING_UNIT_TMP"

sudo systemctl daemon-reload
sudo systemctl enable qbox-streams.service

echo "✅ Streaming agent service installed as qbox-streams.service"

echo ""
echo "=== Installation Complete ==="
echo ""
echo "Services to start:"
echo "  1. Ensure mediamtx.service is running: sudo systemctl start mediamtx"
echo "  2. Start streaming:    sudo systemctl start qbox-streams.service"
echo ""
echo "Check status:"
echo "  sudo systemctl status qbox-streams.service"
echo "  sudo journalctl -u qbox-streams.service -f"
echo ""
