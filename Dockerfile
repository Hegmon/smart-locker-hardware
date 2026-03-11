# syntax=docker/dockerfile:1.3
FROM python:3.11-slim

WORKDIR /app

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_NO_CACHE_DIR=1
ENV PIP_DISABLE_PIP_VERSION_CHECK=1

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
    openssh-client \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Clone lgpio using SSH forward
# --mount=type=ssh allows Docker to access your SSH key temporarily
# You will build using: docker build --ssh default .
RUN --mount=type=ssh git clone git@github.com:agherzan/lgpio.git /tmp/lgpio \
    && cd /tmp/lgpio \
    && make \
    && make install \
    && cd / \
    && rm -rf /tmp/lgpio

# Upgrade pip
RUN pip install --upgrade pip setuptools wheel

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ./hardware ./hardware

RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

WORKDIR /app/hardware

CMD ["python3", "camera_stream_service.py"]