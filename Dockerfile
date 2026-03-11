FROM python:3.11-slim-bullseye

# Set working directory
WORKDIR /app

# Avoid interactive prompts
ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

# Install dependencies needed for Raspberry Pi GPIO + camera
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    python3-dev \
    python3-pip \
    swig \
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
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Upgrade pip and install Python packages needed for GPIO
RUN pip install --upgrade pip setuptools wheel

# Install lgpio Python package instead of apt package
RUN pip install lgpio

# Copy requirements.txt and install other Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy hardware code
COPY ./hardware ./hardware

# Create a non-root user and switch to it
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

WORKDIR /app/hardware

# Default command
CMD ["python3", "camera_stream_service.py"]