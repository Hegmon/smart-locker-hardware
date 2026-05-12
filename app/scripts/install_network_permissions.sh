#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
RULE_SRC="$PROJECT_DIR/deploy/polkit/49-smartlocker-networkmanager.rules"
TARGET_USER="${1:-$(whoami)}"

if [ ! -f "$RULE_SRC" ]; then
  echo "Missing polkit rule: $RULE_SRC" >&2
  exit 1
fi

sudo groupadd -f netdev
sudo usermod -aG netdev "$TARGET_USER"
sudo install -m 0644 "$RULE_SRC" /etc/polkit-1/rules.d/49-smartlocker-networkmanager.rules
sudo systemctl restart polkit || sudo systemctl restart polkit.service || true

echo "Installed NetworkManager permissions for $TARGET_USER."
echo "Log out/in or reboot before running the agent as this user."
