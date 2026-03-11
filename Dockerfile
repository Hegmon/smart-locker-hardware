FROM python:3.11-slim

WORKDIR /app

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

# Install only essential system packages for building lgpio and Python deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    python3-dev \
    python3-pip \
    swig \
    libblas-dev \
    liblapack-dev \
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
    curl \
    unzip \
    && apt-get clean && rm -rf /var/lib/apt/lists/*

# Download lgpio as ZIP and install
RUN curl -L -o /tmp/lgpio.zip https://github.com/agherzan/lgpio/archive/refs/heads/master.zip \
    && mkdir -p /tmp/lgpio \
    && unzip /tmp/lgpio.zip -d /tmp/lgpio \
    && cd /tmp/lgpio/lgpio-master \
    && make \
    && make install \
    && cd / && rm -rf /tmp/lgpio /tmp/lgpio.zip

# Upgrade pip
RUN pip install --upgrade pip setuptools wheel

# Copy requirements and install Python packages
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy hardware code
COPY ./hardware ./hardware

# Create non-root user
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

WORKDIR /app/hardware

CMD ["python3", "camera_stream_service.py"]