# Use official Python ARM image
FROM python:3.11-slim-bullseye

# Set working directory
WORKDIR /app

# Avoid prompts during install
ENV DEBIAN_FRONTEND=noninteractive

# Install system dependencies for Pi GPIO & cameras
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential \
    python3-dev \
    libatlas-base-dev \
    libjpeg-dev \
    libtiff-dev \
    libopenjp2-7-dev \
    libavcodec-dev \
    libavformat-dev \
    libswscale-dev \
    cmake \
    git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy all hardware scripts
COPY ./hardware ./hardware

# Set default working directory for container
WORKDIR /app/hardware

# Default command (can be overridden in docker-compose)
CMD ["python3", "camera_service.py"]