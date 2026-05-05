from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable


StatusProvider = Callable[[], dict[str, Any]]


class AgentHealthServer:
    def __init__(self, host: str, port: int, status_provider: StatusProvider):
        self.host = host
        self.port = port
        self._status_provider = status_provider
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._server is not None:
            return

        status_provider = self._status_provider

        class HealthHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                if self.path not in {"/", "/health"}:
                    self.send_response(404)
                    self.end_headers()
                    return

                payload = json.dumps(status_provider()).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format: str, *args: object) -> None:
                return

        self._server = ThreadingHTTPServer((self.host, self.port), HealthHandler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            daemon=True,
            name=f"health-{self.port}",
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is None:
            return
        self._server.shutdown()
        self._server.server_close()
        self._server = None
        self._thread = None
