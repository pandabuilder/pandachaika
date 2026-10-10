# -*- coding: utf-8 -*-
import logging
from collections.abc import Callable, Iterable
from typing import Any, Optional

from core.base.setup import Settings
from core.base.types import QueueItem

logger = logging.getLogger(__name__)


class WebQueue(object):
    """Queue handler for web downloads backed by database TaskQueue in separate worker processes."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.thread_name = "web_queue"

    def is_running(self) -> bool:
        """Check if any background worker process is active."""
        try:
            from workers.models import WorkerProcess
            return WorkerProcess.is_any_worker_running(threshold_seconds=30)
        except Exception:
            return False

    def queue_size(self) -> int:
        """Return the number of pending tasks in the queue."""
        try:
            from workers.client import queue_size
            return queue_size()
        except Exception:
            return 0

    @property
    def current_processing_items(self) -> Any:
        """Return currently processing task for stats display."""
        try:
            from workers.client import get_processing_task
            return get_processing_task()
        except Exception:
            return None

    @property
    def queue(self) -> Any:
        """Return list of pending tasks for stats display."""
        try:
            from workers.client import get_pending_tasks
            return list(get_pending_tasks())
        except Exception:
            return []

    def remove_by_index(self, index: int) -> bool:
        """Remove/cancel a pending task by its index."""
        try:
            from workers.client import remove_by_index
            return remove_by_index(index)
        except Exception:
            return False

    def enqueue_args_list(
        self,
        args: Iterable[str],
        override_options: "Optional[Settings]" = None,
        archive_callback: "Optional[Callable[[Optional[Any], Optional[str], str], None]]" = None,
        gallery_callback: "Optional[Callable[[Optional[Any], Optional[str], str], None]]" = None,
        use_argparser: bool = True,
        user: Any = None,
        reason: Optional[str] = None,
        event_action: Optional[str] = None,
        priority: int = 10,
    ) -> None:
        """Enqueue crawler task to the database task queue for processing by the worker process.

        Callbacks are optional and deprecated in favor of user, reason, and event_action arguments.
        """
        from workers.client import enqueue

        task_user = user
        if not task_user and override_options and getattr(override_options, "archive_user", None):
            task_user = override_options.archive_user

        task_reason = reason
        if not task_reason and override_options:
            task_reason = getattr(override_options, "archive_reason", "") or getattr(override_options, "gallery_reason", "")

        task_action = event_action
        if not task_action and override_options:
            task_action = getattr(override_options, "event_action", "")

        enqueue(
            args=args,
            task_type="web_crawler",
            override_options=override_options,
            user=task_user,
            reason=task_reason,
            event_action=task_action,
            priority=priority,
        )

    def start_running(self) -> None:
        """No-op in separate process mode since tasks are processed by the worker daemon."""
        pass
