#!/usr/bin/env bash
set -euo pipefail

API_BASE_URL="${API_BASE_URL:-http://127.0.0.1:8000}"
WIFI_INTERFACE="${WIFI_INTERFACE:-wlan0}"
TEST_SSID="${TEST_SSID:-}"
TEST_PASSWORD="${TEST_PASSWORD:-}"

print_section() {
  printf '\n== %s ==\n' "$1"
}

run_cmd() {
  printf '\n$ %s\n' "$*"
  "$@"
}

wait_for_api() {
  local attempts=20
  local delay=2

  for ((i=1; i<=attempts; i++)); do
    if curl -fsS "$API_BASE_URL/system/health" >/dev/null 2>&1; then
      return 0
    fi
    sleep "$delay"
  done

  echo "API did not become ready at $API_BASE_URL"
  return 1
}

confirm() {
  local prompt="$1"
  local answer
  read -r -p "$prompt [y/N]: " answer
  [[ "$answer" == "y" || "$answer" == "Y" ]]
}

print_section "Environment"
run_cmd pwd
run_cmd python3 --version
run_cmd nmcli --version
run_cmd ip link show "$WIFI_INTERFACE"

print_section "Restart Services"
run_cmd sudo systemctl daemon-reload
run_cmd sudo systemctl restart fastapi.service
run_cmd sudo systemctl restart wifi-reconnect.service
run_cmd sudo systemctl status --no-pager fastapi.service
run_cmd sudo systemctl status --no-pager wifi-reconnect.service

print_section "Wait For API"
wait_for_api
run_cmd curl -fsS "$API_BASE_URL/"
run_cmd curl -fsS "$API_BASE_URL/system/health"
run_cmd curl -fsS "$API_BASE_URL/device/heartbeat"
run_cmd curl -fsS "$API_BASE_URL/wifi/status"

print_section "Inspect NetworkManager"
run_cmd nmcli general status
run_cmd nmcli device status
run_cmd nmcli connection show
run_cmd nmcli connection show --active

print_section "Scan Wi-Fi"
run_cmd curl -fsS "$API_BASE_URL/wifi/scan"
run_cmd nmcli dev wifi list ifname "$WIFI_INTERFACE"

if confirm "Run hotspot fallback test? This will disconnect current Wi-Fi."; then
  print_section "Hotspot Fallback"
  run_cmd curl -fsS -X POST "$API_BASE_URL/wifi/disconnect"
  sleep 8
  run_cmd nmcli connection show --active
  run_cmd ip addr show "$WIFI_INTERFACE"
  run_cmd curl -fsS "$API_BASE_URL/wifi/status"
fi

if [[ -n "$TEST_SSID" ]]; then
  print_section "Connect To Test Wi-Fi"
  payload=$(printf '{"ssid":"%s","password":"%s"}' "$TEST_SSID" "$TEST_PASSWORD")
  run_cmd curl -fsS -X POST "$API_BASE_URL/wifi/connect" \
    -H "Content-Type: application/json" \
    -d "$payload"
  sleep 8
  run_cmd nmcli connection show --active
  run_cmd nmcli device status
  run_cmd curl -fsS "$API_BASE_URL/wifi/status"
  run_cmd hostname -I
  run_cmd ping -c 4 8.8.8.8
else
  print_section "Connect To Test Wi-Fi"
  echo "Skipping Wi-Fi connect test because TEST_SSID is not set."
  echo 'Example: TEST_SSID="MyWifi" TEST_PASSWORD="MyPassword" ./app/scripts/test_pi.sh'
fi

if confirm "Run reboot-persistence test now? The Pi will reboot immediately."; then
  print_section "Reboot Test"
  echo "After reboot, run these commands:"
  echo "sudo systemctl status fastapi.service wifi-reconnect.service"
  echo "curl $API_BASE_URL/system/health"
  echo "curl $API_BASE_URL/wifi/status"
  echo "nmcli connection show --active"
  sudo reboot
fi

print_section "Recent Logs"
run_cmd sudo journalctl -u fastapi.service -n 50 --no-pager
run_cmd sudo journalctl -u wifi-reconnect.service -n 50 --no-pager

print_section "Test Complete"
echo "Pi network test flow finished."
