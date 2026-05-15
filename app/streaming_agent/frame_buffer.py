import threading
import time


class SharedFrameBuffer:
    """Thread-safe latest-frame buffer fed by the streaming ffmpeg process."""

    def __init__(self, width=640, height=480, channels=3):
        self.width = width
        self.height = height
        self.channels = channels
        self.frame_size = width * height * channels
        self._lock = threading.Lock()
        self._frame = None
        self._sequence = 0
        self._updated_at = 0.0

    def update(self, frame_bytes):
        with self._lock:
            self._frame = frame_bytes
            self._sequence += 1
            self._updated_at = time.monotonic()

    def latest(self):
        with self._lock:
            return self._frame, self._sequence, self._updated_at
