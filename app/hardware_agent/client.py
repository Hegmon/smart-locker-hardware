from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests


class WifiApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class WifiApiClient:
    timeout_seconds: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "_session", requests.Session())

    def post_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        try:
            response = self._session.post(url, json=payload, timeout=self.timeout_seconds)
        except requests.RequestException as exc:
            raise WifiApiError(f"POST {url} failed: {exc}") from exc
        return self._handle_response(response, f"POST {url}")

    def get_json(self, url: str) -> dict[str, Any] | None:
        try:
            response = self._session.get(url, timeout=self.timeout_seconds)
        except requests.RequestException as exc:
            raise WifiApiError(f"GET {url} failed: {exc}") from exc
        return self._handle_response(response, f"GET {url}")

    def patch_json(self, url: str, payload: dict[str, Any]) -> dict[str, Any] | None:
        try:
            response = self._session.patch(url, json=payload, timeout=self.timeout_seconds)
        except requests.RequestException as exc:
            raise WifiApiError(f"PATCH {url} failed: {exc}") from exc
        return self._handle_response(response, f"PATCH {url}")

    @staticmethod
    def _handle_response(response: requests.Response, action: str) -> dict[str, Any] | None:
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            body: Any
            try:
                body = response.json()
            except ValueError:
                body = response.text
            raise WifiApiError(f"{action} returned {response.status_code}: {body}")

        if not response.content:
            return None
        try:
            parsed = response.json()
        except ValueError:
            return {"raw_response": response.text}
        if isinstance(parsed, dict):
            return parsed
        return {"data": parsed}
