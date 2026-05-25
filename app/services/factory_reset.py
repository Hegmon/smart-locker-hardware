from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from app.deployment.runtime_config import get_bool_setting, get_str_setting
from app.services.backend_state import DEFAULT_STATE_FILE, STATE_FILE, SYSTEM_STATE_FILE
from app.utils.logger import get_logger


logger = get_logger(__name__)
ProgressCallback = Callable[[str, str], None]


@dataclass(frozen=True)
class ResetResult:
    success: bool
    errors: list[str]


class FactoryResetService:
    """Safely clears application-owned state without touching source or system files."""

    def __init__(self, *, progress: ProgressCallback | None = None):
        self.progress = progress or (lambda step, status: None)
        extra_paths = get_str_setting("FACTORY_RESET_EXTRA_PATHS", "")
        self.paths = self._safe_paths(
            [
                STATE_FILE,
                SYSTEM_STATE_FILE,
                DEFAULT_STATE_FILE,
                Path("/var/lib/smartlocker/cache"),
                Path("/var/lib/smartlocker/tokens"),
                Path("/var/lib/smartlocker/queue"),
                Path("/var/lib/smartlocker/wifi_agent_state.json"),
                Path("/var/lib/smartlocker/wifi_agent_queue.json"),
                *[Path(item.strip()) for item in extra_paths.split(",") if item.strip()],
            ]
        )
        self.remove_wifi_profiles = get_bool_setting("FACTORY_RESET_REMOVE_WIFI_PROFILES", True)

    def run(self) -> ResetResult:
        errors: list[str] = []
        self._progress("started", "executing")
        logger.warning("Factory reset requested; clearing application-owned local state")

        for path in self.paths:
            try:
                self._remove_path(path)
                self._progress(f"removed:{path}", "success")
            except Exception as exc:
                message = f"{path}: {exc}"
                errors.append(message)
                logger.exception("Factory reset failed to remove %s", path)
                self._progress(f"failed:{path}", "error")

        if self.remove_wifi_profiles:
            try:
                self._clear_wifi_profiles()
                self._progress("wifi_profiles", "success")
            except Exception as exc:
                errors.append(f"wifi_profiles: {exc}")
                logger.exception("Factory reset failed to clear WiFi profiles")
                self._progress("wifi_profiles", "error")

        self._progress("completed", "success" if not errors else "error")
        return ResetResult(success=not errors, errors=errors)

    def _progress(self, step: str, status: str) -> None:
        try:
            self.progress(step, status)
        except Exception:
            logger.debug("Factory reset progress callback failed", exc_info=True)

    def _remove_path(self, path: Path) -> None:
        if not path.exists() and not path.is_symlink():
            return
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
            return
        path.unlink()

    def _clear_wifi_profiles(self) -> None:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"],
            check=False,
            capture_output=True,
            text=True,
            timeout=8.0,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "nmcli connection list failed")

        protected = {
            get_str_setting("HOTSPOT_CONNECTION", "SmartLockerHotspot"),
            get_str_setting("FACTORY_RESET_PROTECTED_WIFI_PROFILE", ""),
        }
        for line in result.stdout.splitlines():
            name, _, connection_type = line.partition(":")
            if connection_type != "802-11-wireless" or not name or name in protected:
                continue
            delete = subprocess.run(
                ["nmcli", "connection", "delete", "id", name],
                check=False,
                capture_output=True,
                text=True,
                timeout=8.0,
            )
            if delete.returncode != 0:
                raise RuntimeError(delete.stderr.strip() or f"failed to delete WiFi profile {name}")

    def _safe_paths(self, paths: list[Path]) -> list[Path]:
        allowed_roots = [
            Path("/var/lib/smartlocker"),
            Path("/etc/smartlocker"),
            Path(__file__).resolve().parents[1] / "config",
        ]
        safe: list[Path] = []
        for raw_path in paths:
            try:
                path = raw_path.expanduser().resolve(strict=False)
            except OSError:
                continue
            if any(path == root or root in path.parents for root in allowed_roots):
                safe.append(path)
            else:
                logger.warning("Ignoring unsafe factory reset path: %s", raw_path)
        return safe
