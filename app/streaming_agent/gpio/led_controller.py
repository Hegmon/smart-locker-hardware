from app.streaming_agent.gpio.relay_controller import RelayController
from app.streaming_agent.logs.streaming_agent_logs import LoggingManager


logger = LoggingManager.get_logger(__name__)


class LedController(RelayController):
    """Compatibility adapter for older detection code.

    New streaming-agent hardware control should use RelayController directly.
    Detection activity now maps to the red LED and buzzer relays.
    """

    def set_active(self, source, active):
        source = str(source or "detection")
        self._set_red_source(source, active)
        self._set_buzzer_source(source, active)

    def set_person_visible(self, visible):
        super().set_person_visible(visible)

    def set_tamper_active(self, camera_role, active):
        super().set_tamper_active(camera_role, active)
