from __future__ import annotations

"""MQTT command handling for inspection requests."""

from app.core.mqtt_manager import MQTTManager
from app.utils.logger import get_logger
from inspection_agent.manager import InspectionAgentManager
from inspection_agent.schemas.inspection_request import InspectionRequest
from inspection_agent.schemas.inspection_response import InspectionResult, InspectionSummary


logger = get_logger(__name__)


class InspectionSubscriber:
    """Subscribes to inspection commands and publishes structured results."""

    def __init__(self, *, mqtt_manager: MQTTManager, manager: InspectionAgentManager, device_id: str) -> None:
        self.mqtt = mqtt_manager
        self.manager = manager
        self.device_id = device_id
        self.request_topic = f"locker/{self.device_id}/inspection/request"
        self.result_topic = f"locker/{self.device_id}/inspection/result"
        self._started = False

    def start(self) -> None:
        if self._started:
            return
        self.mqtt.subscribe(self.request_topic, self._handle_message, qos=1)
        self._started = True
        logger.info("Inspection MQTT subscriber started topic=%s", self.request_topic)

    def stop(self) -> None:
        self._started = False

    def _handle_message(self, topic: str, payload: bytes) -> None:
        logger.info("Inspection request received topic=%s", topic)
        try:
            request = InspectionRequest.from_payload(self.mqtt.loads(payload))
        except Exception as exc:
            logger.warning("Invalid inspection request payload: %s", exc)
            self._publish_result(
                InspectionResult.failure(
                    request_id="",
                    device_id=self.device_id,
                    module="unknown",
                    message=str(exc),
                )
            )
            return

        if request.action == "run_test":
            self._handle_run_test(request)
            return
        if request.action == "run_all":
            self._handle_run_all(request)
            return

        self._publish_result(
            InspectionResult.failure(
                request_id=request.request_id,
                device_id=self.device_id,
                module=request.module or "unknown",
                message=f"Unsupported action: {request.action}",
            )
        )

    def _handle_run_test(self, request: InspectionRequest) -> None:
        if not request.module:
            self._publish_result(
                InspectionResult.failure(
                    request_id=request.request_id,
                    device_id=self.device_id,
                    module="unknown",
                    message="Missing module for run_test action",
                )
            )
            return
        result = self.manager.run_test(request.module, request_id=request.request_id)
        self._publish_result(result)

    def _handle_run_all(self, request: InspectionRequest) -> None:
        results, summary = self.manager.run_all_tests(request_id=request.request_id)
        for result in results:
            self._publish_result(result)
        self._publish_summary(summary)

    def _publish_result(self, result: InspectionResult) -> None:
        self.mqtt.publish_json(self.result_topic, result.to_dict(), qos=1, retain=False)

    def _publish_summary(self, summary: InspectionSummary) -> None:
        self.mqtt.publish_json(self.result_topic, summary.to_dict(), qos=1, retain=False)
