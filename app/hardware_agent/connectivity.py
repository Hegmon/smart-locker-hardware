from __future__ import annotations

import socket
import subprocess
import time
import urllib.request
from dataclasses import dataclass
from enum import Enum

from app.utils.logger import get_logger


logger = get_logger(__name__)


class ConnectivityMethod(str, Enum):
    DNS = "dns"
    PING = "ping"
    HTTP = "http"


@dataclass(frozen=True)
class ConnectivityConfig:
    method: str = ConnectivityMethod.DNS.value
    timeout_seconds: float = 5.0
    retries: int = 2
    retry_delay_seconds: float = 1.0
    dns_host: str = "one.one.one.one"
    ping_host: str = "1.1.1.1"
    http_url: str = "https://connectivitycheck.gstatic.com/generate_204"


class InternetConnectivityChecker:
    def __init__(self, config: ConnectivityConfig) -> None:
        self.config = config

    def is_online(self) -> bool:
        for attempt in range(1, max(1, self.config.retries) + 1):
            if self._check_once():
                logger.info("Internet connectivity verified via %s", self.config.method)
                return True
            logger.info(
                "Internet connectivity check failed via %s attempt %d/%d",
                self.config.method,
                attempt,
                max(1, self.config.retries),
            )
            if attempt < self.config.retries:
                time.sleep(max(0.1, self.config.retry_delay_seconds))
        return False

    def _check_once(self) -> bool:
        method = (self.config.method or ConnectivityMethod.DNS.value).lower()
        if method == ConnectivityMethod.HTTP.value:
            return self._http_check()
        if method == ConnectivityMethod.PING.value:
            return self._ping_check()
        return self._dns_check()

    def _dns_check(self) -> bool:
        previous_timeout = socket.getdefaulttimeout()
        try:
            socket.setdefaulttimeout(self.config.timeout_seconds)
            socket.getaddrinfo(
                self.config.dns_host,
                443,
                proto=socket.IPPROTO_TCP,
            )
            return True
        except OSError as exc:
            logger.debug("DNS connectivity check failed: %s", exc)
            return False
        finally:
            socket.setdefaulttimeout(previous_timeout)

    def _ping_check(self) -> bool:
        try:
            result = subprocess.run(
                [
                    "ping",
                    "-c",
                    "1",
                    "-W",
                    str(max(1, int(self.config.timeout_seconds))),
                    self.config.ping_host,
                ],
                capture_output=True,
                text=True,
                timeout=max(1.0, self.config.timeout_seconds + 1),
                check=False,
            )
            return result.returncode == 0
        except Exception as exc:
            logger.debug("Ping connectivity check failed: %s", exc)
            return False

    def _http_check(self) -> bool:
        try:
            request = urllib.request.Request(
                self.config.http_url,
                headers={"User-Agent": "smartlocker-connectivity-check/1.0"},
            )
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                return 200 <= response.status < 400
        except Exception as exc:
            logger.debug("HTTP connectivity check failed: %s", exc)
            return False
