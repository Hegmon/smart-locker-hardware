from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


class WifiUploadError(RuntimeError):
    pass


@dataclass(frozen=True)
class WifiApiClient:
    endpoint_url: str
    timeout_seconds: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "_session", requests.Session())

    def send_scan(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        try:
            response = self._session.post(
                self.endpoint_url,
                json=payload,
                timeout=self.timeout_seconds,
            )
        except requests.RequestException as exc:
            raise WifiUploadError(f"WiFi upload request failed: {exc}") from exc

        if response.status_code >= 400:
            body: Any
            try:
                body = response.json()
            except ValueError:
                body = response.text
            raise WifiUploadError(
                f"WiFi upload failed with status {response.status_code}: {body}"
            )

        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return {"raw_response": response.text}
