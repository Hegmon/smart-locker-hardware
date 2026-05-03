#!/usr/bin/env bash
set -euo pipefail

# ============================================================
# MediaMTX (formerly rtsp-simple-server) installer for Raspberry Pi 4
# This script downloads and installs MediaMTX for ARM64 architecture.
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "=== MediaMTX Installer for Raspberry Pi 4 ==="

# Detect architecture
ARCH=$(uname -m)
if [[ "$ARCH" == "aarch64" ]]; then
    TARGET="linux_arm64v8"
elif [[ "$ARCH" == "armv7l" ]]; then
    TARGET="linux_armv7"
else
    echo "❌ Unsupported architecture: $ARCH (expected aarch64 or armv7l)"
    exit 1
fi

echo "📋 Detected architecture: $ARCH ($TARGET)"

# Get latest version (or pin to stable)
VERSION="${MEDIAMTX_VERSION:-v1.10.0}"
DOWNLOAD_URL="https://github.com/bluenviron/mediamtx/releases/download/${VERSION}/mediamtx_${VERSION#v}_${TARGET}.tar.gz"

echo "📦 Downloading MediaMTX ${VERSION}..."
curl -L -o /tmp/mediamtx.tar.gz "$DOWNLOAD_URL"

echo "📂 Extracting to /usr/local/bin/"
sudo tar -C /usr/local/bin -xzf /tmp/mediamtx.tar.gz
sudo chmod +x /usr/local/bin/mediamtx

rm /tmp/mediamtx.tar.gz

# Verify installation
if command -v mediamtx &>/dev/null; then
    echo "✅ MediaMTX installed: $(mediamtx -version 2>&1 || echo 'unknown version')"
else
    echo "❌ Installation failed: mediamtx not found in PATH"
    exit 1
fi

# Create config directory
sudo mkdir -p /etc/mediamtx
echo "📝 Creating config at /etc/mediamtx/mediamtx.yml"

# Generate config.yml
cat > /tmp/mediamtx.yml <<'EOF'
# MediaMTX Configuration for Smart Locker Raspberry Pi 4
# RTSP input on port 8554, HLS output on port 8888

# Logging
logLevel: info

# Metrics (optional, for monitoring)
metrics: no

# RTSP Server (input)
rtsp:
  # RTSP listen address (internal only, LAN access via IP)
  enabled: yes
  # 0.0.0.0:8554 = listen on all interfaces
  # Use 127.0.0.1:8554 for FFmpeg publishing only
  # We'll use localhost for RTSP input from FFmpeg on the same Pi
  address: 0.0.0.0:8554
  # Protocol: TCP only (more reliable)
  protocol: tcp
  # RTSP timeout
  timeout: 10s
  # Not requiring authentication for local network
  # (Consider adding auth in production if exposed)
  authMethod: none
  # Read timeout for RTSP clients
  readTimeout: 10s
  # Write timeout
  writeTimeout: 10s

# HLS Server (output for web/mobile playback)
hls:
  enabled: yes
  # 0.0.0.0:8888 = listen on all interfaces
  address: 0.0.0.0:8888
  # HLS variant: embed in HTML5-compatible way
  variant: lowlatency
  # Segment duration (in seconds)
  segmentCount: 3
  # Segment length (seconds)
  segmentDuration: 1s
  # Part duration (subsegment for low-latency)
  partDuration: 0.5s
  # Removes segments older than this
  segmentRetention: 5s
  # HLS playlist base path (relative to hls address)
  # Paths will be: /hls/{device_id}/{stream_type}/index.m3u8
  # MediaMTX automatically handles /hls/* paths
  # No extra configuration needed; the HLS endpoint automatically serves playlists

# CORS (for web apps)
cors:
  allowOrigin: "*"
  allowMethods: "GET, POST, OPTIONS"
  allowHeaders: "*"

# Run as the current user (will be overridden by systemd User=)
# Run as root is not recommended; but systemd User=root is used for simplicity
# In production create a dedicated mediamtx user.

# Additional settings
readTimeout: 10s
writeTimeout: 10s

# Paths
paths:
  # The path where HLS segments and playlists are stored (temporary)
  # This defaults to /tmp/mediamtx
  # We'll leave default.

# Run as root for simplicity in containerized Pi.
# Ensure no external ports are exposed except LAN.
EOF

sudo mv /tmp/mediamtx.yml /etc/mediamtx/mediamtx.yml
sudo chmod 644 /etc/mediamtx/mediamtx.yml

echo "✅ Config written to /etc/mediamtx/mediamtx.yml"

# Create systemd service
echo "⚙️ Installing systemd service..."
sudo tee /etc/systemd/system/mediamtx.service > /dev/null <<'EOT'
[Unit]
Description=MediaMTX RTSP/HLS Server
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=root
# MEDIAMTX_HOME sets config dir to /etc/mediamtx
Environment=MEDIAMTX_HOME=/etc/mediamtx
ExecStart=/usr/local/bin/mediamtx -config /etc/mediamtx/mediamtx.yml
Restart=always
RestartSec=5
# No special privileges needed for HLS/RTSP
# but allow binding to ports <1024 if needed (not needed as we use >1024)
# CapabilityBoundingSet=CAP_NET_BIND_SERVICE

# Output
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOT

sudo systemctl daemon-reload
sudo systemctl enable mediamtx.service
sudo systemctl start mediamtx.service

sleep 2
if systemctl is-active --quiet mediamtx.service; then
    echo "✅ mediamtx.service is running"
else
    echo "❌ mediamtx.service failed to start"
    sudo systemctl status mediamtx.service --no-pager || true
    exit 1
fi

echo ""
echo "=== MediaMTX installation complete ==="
echo ""
echo "Configuration:"
echo "  RTSP:  rtsp://<pi_ip>:8554/{device_id}/{stream_type}"
echo "  HLS:   http://<pi_ip>:8888/hls/{device_id}/{stream_type}/index.m3u8"
echo ""
echo "Next steps:"
echo "1. Ensure /etc/qbox-device.conf contains device_id=..."
echo "2. Install streaming agent: ./app/scripts/install_streaming.sh"
echo "3. Start service: sudo systemctl start qbox-streaming.service"
echo ""
