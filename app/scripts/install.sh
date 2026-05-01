#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
VENV_DIR="$PROJECT_DIR/.venv"

# MQTT values (can be overridden in environment)
MQTT_HOST="${MQTT_HOST:-69.62.125.223}"
MQTT_PORT="${MQTT_PORT:-1883}"

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
  python3-dev \
  python3-gi \
  build-essential \
  pkg-config \
  git \
  curl \
  network-manager \
  dbus \
  libdbus-1-dev \
  libdbus-glib-1-dev \
  libffi-dev \
  libssl-dev \
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
if [ -d "$VENV_DIR" ]; then
  echo "ℹ️ Virtualenv already exists at $VENV_DIR"
else
  echo "🐍 Creating virtualenv at $VENV_DIR"
  python3 -m venv "$VENV_DIR"
fi

echo "⬆️ Activating virtualenv and upgrading pip..."
# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"
python -m pip install --upgrade pip setuptools wheel

echo "📦 Installing Python requirements..."
if [ -f "$PROJECT_DIR/requirements.txt" ]; then
  pip install --no-cache-dir -r "$PROJECT_DIR/requirements.txt"
else
  echo "⚠️ requirements.txt not found at $PROJECT_DIR/requirements.txt — skipping Python deps"
fi

echo "⚠️ Ensuring DBus and Python DBus bindings are installed (avoid common DBus errors)"
sudo apt-get install -y dbus-user-session python3-dbus || true

echo "🧩 Installing optional Python extras (BLE/MQTT) into virtualenv"
pip install --no-cache-dir paho-mqtt requests pydbus || true

# Ensure the `dbus` module is importable in the virtualenv. Try pip install first,
# if that fails recreate the venv with --system-site-packages to use system python3-dbus.
echo "🔎 Verifying Python 'dbus' module availability in the venv"
if python -c "import dbus" >/dev/null 2>&1; then
  echo "✅ 'dbus' module is available in the venv"
else
  echo "⚠️ 'dbus' not found in venv — attempting pip install dbus-python"
  if pip install --no-cache-dir dbus-python >/dev/null 2>&1; then
    echo "✅ Successfully installed dbus-python into venv"
  else
    echo "❗ pip install dbus-python failed — recreating venv with system-site-packages"
    deactivate >/dev/null 2>&1 || true
    rm -rf "$VENV_DIR"
    python3 -m venv --system-site-packages "$VENV_DIR"
    # shellcheck source=/dev/null
    source "$VENV_DIR/bin/activate"
    if python -c "import dbus" >/dev/null 2>&1; then
      echo "✅ 'dbus' available via system-site-packages"
    else
      echo "❌ 'dbus' still not importable — check system python3-dbus installation"
    fi
  fi
fi

echo "🔐 Enable systemd user lingering so user services can run without a user session"
sudo loginctl enable-linger "$(whoami)" || true

echo "⚙️ Installing systemd services..."

sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$PROJECT_DIR/app/systemmd/fastapi.service" > "$FASTAPI_UNIT_TMP"
sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$PROJECT_DIR/app/systemmd/wifi-reconnect.service" > "$WIFI_UNIT_TMP"
sed "s|__PROJECT_DIR__|$PROJECT_DIR|g" "$PROJECT_DIR/app/systemmd/wifi-upload-agent.service" > "$WIFI_UPLOAD_UNIT_TMP"

# Inject/overwrite MQTT env vars into unit files so services use the desired broker
for f in "$FASTAPI_UNIT_TMP" "$WIFI_UNIT_TMP" "$WIFI_UPLOAD_UNIT_TMP"; do
  if grep -q "Environment=MQTT_HOST" "$f"; then
    sed -i "s|Environment=MQTT_HOST=.*|Environment=MQTT_HOST=$MQTT_HOST|" "$f" || true
  else
    sed -i "/Environment=PYTHONUNBUFFERED=1/a Environment=MQTT_HOST=$MQTT_HOST" "$f" || true
  fi

  if grep -q "Environment=MQTT_PORT" "$f"; then
    sed -i "s|Environment=MQTT_PORT=.*|Environment=MQTT_PORT=$MQTT_PORT|" "$f" || true
  else
    sed -i "/Environment=MQTT_HOST=$MQTT_HOST/a Environment=MQTT_PORT=$MQTT_PORT" "$f" || true
  fi
done

# Also update the in-repo agent service file so it's consistent for manual installs
AGENT_SERVICE_SRC="$PROJECT_DIR/app/hardware_agent/service.service"
if [ -f "$AGENT_SERVICE_SRC" ]; then
  if grep -q "Environment=MQTT_HOST" "$AGENT_SERVICE_SRC"; then
    sed -i "s|Environment=MQTT_HOST=.*|Environment=MQTT_HOST=$MQTT_HOST|" "$AGENT_SERVICE_SRC" || true
  else
    sed -i "/Environment=PYTHONUNBUFFERED=1/a Environment=MQTT_HOST=$MQTT_HOST" "$AGENT_SERVICE_SRC" || true
  fi

  if grep -q "Environment=MQTT_PORT" "$AGENT_SERVICE_SRC"; then
    sed -i "s|Environment=MQTT_PORT=.*|Environment=MQTT_PORT=$MQTT_PORT|" "$AGENT_SERVICE_SRC" || true
  else
    sed -i "/Environment=MQTT_HOST=$MQTT_HOST/a Environment=MQTT_PORT=$MQTT_PORT" "$AGENT_SERVICE_SRC" || true
  fi
fi

sudo cp "$FASTAPI_UNIT_TMP" /etc/systemd/system/fastapi.service
sudo cp "$WIFI_UNIT_TMP" /etc/systemd/system/wifi-reconnect.service
sudo cp "$WIFI_UPLOAD_UNIT_TMP" /etc/systemd/system/wifi-upload-agent.service

sudo systemctl daemon-reload
sudo systemctl enable fastapi.service wifi-reconnect.service wifi-upload-agent.service
sudo systemctl restart fastapi.service wifi-reconnect.service wifi-upload-agent.service

echo "✅ INSTALLATION COMPLETE - Pi4 WiFi + BLE Agent Ready"