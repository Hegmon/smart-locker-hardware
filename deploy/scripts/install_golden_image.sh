#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="${1:-$(pwd)}"
SYSTEMD_DIR="/etc/systemd/system"
CONFIG_DIR="/etc/smartlocker"
LOGROTATE_DIR="/etc/logrotate.d"

if [[ ! -d "$PROJECT_DIR" ]]; then
  echo "Project directory does not exist: $PROJECT_DIR" >&2
  exit 1
fi

if [[ ! -x "$PROJECT_DIR/venv/bin/python" ]]; then
  echo "Missing Python runtime at: $PROJECT_DIR/venv/bin/python" >&2
  exit 1
fi

mkdir -p "$CONFIG_DIR"
mkdir -p /var/lib/smartlocker
mkdir -p /var/log/smartlocker

if [[ ! -f "$CONFIG_DIR/config.json" ]]; then
  cp "$PROJECT_DIR/deploy/config/config.json.sample" "$CONFIG_DIR/config.json"
fi

if [[ ! -f "$CONFIG_DIR/.env" ]]; then
  cp "$PROJECT_DIR/deploy/config/smartlocker.env.sample" "$CONFIG_DIR/.env"
fi

for unit in \
  smartlocker-bootstrap.service \
  smartlocker-device-registry.service \
  smartlocker-hardware-agent.service \
  smartlocker-streaming-agent.service
do
  sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
    "$PROJECT_DIR/deploy/systemd/$unit" > "$SYSTEMD_DIR/$unit"
done

cp "$PROJECT_DIR/deploy/logrotate/smartlocker" "$LOGROTATE_DIR/smartlocker"

systemctl daemon-reload
systemctl enable smartlocker-bootstrap.service
systemctl enable smartlocker-device-registry.service
systemctl enable smartlocker-hardware-agent.service
systemctl enable smartlocker-streaming-agent.service

echo "Smart Locker golden-image services installed."
