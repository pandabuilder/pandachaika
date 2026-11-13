import logging
import threading
import time
from collections.abc import Callable

logger = logging.getLogger(__name__)


class TaskCancelledError(Exception):
    """Raised when an active task is cancelled (or timed out) while running."""


def ping_activity(activity_cb: Callable[[], None] | None) -> None:
    """Call a worker activity callback.

    The callback reports progress to the stall watchdog, and raises ``TaskCancelledError`` when the task was
    cancelled or timed out. Any other error from it is not worth failing the work for, but cancellation must reach
    the running code.
    """
    if not callable(activity_cb):
        return
    try:
        activity_cb()
    except TaskCancelledError:
        raise
    except Exception:
        logger.debug("Activity callback failed", exc_info=True)


_task_hooks = threading.local()


def set_task_hooks(
    activity_cb: Callable[[], None] | None = None, is_cancelled: Callable[[], bool] | None = None
) -> None:
    """Register the activity and cancellation callbacks of the task running in this thread."""
    _task_hooks.activity = activity_cb
    _task_hooks.is_cancelled = is_cancelled


def clear_task_hooks() -> None:
    set_task_hooks(None, None)


def interruptible_sleep(seconds: float, step: float = 1.0) -> None:
    """Sleep, but stop with ``TaskCancelledError`` if the task of this thread is cancelled.

    Reports activity while waiting, because waiting out a rate limit or ban is intentional and must not look
    like a stalled task. Outside of a task this is a plain ``time.sleep``.
    """
    activity_cb = getattr(_task_hooks, "activity", None)
    is_cancelled = getattr(_task_hooks, "is_cancelled", None)
    if activity_cb is None and is_cancelled is None:
        time.sleep(seconds)
        return
    remaining = max(0.0, float(seconds))
    while remaining > 0:
        if callable(is_cancelled) and is_cancelled():
            raise TaskCancelledError("Task cancelled while waiting")
        ping_activity(activity_cb)
        chunk = min(step, remaining)
        time.sleep(chunk)
        remaining -= chunk
