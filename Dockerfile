FROM python:3.11-slim

WORKDIR /app

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

# Install dependencies for Raspberry Pi GPIO + camera
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    python3-dev \
    python3-pip \
    swig \
    libatlas3-base \
    libjpeg-dev \
    libtiff-dev \
    libopenjp2-7-dev \
    libavcodec-dev \
    libavformat-dev \
    libswscale-dev \
    libgpiod-dev \
    libcap-dev \
    pkg-config \
    cmake \
    git \
    curl \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install lgpio from source (works for ARM)
# Install lgpio from source using SSH
RUN git clone git@github.com:agherzan/lgpio.git /tmp/lgpio \
    && cd /tmp/lgpio \
    && make \
    && make install \
    && cd / \
    && rm -rf /tmp/lgpio

# Upgrade pip
RUN pip install --upgrade pip setuptools wheel

# Copy requirements.txt and install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy hardware code
COPY ./hardware ./hardware

# Create non-root user
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

WORKDIR /app/hardware

CMD ["python3", "camera_stream_service.py"]