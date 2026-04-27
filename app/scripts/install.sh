#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/home/pi/smart-locker-hardware"
VENV_DIR="$PROJECT_DIR/.venv"

sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip network-manager
sudo systemctl enable NetworkManager
sudo systemctl start NetworkManager

python3 -m venv "$VENV_DIR"
"$VENV_DIR/bin/pip" install --upgrade pip
"$VENV_DIR/bin/pip" install -r "$PROJECT_DIR/requirements.txt"

sudo cp "$PROJECT_DIR/app/systemmd/fastapi.service" /etc/systemd/system/fastapi.service
sudo cp "$PROJECT_DIR/app/systemmd/wifi-reconnect.service" /etc/systemd/system/wifi-reconnect.service

sudo systemctl daemon-reload
sudo systemctl enable fastapi.service wifi-reconnect.service
sudo systemctl restart fastapi.service wifi-reconnect.service
