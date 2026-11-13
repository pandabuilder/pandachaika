import logging
import os
import signal

from django.http import HttpRequest

logger = logging.getLogger(__name__)


class WebServerRestartError(Exception):
    """Raised when the web server cannot be restarted from the current process."""


def is_gunicorn_request(request: HttpRequest) -> bool:
    return request.META.get("SERVER_SOFTWARE", "").lower().startswith("gunicorn")


def restart_web_server(request: HttpRequest) -> str:
    """Restart the web server serving ``request`` and return a short description of what was done.

    Under gunicorn the current process is a worker, so the signal goes to its parent (the master):
    SIGHUP makes the master spawn fresh workers and gracefully retire the old ones, letting in-flight
    requests (including this one) finish. Views, templates and URL configuration are imported lazily
    in each worker, so they are reloaded. Modules already imported by the master at startup (models,
    ``core``) are only reloaded by restarting the whole process.

    Other backends (CherryPy) restart their engine through SIGUSR2 on the current process.
    """
    if is_gunicorn_request(request):
        if not hasattr(signal, "SIGHUP"):  # pragma: no cover - gunicorn is not available on Windows
            raise WebServerRestartError("This OS does not support signal SIGHUP.")
        master_pid = os.getppid()
        logger.info("Restarting gunicorn workers (SIGHUP to master PID %d)", master_pid)
        os.kill(master_pid, signal.SIGHUP)
        return "Gunicorn is reloading its workers."

    if hasattr(signal, "SIGUSR2"):
        logger.info("Restarting web server (SIGUSR2 to PID %d)", os.getpid())
        os.kill(os.getpid(), signal.SIGUSR2)
        return "Web server restart requested."

    raise WebServerRestartError("This OS does not support signal SIGUSR2.")
