FROM python:3.11-slim-bookworm

WORKDIR /app

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3-dev \
    build-essential \
    pkg-config \
    # GPIO
    libgpiod-dev \
    python3-gpiod \
    gpiod \
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

RUN pip install --upgrade pip setuptools wheel

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY ./hardware ./hardware

RUN groupadd -f video && groupadd -f gpio && groupadd -f i2c \
    && useradd -m appuser \
    && usermod -aG video,gpio,i2c appuser \
    && chown -R appuser:appuser /app

USER appuser

WORKDIR /app/hardware

CMD ["python3", "camera_stream_service.py"]