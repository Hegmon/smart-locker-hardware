# Multi-platform base image (works on Raspberry Pi ARM64 and x86_64)
FROM python:3.11-slim-bookworm

# Set working directory
WORKDIR /app

# Environment variables
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
    libcap-dev \
    pkg-config \
    cmake \
    git \
    curl && \
    rm -rf /var/lib/apt/lists/*

# Upgrade pip tools
RUN pip install --upgrade pip setuptools wheel

# Copy requirements first (better Docker caching)
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir \
    --root-user-action=ignore \
    -r requirements.txt || \
    pip install --no-cache-dir \
    --root-user-action=ignore \
    --break-system-packages \
    -r requirements.txt

# Copy hardware scripts
COPY ./hardware ./hardware

# Create non-root user for security
RUN useradd -m appuser

# Change ownership
RUN chown -R appuser:appuser /app

# Switch to non-root user
USER appuser

# Set working directory
WORKDIR /app/hardware

# Default command
CMD ["python3", "camera_stream_service.py"]