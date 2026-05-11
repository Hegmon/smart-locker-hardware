import threading
import time

import psutil

from app.streaming_agent.logs.streaming_agent_logs import LoggingManager


logger = LoggingManager.get_logger(__name__)


class HealthMonitor:
    """
    Track:
    - CPU usage
    - RAM usage
    - process health
    - stream uptime
    """

    def __init__(self, stream_registry, interval=5):
        self.stream_registry = stream_registry
        self.interval = interval
        self.running = False
        self.thread = None
        self.metrics = {}
        self.lock = threading.Lock()

    def start(self):
        with self.lock:
            if self.running:
                logger.info("Health monitor is already running")
                return

            logger.info("Starting health monitor")
            self.running = True
            self.thread = threading.Thread(target=self._monitor_loop, daemon=True, name="health-monitor")
            self.thread.start()
            logger.info("Health monitor started successfully")

    def stop(self):
        with self.lock:
            if not self.running:
                logger.info("Health monitor is not running")
                return

            logger.info("Stopping health monitor")
            self.running = False

        if self.thread:
            self.thread.join(timeout=self.interval + 1)
            self.thread = None
        logger.info("Health monitor stopped successfully")

    def _monitor_loop(self):
        while self.running:
            try:
                self._collect_system_metrics()
                self._collect_stream_metrics()
                logger.info("Health metrics updated")
            except Exception:
                logger.exception("Health monitoring error")

            time.sleep(self.interval)

    def _collect_system_metrics(self):
        cpu_percent = psutil.cpu_percent()
        ram = psutil.virtual_memory()
        self.metrics["system"] = {
            "cpu_percent": cpu_percent,
            "ram_percent": ram.percent,
            "ram_used_mb": round(ram.used / 1024 / 1024, 2),
            "ram_total_mb": round(ram.total / 1024 / 1024, 2),
        }

    def _collect_stream_metrics(self):
        stream_data = {}
        for stream_name, stream in self.stream_registry.items():
            is_running = stream.is_running()
            process_id = stream.process.pid if stream.process and stream.process.pid else None
            uptime = None

            if process_id:
                try:
                    process = psutil.Process(process_id)
                    uptime = round(time.time() - process.create_time(), 2)
                except Exception:
                    uptime = None

            stream_data[stream_name] = {
                "running": is_running,
                "pid": process_id,
                "uptime_seconds": uptime,
                "restart_count": stream.restart_attempts,
                "rtsp_url": stream.rtsp_url,
            }

        self.metrics["streams"] = stream_data

    def get_metrics(self):
        return self.metrics
