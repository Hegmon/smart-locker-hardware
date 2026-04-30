#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"

FASTAPI_UNIT_TMP="$(mktemp)"
WIFI_UNIT_TMP="$(mktemp)"
WIFI_UPLOAD_UNIT_TMP="$(mktemp)"

cleanup() {
  rm -f "$FASTAPI_UNIT_TMP" "$WIFI_UNIT_TMP" "$WIFI_UPLOAD_UNIT_TMP"
}
trap cleanup EXIT

echo "🔧 Updating system packages..."
sudo apt-get update

echo "📦 Installing core system dependencies for Pi4 WiFi + BLE agent..."

sudo apt-get install -y \
  python3 \
  python3-venv \
  python3-pip \
  build-essential \
  pkg-config \
  git \
  curl \
  network-manager \
  dbus \
  libdbus-1-dev \
  libdbus-glib-1-dev \
  bluez \
  bluetooth \
  rfkill \
  iw \
  wireless-tools \
  net-tools \
  pi-bluetooth \
  libglib2.0-dev \
  libgirepository1.0-dev \
  gir1.2-glib-2.0 \
  ffmpeg \
  libavformat-dev \
  libavcodec-dev \
  libavdevice-dev \
  libavutil-dev \
  libavfilter-dev \
  libswscale-dev \
  libswresample-dev

echo "🔵 Enabling Bluetooth + NetworkManager..."
sudo systemctl enable bluetooth
sudo systemctl start bluetooth

sudo systemctl enable NetworkManager
sudo systemctl start NetworkManager

echo "🐍 Creating Python virtual environment..."
python3 -m venv "$VENV_DIR"

echo "⬆️ Upgrading pip..."
"$VENV_DIR/bin/pip" install --upgrade pip setuptools wheel

echo "📦 Installing Python requirements..."
"$VENV_DIR/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

echo "⚠️ Installing DBus Python bindings (Pi-safe method)..."
sudo apt-get install -y python3-dbus || true

echo "🧩 Installing optional BLE Python support..."
"$VENV_DIR/bin/pip" install \
  paho-mqtt \
  requests \
  pydbus \
  gobject \
  dbus-python || true

echo "⚙️ Installing systemd services..."

sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$PROJECT_DIR/app/systemmd/fastapi.service" > "$FASTAPI_UNIT_TMP"
sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$PROJECT_DIR/app/systemmd/wifi-reconnect.service" > "$WIFI_UNIT_TMP"
sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$PROJECT_DIR/app/systemmd/wifi-upload-agent.service" > "$WIFI_UPLOAD_UNIT_TMP"

sudo cp "$FASTAPI_UNIT_TMP" /etc/systemd/system/fastapi.service
sudo cp "$WIFI_UNIT_TMP" /etc/systemd/system/wifi-reconnect.service
sudo cp "$WIFI_UPLOAD_UNIT_TMP" /etc/systemd/system/wifi-upload-agent.service

sudo systemctl daemon-reload
sudo systemctl enable fastapi.service wifi-reconnect.service wifi-upload-agent.service
sudo systemctl restart fastapi.service wifi-reconnect.service wifi-upload-agent.service

echo "✅ INSTALLATION COMPLETE - Pi4 WiFi + BLE Agent Ready"