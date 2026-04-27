# Smart Locker Hardware

FastAPI service for a Raspberry Pi 4 that:

- lists available Wi-Fi networks
- connects the Pi to a selected Wi-Fi network
- enables hotspot mode when Wi-Fi is not connected
- switches back to normal Wi-Fi client mode after a successful connection

## API

- `GET /` returns service status
- `GET /system/health` returns health status
- `GET /device/heartbeat` returns device heartbeat
- `GET /device/backend/status` returns backend registration state and stored device UUID
- `POST /device/backend/register` registers the Pi in the Django backend one time
- `POST /device/backend/telemetry` sends telemetry to the Django backend immediately
- `GET /hardware/status` returns detected hardware status
- `GET /hardware/lights/status` returns light controller status
- `GET /hardware/cameras/status` returns internal and external camera detection status
- `GET /wifi/status` returns current Wi-Fi state
- `GET /wifi/scan` lists nearby Wi-Fi networks
- `POST /wifi/connect` connects to a Wi-Fi network
- `POST /wifi/disconnect` disconnects Wi-Fi and enables hotspot mode
- `POST /wifi/hotspot/start` forces hotspot mode

Example connect request:

```json
{
  "ssid": "MyWifi",
  "password": "MyPassword"
}
```

## Raspberry Pi 4 setup

Use Raspberry Pi OS with NetworkManager enabled. This project now uses `nmcli` for both station mode and hotspot mode, so you should not separately manage `wpa_supplicant`, `hostapd`, or `dnsmasq` for the same `wlan0` interface.

1. Copy the project to `/home/pi/smart-locker-hardware`.
2. Install dependencies:

```bash
cd /home/pi/smart-locker-hardware
chmod +x app/scripts/install.sh app/scripts/start_ap.sh app/scripts/stop_ap.sh
./app/scripts/install.sh
```

3. Optional hotspot settings:

```bash
export HOTSPOT_SSID="SmartLocker-Setup"
export HOTSPOT_PASSWORD="SmartLocker123"
export HOTSPOT_CONNECTION="SmartLockerHotspot"
export WIFI_INTERFACE="wlan0"
```

If you want those values to persist in systemd, add them in the service files or with `Environment=` lines before enabling the services.

4. Start services:

```bash
sudo systemctl restart fastapi.service
sudo systemctl restart wifi-reconnect.service
```

5. Check service logs:

```bash
sudo systemctl status fastapi.service
sudo systemctl status wifi-reconnect.service
journalctl -u fastapi.service -f
journalctl -u wifi-reconnect.service -f
```

6. Backend registration and telemetry:

```bash
curl http://127.0.0.1:8000/device/backend/status
curl -X POST http://127.0.0.1:8000/device/backend/register
curl -X POST http://127.0.0.1:8000/device/backend/telemetry
```

Expected result:
- the first successful registration stores the Django `id` UUID in `app/config/backend_device.json`
- later restarts reuse that stored UUID and do not create a second hardware device
- telemetry posts always use that stored UUID as the `device` field

Example cron job to send telemetry every 5 minutes:

```bash
*/5 * * * * cd /home/pi/smart-locker-hardware && /home/pi/smart-locker-hardware/.venv/bin/python -m app.scripts.send_telemetry >> /var/log/qbox-telemetry.log 2>&1
```

## Pi 4 test plan

1. Boot test:
   Confirm both services are active after reboot.

```bash
sudo reboot
sudo systemctl status fastapi.service
sudo systemctl status wifi-reconnect.service
```

2. Hotspot fallback test:
   Start with the Pi not connected to any saved Wi-Fi or with the router powered off.

```bash
nmcli device status
nmcli connection show --active
```

Expected result: `SmartLockerHotspot` becomes active on `wlan0`.

3. Scan Wi-Fi test:

```bash
curl http://127.0.0.1:8000/wifi/scan
```

Expected result: JSON list of nearby SSIDs with signal strength.

4. Connect Wi-Fi test:

```bash
curl -X POST http://127.0.0.1:8000/wifi/connect \
  -H "Content-Type: application/json" \
  -d '{"ssid":"YOUR_WIFI_NAME","password":"YOUR_WIFI_PASSWORD"}'
```

Expected result: API returns `"status":"connected"`, the hotspot goes down, and `nmcli connection show --active` shows your Wi-Fi.

5. Failover test:
   After the Pi is connected to Wi-Fi, power off the router or move the Pi out of range. Wait about 15 seconds.

```bash
nmcli connection show --active
curl http://127.0.0.1:8000/wifi/status
```

Expected result: hotspot mode comes back automatically.

6. Recovery test:
   Turn the router back on, then call the connect API again if needed.

Expected result: the Pi reconnects to the target Wi-Fi and leaves hotspot mode.

## Notes

- `wifi-reconnect.service` runs as `root` because changing network mode on the Pi requires elevated privileges.
- `fastapi.service` also runs as `root` so the connect, disconnect, scan, and hotspot endpoints can call `nmcli` successfully from systemd.
- If `wlan0` is being managed by another tool, disable that conflict first.
- Camera detection uses `/dev/video*` and optionally `libcamera-hello --list-cameras`.
- You can override the expected camera device paths with `INTERNAL_CAMERA_DEVICE` and `EXTERNAL_CAMERA_DEVICE`.
- Light status is configuration-based until the actual GPIO light on/off control logic is added.
- Backend registration runs at FastAPI startup. If `app/config/backend_device.json` already contains a `device_uuid`, registration is skipped.
- Use `QBOX_DEVICE_NAME`, `QBOX_DEVICE_REGISTRATION_URL`, `QBOX_TELEMETRY_URL`, `QBOX_AUTO_REGISTER`, and `LOCKER_DEFAULT_STATUS` to override backend sync behavior.
