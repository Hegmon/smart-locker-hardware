import signal
import subprocess
import threading
import time

from app.streaming_agent.logs.streaming_agent_logs import LoggingManager


logger = LoggingManager.get_logger(__name__)


class StreamProcess:
    """Manage the lifecycle of one ffmpeg streaming process."""

    def __init__(self, name, ffmpeg_command):
        self.name = name
        self.ffmpeg_command = ffmpeg_command
        self.rtsp_url = ffmpeg_command[-1] if ffmpeg_command else None
        self.process = None
        self.running = False
        self.restart_attempts = 0
        self.max_restart_attempts = 3
        self.lock = threading.Lock()
        self._stop_requested = False

    def start(self):
        with self.lock:
            if self.running:
                logger.info("%s is already running", self.name)
                return

            try:
                self._stop_requested = False
                logger.info("Starting %s with command: %s", self.name, " ".join(self.ffmpeg_command))
                self.process = subprocess.Popen(
                    self.ffmpeg_command,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                self.running = True
                threading.Thread(target=self._monitor, daemon=True, name=f"{self.name}-monitor").start()
            except Exception:
                logger.exception("Failed to start %s", self.name)
                self.running = False
                self.process = None

    def stop(self):
        with self.lock:
            if not self.running or self.process is None:
                logger.info("%s is not running", self.name)
                self.running = False
                self.process = None
                return

            try:
                self._stop_requested = True
                logger.info("Stopping %s", self.name)
                self.process.send_signal(signal.SIGTERM)
                self.process.wait(timeout=5)
                logger.info("%s stopped successfully", self.name)
            except subprocess.TimeoutExpired:
                logger.warning("%s did not stop gracefully, killing it", self.name)
                self.process.kill()
                self.process.wait(timeout=5)
            except Exception:
                logger.exception("Error stopping %s", self.name)
            finally:
                self.running = False
                self.process = None

    def restart(self):
        logger.warning("Restarting %s", self.name)
        self.stop()
        time.sleep(2)

        if self.restart_attempts < self.max_restart_attempts:
            self.restart_attempts += 1
            self.start()
        else:
            logger.error("Max restart attempts reached for %s", self.name)
            self.restart_attempts = 0

    def _monitor(self):
        while self.running and self.process is not None:
            status = self.process.poll()
            if status is not None:
                stderr_output = ""
                if self.process.stderr:
                    try:
                        stderr_output = self.process.stderr.read().strip()
                    except Exception:
                        stderr_output = ""

                self.running = False
                self.process = None

                if self._stop_requested:
                    logger.info("%s exited after stop request with status %s", self.name, status)
                    break

                if stderr_output:
                    logger.error("%s stopped unexpectedly with status %s: %s", self.name, status, stderr_output)
                else:
                    logger.error("%s stopped unexpectedly with status %s", self.name, status)
                self.restart()
                break

            time.sleep(5)

    def is_running(self):
        return self.running and self.process is not None and self.process.poll() is None
