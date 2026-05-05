from __future__ import annotations

from app.deployment.runtime_config import get_bool_setting, require_settings
from app.utils.logger import get_logger


logger = get_logger(__name__)


def validate_runtime_configuration() -> list[str]:
    missing: list[str] = []

    missing.extend(require_settings("MQTT_HOST", "MQTT_PORT"))

    if get_bool_setting("QBOX_AUTO_REGISTER", True):
        missing.extend(
            require_settings(
                "QBOX_DEVICE_REGISTRATION_URL",
                "QBOX_TELEMETRY_URL",
            )
        )

    unique_missing = sorted(set(missing))
    if unique_missing:
        logger.warning("Missing recommended configuration values: %s", ", ".join(unique_missing))
    return unique_missing
