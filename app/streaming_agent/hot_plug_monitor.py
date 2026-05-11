import threading
import time

import pyudev

from app.streaming_agent.logs.streaming_agent_logs import LoggingManager


logger = LoggingManager.get_logger(__name__)


class HotPlugMonitor:
    """Monitor USB camera add/remove events and rebuild streams."""

    def __init__(self, stream_manager, debounce_seconds=3):
        self.stream_manager = stream_manager
        self.debounce_seconds = debounce_seconds
        self.running = False
        self.thread = None
        self.lock = threading.Lock()
        self.context = pyudev.Context()
        self.monitor = pyudev.Monitor.from_netlink(self.context)
        self.monitor.filter_by(subsystem="video4linux")
        self.last_event_time = 0

    def start(self):
        with self.lock:
            if self.running:
                logger.info("Hot plug monitor is already running")
                return

            logger.info("Starting hot plug monitor")
            self.running = True
            self.thread = threading.Thread(target=self._monitor_loop, daemon=True, name="hot-plug-monitor")
            self.thread.start()
            logger.info("Hot plug monitor started successfully")

    def stop(self):
        with self.lock:
            if not self.running:
                logger.info("Hot plug monitor is not running")
                return

            logger.info("Stopping hot plug monitor")
            self.running = False

        if self.thread:
            self.thread.join(timeout=2)
            self.thread = None
        logger.info("Hot plug monitor stopped successfully")

    def _monitor_loop(self):
        while self.running:
            try:
                device = self.monitor.poll(timeout=1)
                if device is None:
                    continue
                self._handle_device_event(device)
            except Exception:
                logger.exception("Hot plug monitoring error")
                time.sleep(2)

    def _handle_device_event(self, device):
        current_time = time.time()
        if current_time - self.last_event_time < self.debounce_seconds:
            logger.info("Ignoring camera event due to debounce")
            return

        self.last_event_time = current_time
        action = device.action
        if action == "add":
            logger.info("Camera connected, rebuilding streams")
            self._rebuild_streams()
        elif action == "remove":
            logger.info("Camera disconnected, rebuilding streams")
            self._rebuild_streams()

    def _rebuild_streams(self):
        logger.info("Rebuilding streams based on current camera state")
        try:
            self.stream_manager.stop_all()
            time.sleep(2)
            self.stream_manager.initialize()
            self.stream_manager.start_all()
            logger.info("Stream rebuilding completed")
        except Exception:
            logger.exception("Error rebuilding streams")
