Smart Locker Hardware System (Raspberry Pi 4)
Overview
This repository contains the hardware software for the Smart Locker system running on Raspberry Pi 4. It is designed to:

Control lockers via GPIO pins
Capture camera feeds using Pi Camera
Communicate with a Django backend for status updates and commands
Automatically update itself from GitHub
Run in Docker containers for easy deployment and reproducibility

This system is ready for bulk production by cloning the SD card image to multiple devices.

Architecture
Components

Hardware Services
camera_service.py → captures video, images, and sends data to backend
locker_controller.py → controls locker opening/closing via GPIO

Docker
Each service runs inside its own Docker container
Uses volumes to store logs and data persistently

Update System
update.sh → pulls the latest code from GitHub, installs dependencies, rebuilds containers
start.sh → runs services on boot

CI/CD
GitHub Actions pipeline builds multi-architecture Docker images (ARM for Pi)
Pushes images to Docker Hub

Networking
Services communicate with Django backend via HTTP API
Each device has a unique DEVICE_ID configured via .env



Setup Instructions
Prerequisites

Raspberry Pi 4 (ARMv7/ARM64)
Raspberry Pi OS (64-bit recommended)
Docker & Docker Compose installed

Bashsudo apt update && sudo apt install -y docker.io docker-compose
sudo usermod -aG docker pi
sudo apt install git -y
Clone Repository
Bashgit clone https://github.com/yourusername/smart-locker-hardware.git
cd smart-locker-hardware
Environment Variables
Create .env in the project root:
textAPI_TOKEN=YOUR_SECURE_DEVICE_TOKEN
API_URL=https://yourserver.com/api
DEVICE_ID=locker-001
For bulk production, each device can have a unique DEVICE_ID.
Install Dependencies (if not using Docker)
Bashpip3 install -r requirements.txt
Docker Setup

Build images for Pi:Bashdocker-compose build
Start services:Bashdocker-compose up -d
Check logs:Bashdocker logs -f camera_service
docker logs -f locker_controller

Auto-Update on Boot

start.sh runs at boot (via systemd) and executes update.sh:Bashsudo cp start.sh /usr/local/bin/start.sh
sudo chmod +x /usr/local/bin/start.sh
Systemd service:Bashsudo cp locker.service /etc/systemd/system/
sudo systemctl enable locker.service
sudo systemctl start locker.service
On boot, the Pi will:
Pull the latest code from GitHub
Install any new dependencies
Restart Docker containers with the new code


GitHub Actions Pipeline

CI/CD pipeline builds Docker images for ARM & AMD architectures
Pushes image to Docker Hub
Allows automatic updates on each Pi when update.sh is run

Scaling to 100+ Devices

Docker & .env isolation
Each Pi runs the same Docker image
.env file ensures unique DEVICE_ID and secure API token

GitHub updates
Single repo → all devices can pull updates simultaneously
Docker containers handle restarts automatically

Backend considerations
Django backend should handle concurrent API requests
Use database indexing and caching for performance
Optional: load balancer if you have multiple backend servers

Resource management on Pi
Each Pi runs lightweight Docker containers (camera + locker)
CPU & memory limits can be applied via Docker Compose to prevent overload

Logging & monitoring
Centralized logging system (e.g., ELK Stack or Graylog) can be used for 100+ devices
Optionally, Watchtower can auto-pull Docker images for fully automated updates


File Structure
textsmart-locker-hardware/
├── hardware/
│   ├── camera_service.py
│   └── locker_controller.py
├── scripts/
│   ├── update.sh
│   └── start.sh
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── locker.service        # systemd service
└── .env                  # environment variables
Tips for Bulk Production

Create a master SD card image with Docker and all dependencies installed
Clone the SD card for all Pis
Each Pi only needs a unique .env file for API credentials and device ID
Use update.sh to push code updates to all devices automatically
