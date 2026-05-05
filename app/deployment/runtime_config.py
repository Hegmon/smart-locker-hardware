from __future__ import annotations

import json
import os
from functools import lru_cache
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ETC_DIR = Path(os.getenv("SMARTLOCKER_ETC_DIR", "/etc/smartlocker"))
DEFAULT_CONFIG_JSON = Path(
    os.getenv("SMARTLOCKER_CONFIG_JSON", str(DEFAULT_ETC_DIR / "config.json"))
)
BOOT_ENV_PATHS = (
    Path("/boot/firmware/smartlocker.env"),
    Path("/boot/smartlocker.env"),
)
DOT_ENV_PATHS = (
    DEFAULT_ETC_DIR / ".env",
    PROJECT_ROOT / ".env",
)


def _parse_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return values

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'").strip('"')
    return values


def _flatten_json(prefix: str, value: Any, output: dict[str, Any]) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            next_prefix = f"{prefix}_{key}" if prefix else str(key)
            _flatten_json(next_prefix, child, output)
        return
    if prefix:
        output[prefix.upper()] = value


@lru_cache(maxsize=1)
def load_json_config() -> dict[str, Any]:
    try:
        raw = json.loads(DEFAULT_CONFIG_JSON.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError):
        return {}

    if not isinstance(raw, dict):
        return {}

    flattened: dict[str, Any] = {}
    _flatten_json("", raw, flattened)
    return flattened


def _load_paths(paths: tuple[Path, ...]) -> dict[str, str]:
    merged: dict[str, str] = {}
    for path in paths:
        merged.update(_parse_env_file(path))
    return merged


@lru_cache(maxsize=1)
def load_boot_env_config() -> dict[str, str]:
    return _load_paths(BOOT_ENV_PATHS)


@lru_cache(maxsize=1)
def load_dot_env_config() -> dict[str, str]:
    return _load_paths(DOT_ENV_PATHS)


def get_setting(key: str, default: Any = None, *, aliases: tuple[str, ...] = ()) -> Any:
    candidates = (key, *aliases)
    boot_env = load_boot_env_config()
    json_config = load_json_config()
    dot_env = load_dot_env_config()

    for candidate in candidates:
        if candidate in os.environ:
            return os.environ[candidate]
        if candidate.upper() in os.environ:
            return os.environ[candidate.upper()]

    for candidate in candidates:
        if candidate in boot_env:
            return boot_env[candidate]
        if candidate.upper() in boot_env:
            return boot_env[candidate.upper()]

    for candidate in candidates:
        if candidate in json_config:
            return json_config[candidate]
        if candidate.upper() in json_config:
            return json_config[candidate.upper()]

    for candidate in candidates:
        if candidate in dot_env:
            return dot_env[candidate]
        if candidate.upper() in dot_env:
            return dot_env[candidate.upper()]

    return default


def get_str_setting(key: str, default: str = "", *, aliases: tuple[str, ...] = ()) -> str:
    value = get_setting(key, default, aliases=aliases)
    if value is None:
        return default
    return str(value).strip()


def get_int_setting(key: str, default: int, *, aliases: tuple[str, ...] = ()) -> int:
    value = get_setting(key, default, aliases=aliases)
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def get_float_setting(key: str, default: float, *, aliases: tuple[str, ...] = ()) -> float:
    value = get_setting(key, default, aliases=aliases)
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def get_bool_setting(key: str, default: bool = False, *, aliases: tuple[str, ...] = ()) -> bool:
    value = get_setting(key, default, aliases=aliases)
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def get_path_setting(key: str, default: str, *, aliases: tuple[str, ...] = ()) -> Path:
    return Path(get_str_setting(key, default, aliases=aliases))


def require_settings(*keys: str) -> list[str]:
    missing: list[str] = []
    for key in keys:
        if not get_str_setting(key, "").strip():
            missing.append(key)
    return missing
