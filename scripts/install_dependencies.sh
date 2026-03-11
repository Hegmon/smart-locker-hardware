#!/bin/bash
# Install system dependencies
sudo apt-get update
sudo apt-get install -y \
    python3-pip \
    python3-dev \
    ffmpeg \
    v4l-utils \
    libcamera-apps \
    libgpiod2 \
    libjpeg62-turbo \
    libargon2-1 \
    python3-gpiozero \
    python3-picamera2

# Install Python packages
pip3 install --break-system-packages \
    gpiozero==2.0.1 \
    lgpio==0.2.2.0 \
    gpiod==2.1.3 \
    smbus2==0.4.2 \
    spidev==3.5 \
    picamera2==0.3.31 \
    opencv-python-headless==4.8.1.78 \
    av==12.3.0 \
    numpy==1.24.4 \
    Pillow==10.0.1 \
    requests==2.28.2 \
    python-dotenv==1.0.0 \
    python-dateutil==2.8.2 \
    pytz==2023.3 \
    tqdm==4.65.0 \
    cryptography==41.0.3

echo "Dependencies installed!"
