from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any


# =========================================================
# QUEUE ITEM
# =========================================================
@dataclass
class QueueItem:
    kind: str
    payload: dict[str, Any]
    retry_count: int = 0

    def key(self) -> str:
        """
        Unique identity for deduplication (important for MQTT retries)
        """
        return f"{self.kind}:{hash(json.dumps(self.payload, sort_keys=True))}"


# =========================================================
# THREAD SAFE FILE STORAGE
# =========================================================
class JsonFileStorage:
    def __init__(self, state_file: Path, queue_file: Path) -> None:
        self.state_file = state_file
        self.queue_file = queue_file

        # Prevent race conditions in agent loop
        self._lock = threading.Lock()

        # Prevent infinite queue growth (important at scale)
        self.max_queue_size = 500

    # =========================================================
    # STATE MANAGEMENT
    # =========================================================
    def load_state(self) -> dict[str, Any]:
        with self._lock:
            return self._read_json(self.state_file, default={})

    def save_state(self, state: dict[str, Any]) -> None:
        with self._lock:
            self._write_json_atomic(self.state_file, state)

    # =========================================================
    # QUEUE MANAGEMENT (MQTT OFFLINE BUFFER)
    # =========================================================
    def load_queue(self) -> list[QueueItem]:
        with self._lock:
            raw = self._read_json(self.queue_file, default=[])

        if not isinstance(raw, list):
            return []

        queue: list[QueueItem] = []

        for item in raw:
            if not isinstance(item, dict):
                continue

            queue.append(
                QueueItem(
                    kind=str(item.get("kind", "unknown")),
                    payload=item.get("payload", {}),
                    retry_count=self._safe_int(item.get("retry_count"), 0),
                )
            )

        return queue

    def save_queue(self, queue: list[QueueItem]) -> None:
        with self._lock:
            # enforce size limit (prevents disk crash on 100+ devices)
            queue = queue[-self.max_queue_size :]

            self._write_json_atomic(
                self.queue_file,
                [
                    {
                        "kind": q.kind,
                        "payload": q.payload,
                        "retry_count": q.retry_count,
                    }
                    for q in queue
                ],
            )

    def append_queue_item(self, item: QueueItem) -> None:
        with self._lock:
            queue = self.load_queue()

            # ---------------------------
            # DEDUPLICATION (CRITICAL FOR MQTT)
            # ---------------------------
            existing_keys = {q.key() for q in queue}

            if item.key() in existing_keys:
                return  # avoid duplicate retry spam

            queue.append(item)

            self.save_queue(queue)

    # =========================================================
    # SAFE JSON IO
    # =========================================================
    def _read_json(self, path: Path, default: Any) -> Any:
        try:
            if not path.exists():
                return default

            with path.open("r", encoding="utf-8") as f:
                return json.load(f)

        except (json.JSONDecodeError, OSError):
            return default

    def _write_json_atomic(self, path: Path, payload: Any) -> None:
        """
        Atomic write (prevents corruption on power failure)
        """

        path.parent.mkdir(parents=True, exist_ok=True)

        tmp_path = None

        try:
            with NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=str(path.parent),
                delete=False,
            ) as f:
                json.dump(payload, f, indent=2, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
                tmp_path = Path(f.name)

            tmp_path.replace(path)

        except PermissionError as e:
            raise PermissionError(
                f"Cannot write to {path}. Fix systemd permissions."
            ) from e

        finally:
            if tmp_path and tmp_path.exists():
                try:
                    tmp_path.unlink()
                except Exception:
                    pass

    # =========================================================
    # HELPERS
    # =========================================================
    @staticmethod
    def _safe_int(v: Any, default: int) -> int:
        try:
            return int(v)
        except Exception:
            return default