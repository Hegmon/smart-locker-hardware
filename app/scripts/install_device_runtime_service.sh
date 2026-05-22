#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

GLOBAL_SERVICE_SRC="$PROJECT_DIR/app/systemmd/qbox-device.service"
GLOBAL_SERVICE_TMP="$(mktemp)"

cleanup() {
  rm -f "$GLOBAL_SERVICE_TMP"
}
trap cleanup EXIT

if [[ ! -f "$GLOBAL_SERVICE_SRC" ]]; then
  echo "Service file not found: $GLOBAL_SERVICE_SRC" >&2
  exit 1
fi

sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$GLOBAL_SERVICE_SRC" > "$GLOBAL_SERVICE_TMP"

echo "Installing qbox-device.service for project: $PROJECT_DIR"
echo "Removing stale qbox-device.service drop-in overrides so every device uses the repo service template"
sudo rm -rf /etc/systemd/system/qbox-device.service.d
sudo cp "$GLOBAL_SERVICE_TMP" /etc/systemd/system/qbox-device.service

echo "Disabling old split agent services so the device keeps one MQTT connection"
sudo systemctl disable --now wifi-upload-agent.service qbox-streams.service 2>/dev/null || true
sudo systemctl disable --now qbox-telemetry-agent.service qbox-heartbeat-agent.service qbox-control-agent.service 2>/dev/null || true
sudo systemctl disable --now qbox-wifi-agent.service qbox-streaming-agent.service 2>/dev/null || true

sudo systemctl daemon-reload
sudo systemctl enable qbox-device.service
sudo systemctl restart qbox-device.service

echo "qbox-device.service is running. Follow logs with:"
echo "  sudo journalctl -u qbox-device.service -f"
