from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional

import requests


class WifiApiError(RuntimeError):
    pass


@dataclass
class WifiApiClient:
    """
    Production-ready client:
    - MQTT is PRIMARY (handled outside this class)
    - HTTP is FALLBACK only
    - Safe for 100+ devices
    """

    base_url: str
    timeout_seconds: float

    def __post_init__(self) -> None:
        self._session = requests.Session()

    # ---------------- HTTP FALLBACK ----------------
    def post_json(self, url: str, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
        return self._request("POST", url, payload)

    def get_json(self, url: str) -> Optional[dict[str, Any]]:
        return self._request("GET", url)

    def patch_json(self, url: str, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
        return self._request("PATCH", url, payload)

    # ---------------- INTERNAL ----------------
    def _request(
        self,
        method: str,
        url: str,
        payload: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        try:
            if method == "POST":
                response = self._session.post(url, json=payload, timeout=self.timeout_seconds)

            elif method == "GET":
                response = self._session.get(url, timeout=self.timeout_seconds)

            elif method == "PATCH":
                response = self._session.patch(url, json=payload, timeout=self.timeout_seconds)

            else:
                raise WifiApiError(f"Unsupported method: {method}")

        except requests.RequestException as exc:
            raise WifiApiError(f"{method} {url} failed: {exc}") from exc

        return self._handle_response(response, f"{method} {url}")

    @staticmethod
    def _handle_response(response: requests.Response, action: str) -> Optional[dict[str, Any]]:
        # ---------------- SAFE ERROR HANDLING ----------------
        if response.status_code == 404:
            return None

        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = response.text

            raise WifiApiError(f"{action} returned {response.status_code}: {body}")

        # ---------------- EMPTY RESPONSE ----------------
        if not response.content:
            return None

        # ---------------- SAFE JSON PARSE ----------------
        try:
            parsed = response.json()
        except ValueError:
            return {"raw_response": response.text}

        if isinstance(parsed, dict):
            return parsed

        return {"data": parsed}

    # ---------------- HEALTH CHECK ----------------
    def is_backend_alive(self) -> bool:
        """
        Optional health check (used for fallback logic)
        """
        try:
            response = self._session.get(self.base_url, timeout=3)
            return response.status_code < 500
        except Exception:
            return False