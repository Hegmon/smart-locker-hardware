# Multi-platform base image for Raspberry Pi (ARM64) and x86_64
# For Raspberry Pi use: python:3.11-slim-bookworm
FROM python:3.11-slim-bookworm

# Set working directory
WORKDIR /app

# Avoid prompts during install
ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

# Install system dependencies for Pi GPIO & cameras
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential \
    python3-dev \
    python3-pip \
    libatlas-base-dev \
    libjpeg-dev \
    libtiff-dev \
    libopenjp2-7-dev \
    libavcodec-dev \
    libavformat-dev \
    libswscale-dev \
    libgpiod2 \
    libgpiod-dev \
    cmake \
    git \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -s /bin/bash appuser

# Upgrade pip first to avoid issues
RUN pip install --upgrade pip setuptools wheel

# Copy requirements
COPY requirements.txt .

# Install Python dependencies with error handling
# Use --ignore-installed to avoid conflicts
RUN pip install --no-cache-dir --upgrade \
    --root-user-action=ignore \
    -r requirements.txt || \
    pip install --no-cache-dir --upgrade \
    --root-user-action=ignore \
    --break-system-packages \
    -r requirements.txt

# Copy all hardware scripts
COPY ./hardware ./hardware

# Set default working directory for container
WORKDIR /app/hardware

# Switch to non-root user
USER appuser

# Default command (can be overridden in docker-compose)
CMD ["python3", "camera_service.py"]
