FROM python:3.11-slim-bullseye

WORKDIR /app

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

# upgrade pip
RUN pip install --upgrade pip setuptools wheel

# copy requirements
COPY requirements.txt .

# install python packages
RUN pip install --no-cache-dir -r requirements.txt

# copy hardware code
COPY ./hardware ./hardware

# create non root user
RUN useradd -m appuser && chown -R appuser:appuser /app
USER appuser

WORKDIR /app/hardware

CMD ["python3", "camera_stream_service.py"]