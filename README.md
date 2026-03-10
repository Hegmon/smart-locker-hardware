# 🛡️📷 Smart Locker Hardware System

<p align="center">
  <img src="https://img.shields.io/badge/Raspberry%20Pi%204-Model%20B-red?logo=raspberrypi" alt="Raspberry Pi 4">
  <img src="https://img.shields.io/badge/docker-%230db7ed.svg?logo=docker&logoColor=white" alt="Docker">
  <img src="https://img.shields.io/badge/GitHub%20Actions-2088FF?logo=github-actions&logoColor=white" alt="GitHub Actions">
  <img src="https://img.shields.io/badge/python-3.11+-3776AB?logo=python&logoColor=white" alt="Python">
  <img src="https://img.shields.io/badge/License-MIT-green" alt="License">
</p>

Raspberry Pi 4 based smart locker controller with camera integration, Docker deployment, automatic GitHub updates, and production-ready scaling design.

---

## ✨ Features

| Feature | Description |
|---------|-------------|
| 🔐 **GPIO Control** | Locker solenoid / relay control via GPIO pins |
| 📷 **Camera Integration** | Raspberry Pi Camera capture (photos + video streams) |
| 🌐 **REST API Communication** | Secure communication with Django backend |
| 🐳 **Docker Deployment** | Fully containerized with Docker & docker-compose |
| 🔄 **Auto-Updates** | Automatic code & image updates on boot |
| 🏗️ **Multi-Architecture** | Docker images for ARM64 / AMD64 |
| 📈 **Fleet Management** | Designed for **100+ device** fleet management |
| ⚡ **Bulk Provisioning** | Easy bulk provisioning via master SD card image + unique `.env` |

---

## 📦 Architecture Overview

```
┌───────────────────────┐       ┌───────────────────────┐
│   Raspberry Pi 4      │       │   Django Backend      │
│                       │       │                       │
│  ┌───────────────┐    │       │  ┌─────────────────┐  │
│  │ camera_service│◄───┼───────┼─►│   API Endpoints │  │
│  └───────────────┘    │  HTTP │  └─────────────────┘  │
│  ┌───────────────┐    │       │                       │
│  │locker_controller◄───┼───────┼─►│   Device Status │  │
│  └───────────────┘    │       │  └─────────────────┘  │
│        │              │       └───────────────────────┘
│   GPIO pins           │
│   Pi Camera           │
└───────────────────────┘
            ▲
            │ pull / update
            │
    GitHub + Docker Hub
```

---

## 🚀 Quick Start

### 1. Prerequisites

```bash
# Update and install essentials
sudo apt update && sudo apt upgrade -y
sudo apt install -y git docker.io docker-compose

# Add current user to docker group (recommended: user 'pi')
sudo usermod -aG docker $USER

# Log out and back in (or reboot)
reboot
```

### 2. Clone & Configure

```bash
git clone https://github.com/yourusername/smart-locker-hardware.git
cd smart-locker-hardware

# Create and edit .env file
cp .env.example .env
nano .env
```

**.env example:**
```ini
# Required
API_URL=https://yourserver.com/api
API_TOKEN=dev_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
DEVICE_ID=locker-042

# Optional / advanced
LOG_LEVEL=INFO
CAMERA_RESOLUTION=1920x1080
HEARTBEAT_INTERVAL=30
```

### 3. Docker Deployment (recommended)

```bash
# Build containers (first time ~5-10 min)
docker compose build

# Start in background
docker compose up -d

# Follow logs (most useful during setup)
docker compose logs -f camera
docker compose logs -f locker
```

---

## 🔄 Auto-update on Boot

The system uses a systemd service that runs `start.sh` → `update.sh` on every boot.

```bash
# Install systemd service (one-time)
sudo cp locker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable locker.service
sudo systemctl start locker.service

# Check status
sudo systemctl status locker.service
```

### What happens on boot:

1. `update.sh` → git pull latest code
2. Rebuilds containers if needed (`docker compose build --pull`)
3. Restarts services (`docker compose up -d`)

---

## 🏭 Scaling to 100+ Devices

| Aspect | Solution / Recommendation |
|--------|---------------------------|
| Unique identification | Unique `DEVICE_ID` in each `.env` |
| Code distribution | Single GitHub repo + auto-pull on boot |
| Container updates | GitHub Actions multi-arch builds |
| Fully automatic | Add Watchtower container |
| Centralized logging | ELK / Loki / Graylog / Fluent Bit → central server |
| Monitoring | Prometheus + Node Exporter + Grafana (lightweight on Pi) |
| Mass provisioning | Burn master SD card image → customize only `.env` per device |

---

## 📂 Project Structure

```
smart-locker-hardware/
├── .github/
│   └── workflows/
│       └── build.yml          # GitHub Actions CI/CD pipeline
├── hardware/                   # Main application logic
│   ├── camera_stream_service.py
│   └── locker_controller.py
├── scripts/                    # Boot & update logic
│   ├── start.sh
│   └── update.sh
├── .env.example               # Environment template
├── requirements.txt           # Python dependencies
├── Dockerfile                 # Docker image definition
├── docker-compose.yml         # Docker Compose configuration
├── locker.service             # systemd unit file
└── README.md                  # This file
```

---

## 🔧 Development Tips

Use `docker compose up` (without `-d`) during development for live output.

Mount local code for fast iteration:

```yaml
# in docker-compose.yml (dev override)
services:
  camera:
    volumes:
      - ./hardware:/app/hardware:ro
```

---

## 🐳 Docker Hub & GitHub Actions

This project includes automated Docker image builds via GitHub Actions:

- **Multi-architecture images**: ARM64 (Raspberry Pi) & AMD64 (x86_64)
- **Auto-push on tags**: Tag a release to push to registry
- **Cache optimization**: Faster builds with GitHub Actions cache

### Available Triggers

| Event | Action |
|-------|--------|
| Push to `main`/`master` | Build & push latest |
| Push tag `v*` | Build & push versioned release |
| Pull Request | Build (no push) |
| Manual | Full rebuild with no cache |

---

## 🤝 Contributing

Contributions are welcome! Please feel free to submit a Pull Request.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/amazing-feature`)
3. Commit your changes (`git commit -m 'Add some amazing feature'`)
4. Push to the branch (`git push origin feature/amazing-feature`)
5. Open a Pull Request

---

## 📜 License

MIT License

> *Freely use, modify, and deploy — just keep the spirit of open-source alive!*

---
