"""Background task manager with SSE streaming."""

import threading
import uuid
from dataclasses import dataclass, field
from queue import Queue, Empty
from typing import Any, Generator


@dataclass
class TaskInfo:
    id: str
    operation: str
    status: str = "pending"  # pending, running, completed, failed
    progress: float = 0.0
    message: str = ""
    result: Any = None
    error: str = ""
    events: Queue = field(default_factory=Queue)


class TaskManager:
    """Manages background tasks with SSE progress streaming."""

    def __init__(self):
        self._tasks: dict[str, TaskInfo] = {}
        self._lock = threading.Lock()

    def create(self, operation: str) -> str:
        """Create a new task. Returns task_id."""
        task_id = str(uuid.uuid4())[:8]
        task = TaskInfo(id=task_id, operation=operation)
        with self._lock:
            self._tasks[task_id] = task
        return task_id

    def run_in_background(self, task_id: str, fn, *args, **kwargs) -> None:
        """Run a function in a daemon thread, updating task status."""
        task = self.get(task_id)
        if not task:
            return

        def _run():
            task.status = "running"
            task.events.put({"event": "status", "data": "running"})
            try:
                result = fn(*args, **kwargs)
                self.complete(task_id, result)
            except Exception as e:
                self.fail(task_id, str(e))

        thread = threading.Thread(target=_run, daemon=True)
        thread.start()

    def update_progress(self, task_id: str, pct: float, msg: str) -> None:
        """Push a progress update."""
        task = self.get(task_id)
        if not task:
            return
        task.progress = pct
        task.message = msg
        task.events.put({"event": "progress", "data": {"pct": pct, "msg": msg}})

    def complete(self, task_id: str, result: Any) -> None:
        """Mark task as completed."""
        task = self.get(task_id)
        if not task:
            return
        task.status = "completed"
        task.progress = 1.0
        task.result = result
        task.events.put({"event": "complete", "data": result})

    def fail(self, task_id: str, error: str) -> None:
        """Mark task as failed."""
        task = self.get(task_id)
        if not task:
            return
        task.status = "failed"
        task.error = error
        task.events.put({"event": "error", "data": error})

    def get(self, task_id: str) -> TaskInfo | None:
        with self._lock:
            return self._tasks.get(task_id)

    def stream_events(self, task_id: str) -> Generator[str, None, None]:
        """Yield SSE-formatted event strings."""
        task = self.get(task_id)
        if not task:
            yield f"event: error\ndata: {{\"msg\": \"Task not found\"}}\n\n"
            return

        import json

        while True:
            try:
                event = task.events.get(timeout=30)
            except Empty:
                # Send keepalive
                yield ": keepalive\n\n"
                continue

            event_type = event["event"]
            data = event["data"]

            if isinstance(data, dict):
                yield f"event: {event_type}\ndata: {json.dumps(data)}\n\n"
            elif isinstance(data, str):
                yield f"event: {event_type}\ndata: {json.dumps({'msg': data})}\n\n"
            else:
                # Serialize dataclass-like results
                try:
                    from dataclasses import asdict
                    yield f"event: {event_type}\ndata: {json.dumps(asdict(data))}\n\n"
                except (TypeError, Exception):
                    yield f"event: {event_type}\ndata: {json.dumps({'msg': str(data)})}\n\n"

            if event_type in ("complete", "error"):
                break
