#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"
FASTAPI_UNIT_TMP="$(mktemp)"
WIFI_UNIT_TMP="$(mktemp)"

cleanup() {
  rm -f "$FASTAPI_UNIT_TMP" "$WIFI_UNIT_TMP"
}

trap cleanup EXIT

sudo apt-get update
sudo apt-get install -y \
  python3 \
  python3-venv \
  python3-pip \
  network-manager \
  pkg-config \
  ffmpeg \
  libavformat-dev \
  libavcodec-dev \
  libavdevice-dev \
  libavutil-dev \
  libavfilter-dev \
  libswscale-dev \
  libswresample-dev
sudo systemctl enable NetworkManager
sudo systemctl start NetworkManager

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$PROJECT_DIR/app/systemmd/fastapi.service" > "$FASTAPI_UNIT_TMP"
sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$PROJECT_DIR/app/systemmd/wifi-reconnect.service" > "$WIFI_UNIT_TMP"

sudo cp "$FASTAPI_UNIT_TMP" /etc/systemd/system/fastapi.service
sudo cp "$WIFI_UNIT_TMP" /etc/systemd/system/wifi-reconnect.service

sudo systemctl daemon-reload
sudo systemctl enable fastapi.service wifi-reconnect.service
sudo systemctl restart fastapi.service wifi-reconnect.service
