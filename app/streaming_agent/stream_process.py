import signal
import subprocess
import threading
import time

from app.streaming_agent.logs.streaming_agent_logs import LoggingManager


logger = LoggingManager.get_logger(__name__)


class StreamProcess:
    """Manage the lifecycle of one ffmpeg streaming process."""

    def __init__(self, name, ffmpeg_command, frame_buffer=None):
        self.name = name
        self.ffmpeg_command = ffmpeg_command
        self.rtsp_url = ffmpeg_command[-1] if ffmpeg_command else None
        if ffmpeg_command and "rtsp" in ffmpeg_command:
            rtsp_index = len(ffmpeg_command) - 1 - ffmpeg_command[::-1].index("rtsp")
            if rtsp_index + 1 < len(ffmpeg_command):
                self.rtsp_url = ffmpeg_command[rtsp_index + 1]
        self.frame_buffer = frame_buffer
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
                    stdout=subprocess.PIPE if self.frame_buffer else subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                self.running = True
                if self.frame_buffer:
                    threading.Thread(target=self._read_frames, daemon=True, name=f"{self.name}-frames").start()
                threading.Thread(target=self._drain_stderr, daemon=True, name=f"{self.name}-stderr").start()
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

    def _read_frames(self):
        if not self.frame_buffer or not self.process or not self.process.stdout:
            return

        frame_size = self.frame_buffer.frame_size
        while self.running and self.process is not None:
            try:
                frame = self.process.stdout.buffer.read(frame_size)
            except Exception:
                logger.exception("Frame reader failed for %s", self.name)
                return

            if len(frame) != frame_size:
                return
            self.frame_buffer.update(frame)

    def _drain_stderr(self):
        if not self.process or not self.process.stderr:
            return
        while self.running and self.process is not None:
            try:
                line = self.process.stderr.readline()
            except Exception:
                return
            if not line:
                return
            logger.warning("%s ffmpeg: %s", self.name, line.strip())

    def is_running(self):
        return self.running and self.process is not None and self.process.poll() is None
