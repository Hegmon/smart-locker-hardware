import threading
import time

from app.streaming_agent.logs.streaming_agent_logs import LoggingManager


logger = LoggingManager.get_logger(__name__)


class StreamWatchdog:
    def __init__(self, stream_registry, check_interval=5, restart_cooldown=3):
        self.stream_registry = stream_registry
        self.check_interval = check_interval
        self.restart_cooldown = restart_cooldown
        self.running = False
        self.thread = None
        self.lock = threading.Lock()

    def start(self):
        with self.lock:
            if self.running:
                logger.info("Watchdog is already running")
                return

            logger.info("Starting stream watchdog")
            self.running = True
            self.thread = threading.Thread(target=self._monitor_loop, daemon=True, name="stream-watchdog")
            self.thread.start()

    def stop(self):
        with self.lock:
            if not self.running:
                logger.info("Watchdog is not running")
                return

            logger.info("Stopping stream watchdog")
            self.running = False

        if self.thread:
            self.thread.join(timeout=self.check_interval + 1)
            self.thread = None
        logger.info("Watchdog stopped")

    def _monitor_loop(self):
        while self.running:
            for stream_name, stream in self.stream_registry.items():
                if not self.running:
                    break

                try:
                    if stream.is_running():
                        logger.info("Stream %s is running", stream_name)
                    else:
                        logger.warning("Stream %s is not running, attempting restart", stream_name)
                        self._restart_stream(stream_name, stream)
                except Exception:
                    logger.exception("Error checking stream %s status", stream_name)

            time.sleep(self.check_interval)

    def _restart_stream(self, stream_name, stream):
        logger.warning("Attempting to restart stream %s", stream_name)
        try:
            stream.restart()
            logger.info("Restart command sent for stream %s", stream_name)
        except Exception:
            logger.exception("Failed restarting stream %s", stream_name)
        time.sleep(self.restart_cooldown)
