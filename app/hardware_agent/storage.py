from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


@dataclass
class QueueItem:
    payload: dict[str, Any]
    retry_count: int = 0


class JsonFileStorage:
    def __init__(self, state_file: Path, queue_file: Path) -> None:
        self.state_file = state_file
        self.queue_file = queue_file

    def load_state(self) -> dict[str, Any]:
        return self._read_json(self.state_file, default={})

    def save_state(self, state: dict[str, Any]) -> None:
        self._write_json(self.state_file, state)

    def load_queue(self) -> list[QueueItem]:
        raw_queue = self._read_json(self.queue_file, default=[])
        if not isinstance(raw_queue, list):
            return []
        queue: list[QueueItem] = []
        for item in raw_queue:
            if not isinstance(item, dict) or "payload" not in item:
                continue
            payload = item["payload"]
            retry_count = item.get("retry_count", 0)
            if isinstance(payload, dict):
                queue.append(QueueItem(payload=payload, retry_count=int(retry_count)))
        return queue

    def save_queue(self, queue: list[QueueItem]) -> None:
        serializable = [
            {"payload": item.payload, "retry_count": item.retry_count}
            for item in queue
        ]
        self._write_json(self.queue_file, serializable)

    def append_queue_item(self, queue_item: QueueItem) -> None:
        queue = self.load_queue()
        queue.append(queue_item)
        self.save_queue(queue)

    def _read_json(self, path: Path, default: Any) -> Any:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return default
        except json.JSONDecodeError:
            return default

    def _write_json(self, path: Path, payload: Any) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
        ) as temp_file:
            json.dump(payload, temp_file, indent=2, sort_keys=True)
            temp_file.flush()
            os.fsync(temp_file.fileno())
            temp_path = Path(temp_file.name)
        temp_path.replace(path)
