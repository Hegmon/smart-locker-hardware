FROM python:3.11-slim-bookworm

WORKDIR /app

ENV DEBIAN_FRONTEND=noninteractive
ENV PIP_NO_CACHE_DIR=1
ENV PYTHONDONTWRITEBYTECODE=1

# Add Raspberry Pi repository for libcamera-apps (if needed later)
RUN echo "deb http://archive.raspberrypi.com/debian bookworm main" > /etc/apt/sources.list.d/raspi.list && \
    curl -sSL https://archive.raspberrypi.com/debian/archivrepo.gpg.key | gpg --dearmor -o /etc/apt/trusted.gpg.d/raspi.gpg && \
    apt-get update || true

# Install essential system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    libgpiod2 \
    ffmpeg \
    v4l-utils \
    libjpeg62-turbo \
    libargon2-1 \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Install pip and wheel
RUN pip install --no-cache-dir --upgrade pip wheel

# Copy and install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir --break-system-packages -r requirements.txt

# Copy application code
COPY ./hardware ./hardware

# Create user (no root)
RUN useradd -m -r appuser && \
    chown -R appuser:appuser /app

USER appuser

WORKDIR /app/hardware

# Use exec form for proper signal handling
CMD ["python3", "-u", "camera_stream_service.py"]
