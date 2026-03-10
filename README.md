# Smart Locker Hardware System 🛡️📷

Raspberry Pi 4 based smart locker controller with camera integration, Docker deployment, automatic GitHub updates, and production-ready scaling design.

[![Raspberry Pi](https://img.shields.io/badge/Raspberry%20Pi%204-Model%20B-red?logo=raspberrypi)](https://www.raspberrypi.com/products/raspberry-pi-4-model-b/)
[![Docker](https://img.shields.io/badge/docker-%230db7ed.svg?logo=docker&logoColor=white)](https://www.docker.com/)
[![GitHub Actions](https://img.shields.io/badge/GitHub%20Actions-2088FF?logo=github-actions&logoColor=white)](#github-actions-pipeline)

## ✨ Features

- GPIO-based locker solenoid / relay control
- Raspberry Pi Camera capture (photos + video streams)
- Secure communication with Django backend via REST API
- Fully containerized with **Docker & docker-compose**
- Automatic code & image updates on boot
- Multi-architecture Docker images (ARM64 / AMD64)
- Designed for **100+ device** fleet management
- Easy bulk provisioning via master SD card image + unique `.env`

## 🚀 Quick Start

### 1. Prerequisites

```bash
# Update system and install essentials
sudo apt update && sudo apt upgrade -y
sudo apt install -y git docker.io docker-compose

# Add current user to docker group (recommended: user 'pi')
sudo usermod -aG docker $USER

# Important: log out and log back in (or reboot) for group change to take effect
2. Clone & Configure
Bashgit clone https://github.com/yourusername/smart-locker-hardware.git
cd smart-locker-hardware

# Create and edit environment file
cp .env.example .env
nano .env
.env example:
ini# ────────────────────────────────────────────────
# Required settings
API_URL=https://yourserver.com/api
API_TOKEN=dev_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
DEVICE_ID=locker-042

# Optional / tuning parameters
LOG_LEVEL=INFO
CAMERA_RESOLUTION=1920x1080
HEARTBEAT_INTERVAL=30          # seconds
3. Docker Deployment (recommended)
Bash# Build containers (first time: ~5–10 minutes)
docker compose build

# Start services in detached mode
docker compose up -d

# View logs (very useful during initial setup)
docker compose logs -f camera
docker compose logs -f locker
🔄 Auto-update on Boot
The system uses a systemd service that runs start.sh → update.sh automatically on every boot.
Bash# Install the systemd service (one-time setup)
sudo cp locker.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable locker.service
sudo systemctl start locker.service

# Check service status
sudo systemctl status locker.service
What happens automatically on every boot:

update.sh performs git pull → gets latest code
Rebuilds containers when needed (docker compose build --pull)
Restarts services (docker compose up -d)

🏭 Scaling to 100+ Devices

AspectSolution / RecommendationUnique identificationUnique DEVICE_ID in each .env fileCode distributionSingle GitHub repo + auto-pull on bootContainer updatesDocker Hub + GitHub Actions multi-arch buildsFully automaticAdd Watchtower containerCentralized loggingELK / Loki / Graylog / Fluent Bit → central serverMonitoringPrometheus + Node Exporter + Grafana (lightweight on Pi)Mass provisioningBurn master SD card image → customize only .env per device
📂 Project Structure
textsmart-locker-hardware/
├── hardware/                    # Core application logic
│   ├── camera_service.py
│   └── locker_controller.py
├── scripts/                     # Boot & update scripts
│   ├── start.sh
│   └── update.sh
├── .env.example                 # Template for environment variables
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── locker.service               # systemd unit file
└── README.md
🔧 Development Tips

Run interactively during development:

Bashdocker compose up

For fast code iteration, mount local source code:

YAML# Add to docker-compose.yml (development override)
services:
  camera:
    volumes:
      - ./hardware:/app/hardware:ro
  locker:
    volumes:
      - ./hardware:/app/hardware:ro
📜 License
MIT License
Feel free to use, modify, and deploy — attribution appreciated but not required.
