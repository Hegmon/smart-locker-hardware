FROM python:3.11-slim-bookworm

WORKDIR /app

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_NO_CACHE_DIR=1

# System deps - use apt lgpio/gpiod, avoid building from source
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-dev \
    build-essential \
    pkg-config \
    # GPIO
    python3-lgpio \
    python3-gpiozero \
    libgpiod-dev \
    python3-libgpiod \
    # Camera / OpenCV deps
    libopencv-dev \
    python3-opencv \
    libjpeg-dev \
    libtiff-dev \
    libopenjp2-7-dev \
    # AV / media
    libavcodec-dev \
    libavformat-dev \
    libswscale-dev \
    libavdevice-dev \
    # Utils
    v4l-utils \
    curl \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Upgrade pip
RUN pip install --upgrade pip setuptools wheel

# Copy and install requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app code
COPY ./hardware ./hardware

# Non-root user with video/gpio group access
RUN groupadd -f video && groupadd -f gpio && groupadd -f i2c \
    && useradd -m appuser \
    && usermod -aG video,gpio,i2c appuser \
    && chown -R appuser:appuser /app

USER appuser

WORKDIR /app/hardware

CMD ["python3", "camera_stream_service.py"]