#!/usr/bin/env python3.14
# -*- coding: utf-8 -*-
import argparse
import os
import os.path
import sys
import typing
from urllib.parse import urlparse
from urllib.request import url2pathname

import cherrypy
from cherrypy.lib import static
from cherrypy.process import plugins
from cherrypy.process.wspbus import Bus

try:
    from gunicorn.app.base import BaseApplication
except ImportError:
    BaseApplication = None  # type: ignore[assignment,misc]

from httplogger import HTTPLogger

sys.path.append(os.path.dirname(__file__))

from core.base.setup import Settings

__all__ = ["DjangoAppPlugin", "MediaAndStaticFilesHandler", "GunicornApplication", "run_cherrypy", "run_gunicorn"]


class DjangoAppPlugin(plugins.SimplePlugin):

    def __init__(
        self,
        bus: Bus,
        settings_module: str = "settings",
        wsgi_http_logger: type = HTTPLogger,
        local_settings: Settings | None = None,
    ) -> None:
        """CherryPy engine plugin to configure and mount
        the Django application onto the CherryPy server.
        """
        plugins.SimplePlugin.__init__(self, bus)
        self.settings_module = settings_module
        self.wsgi_http_logger = wsgi_http_logger
        if local_settings:
            self.crawler_settings = local_settings
        else:
            self.crawler_settings = Settings(load_from_disk=True)

    def start(self) -> None:
        """When the bus starts, the plugin is also started,
        and we load the Django application. We then mount it on
        the CherryPy engine for serving as a WSGI application.
        We let CherryPy serve the application's static files.
        """
        from pandabackup import settings

        settings.crawler_settings = self.crawler_settings
        settings.CRAWLER_SETTINGS = self.crawler_settings

        os.environ["DJANGO_SETTINGS_MODULE"] = self.settings_module

        from django.core.wsgi import get_wsgi_application

        def corsstaticdir(
            section: str,
            dir: str,
            root: str = "",
            match: str = "",
            content_types: None = None,
            index: str = "",
            debug: bool = False,
        ) -> bool:
            cherrypy.response.headers["Access-Control-Allow-Origin"] = "*"
            return static.staticdir(section, dir, root, match, content_types, index, debug)

        cherrypy.tree.graft(self.wsgi_http_logger(get_wsgi_application(), self.crawler_settings))

        settings.WORKERS.start_workers(self.crawler_settings)
        self.crawler_settings.create_missing_directories()

        tool = cherrypy._cptools.HandlerTool(corsstaticdir)
        static_handler = tool.handler(
            section="/",
            dir=os.path.split(settings.STATIC_ROOT)[1],
            root=os.path.abspath(os.path.split(settings.STATIC_ROOT)[0]),
        )
        cherrypy.tree.mount(static_handler, settings.STATIC_URL)

        media_handler = tool.handler(
            section="/",
            dir=os.path.split(settings.MEDIA_ROOT)[1],
            root=os.path.abspath(os.path.split(settings.MEDIA_ROOT)[0]),
        )
        cherrypy.tree.mount(media_handler, settings.MEDIA_URL)


from django.contrib.staticfiles.handlers import StaticFilesHandler


class MediaAndStaticFilesHandler(StaticFilesHandler):
    """WSGI handler that serves both static and media files with CORS headers,
    falling back to the main Django WSGI application.
    """

    def __init__(self, application: typing.Any) -> None:
        super().__init__(application)
        from pandabackup import settings

        self.media_url = urlparse(getattr(settings, "MEDIA_URL", "/media/"))
        self.media_root = getattr(settings, "MEDIA_ROOT", "")

    def _should_handle_media(self, path: str) -> bool:
        return bool(path.startswith(self.media_url.path) and not self.media_url.netloc)

    def serve_media(self, request: typing.Any) -> typing.Any:
        from django.views.static import serve

        rel_path = url2pathname(request.path.removeprefix(self.media_url.path))
        return serve(request, rel_path, document_root=self.media_root, show_indexes=False)

    def get_response(self, request: typing.Any) -> typing.Any:
        from django.http import Http404
        from django.core.handlers.exception import response_for_exception

        if self._should_handle_media(request.path_info):
            try:
                resp = self.serve_media(request)
                resp["Access-Control-Allow-Origin"] = "*"
                return resp
            except Http404 as e:
                return response_for_exception(request, e)
        resp = super().get_response(request)
        resp["Access-Control-Allow-Origin"] = "*"
        return resp

    def __call__(self, environ: dict[str, typing.Any], start_response: typing.Callable) -> typing.Any:
        from django.core.handlers.wsgi import get_path_info

        path = get_path_info(environ)
        if not self._should_handle(path) and not self._should_handle_media(path):
            return self.application(environ, start_response)
        return super(StaticFilesHandler, self).__call__(environ, start_response)


if BaseApplication is not None:

    class GunicornApplication(BaseApplication):
        """Custom Gunicorn Application instance configured from dictionary options."""

        def __init__(self, app: typing.Any, options: dict[str, typing.Any] | None = None) -> None:
            self.options = options or {}
            self.application = app
            super().__init__()

        def load_config(self) -> None:
            for key, value in self.options.items():
                if key in self.cfg.settings and value is not None:
                    self.cfg.set(key.lower(), value)

        def load(self) -> typing.Any:
            return self.application

else:

    class GunicornApplication:  # type: ignore[no-redef]

        def __init__(self, *args: typing.Any, **kwargs: typing.Any) -> None:
            raise RuntimeError("Gunicorn is not installed.")


def run_gunicorn(crawler_settings: Settings, args: argparse.Namespace) -> None:
    if BaseApplication is None:
        raise RuntimeError("Gunicorn is not installed. Please install gunicorn or choose cherrypy backend.")

    os.environ["DJANGO_SETTINGS_MODULE"] = "pandabackup.settings"

    from pandabackup import settings

    settings.crawler_settings = crawler_settings
    settings.CRAWLER_SETTINGS = crawler_settings

    from django.core.wsgi import get_wsgi_application

    wsgi_app = get_wsgi_application()
    settings.WORKERS.start_workers(crawler_settings)
    crawler_settings.create_missing_directories()

    app = MediaAndStaticFilesHandler(wsgi_app)

    port = args.port or crawler_settings.webserver.bind_port
    socket_file = args.socket_file or crawler_settings.webserver.socket_file
    bind_address = crawler_settings.webserver.bind_address

    if socket_file:
        bind_target = f"unix:{socket_file}"
        listening_on_message = f"socket file {socket_file}"
    else:
        bind_target = f"{bind_address}:{port}"
        listening_on_message = f"{bind_address}:{port}"

    socket_queue_size = args.socket_queue_size or crawler_settings.webserver.socket_queue_size
    workers = args.workers or crawler_settings.webserver.workers
    threads = args.threads or crawler_settings.webserver.threads
    timeout = getattr(args, "timeout", None) or crawler_settings.webserver.timeout

    def when_ready(server: typing.Any) -> None:
        if socket_file and os.path.exists(socket_file):
            try:
                os.chmod(socket_file, 0o777)
            except OSError:
                pass

    def post_fork(server: typing.Any, worker: typing.Any) -> None:
        from django.db import connections

        connections.close_all()

    log_base_dir = os.path.dirname(crawler_settings.log_location) if crawler_settings.log_location else ""

    access_log = None
    if crawler_settings.webserver.write_access_log:
        access_log = os.path.join(log_base_dir, "access.log") if log_base_dir else "access.log"
    elif crawler_settings.webserver.log_to_screen:
        access_log = "-"

    error_log = "-"
    if not crawler_settings.webserver.log_to_screen and log_base_dir:
        error_log = os.path.join(log_base_dir, "error.log")

    log_level = "info"
    if crawler_settings.log_level and crawler_settings.log_level.lower() in (
        "debug",
        "info",
        "warning",
        "error",
        "critical",
    ):
        log_level = crawler_settings.log_level.lower()
    elif crawler_settings.django_debug_mode:
        log_level = "debug"

    options: dict[str, typing.Any] = {
        "bind": bind_target,
        "backlog": socket_queue_size,
        "workers": workers,
        "threads": threads,
        "timeout": timeout,
        "proc_name": "pandachaika",
        "loglevel": log_level,
        "accesslog": access_log,
        "errorlog": error_log,
        "reload": crawler_settings.cherrypy_auto_restart,
        "when_ready": when_ready,
        "post_fork": post_fork,
    }

    if threads > 1:
        options["worker_class"] = "gthread"

    if crawler_settings.webserver.enable_ssl:
        options["certfile"] = crawler_settings.webserver.ssl_certificate
        options["keyfile"] = crawler_settings.webserver.ssl_private_key

    if args.pidfile:
        options["pidfile"] = args.pidfile

    if args.daemonize:
        options["daemon"] = True

    from django.db import connections

    connections.close_all()

    print(f"Loading and serving Panda Backup with Gunicorn. Listening on {listening_on_message}")
    GunicornApplication(app, options).run()


def run_cherrypy(crawler_settings: Settings, args: argparse.Namespace) -> None:
    if args.port:
        cherrypy_port = args.port
    else:
        cherrypy_port = crawler_settings.webserver.bind_port

    if args.socket_file:
        cherrypy_socket_file = args.socket_file
    else:
        cherrypy_socket_file = crawler_settings.webserver.socket_file

    if args.socket_queue_size:
        cherrypy_socket_queue_size = args.socket_queue_size
    else:
        cherrypy_socket_queue_size = crawler_settings.webserver.socket_queue_size

    if args.thread_pool:
        cherrypy_thread_pool = args.thread_pool
    else:
        cherrypy_thread_pool = crawler_settings.webserver.thread_pool

    cherrypy_settings = {
        "server.socket_host": crawler_settings.webserver.bind_address,
        "server.socket_port": cherrypy_port,
        "server.socket_file": cherrypy_socket_file,
        "server.socket_queue_size": cherrypy_socket_queue_size,
        "server.thread_pool": cherrypy_thread_pool,
        "checker.on": False,
        "engine.autoreload.on": crawler_settings.cherrypy_auto_restart,
        "log.screen": crawler_settings.webserver.log_to_screen,
        "server.max_request_body_size": 0,
    }

    if crawler_settings.webserver.enable_ssl:
        cherrypy_settings.update(
            {
                "server.ssl_module": "builtin",
                "server.ssl_certificate": crawler_settings.webserver.ssl_certificate,
                "server.ssl_private_key": crawler_settings.webserver.ssl_private_key,
                "response.timeout": 600,
            }
        )

    cherrypy.config.update(cherrypy_settings)

    def stop_process() -> None:
        cherrypy.engine.exit()

    def restart_process() -> None:
        cherrypy.engine.restart()

    user_handler = cherrypy.process.plugins.SignalHandler(cherrypy.engine)
    user_handler.handlers["SIGUSR2"] = restart_process
    user_handler.handlers["SIGTERM"] = stop_process

    # SIGTERM exits, SIGUSR2 forces restart.
    cherrypy.engine.signal_handler = user_handler

    if args.pidfile:
        plugins.PIDFile(cherrypy.engine, args.pidfile).subscribe()

    if args.daemonize:
        # TODO: Daemonizing is stopping the start_workers method form starting workers on startup.
        cherrypy.config.update({"log.screen": False})
        plugins.Daemonizer(cherrypy.engine).subscribe()

    DjangoAppPlugin(
        cherrypy.engine,
        settings_module="pandabackup.settings",
        local_settings=crawler_settings,
    ).subscribe()

    if cherrypy_socket_file:
        listening_on_message = "socket file {}".format(cherrypy_socket_file)
    else:
        listening_on_message = "{}:{}".format(crawler_settings.webserver.bind_address, str(cherrypy_port))

    cherrypy.log("Loading and serving Panda Backup with CherryPy. Listening on {}".format(listening_on_message))

    cherrypy.engine.signals.subscribe()
    cherrypy.engine.start()
    if cherrypy_socket_file:
        os.chmod(cherrypy_socket_file, 0o777)  # The fchmod in cherrypy is not working properly.
    cherrypy.engine.block()


if __name__ == "__main__":

    parser = argparse.ArgumentParser(prog="PandaServer")

    parser.add_argument(
        "-c",
        "--config-dir",
        required=False,
        action="store",
        help="Directory to store configuration files. (database, logs, configuration file)",
    )

    parser.add_argument("-p", "--port", type=int, required=False, action="store", help="Override port.")

    parser.add_argument(
        "-s",
        "--socket-file",
        required=False,
        action="store",
        help="Use the specified UNIX socket.",
    )

    parser.add_argument(
        "-d",
        "--daemonize",
        required=False,
        action="store_true",
        default=False,
        help="Run the server as a daemon.",
    )

    parser.add_argument(
        "-pf",
        "--pidfile",
        required=False,
        action="store",
        default=None,
        help="Store the process id in the given file.",
    )

    parser.add_argument(
        "--socket-queue-size",
        type=int,
        required=False,
        action="store",
        help="Override socket queue size (backlog).",
    )

    parser.add_argument(
        "--thread-pool",
        type=int,
        required=False,
        action="store",
        help="Override thread pool size (CherryPy).",
    )

    parser.add_argument(
        "--backend",
        "--server",
        choices=["cherrypy", "gunicorn"],
        required=False,
        action="store",
        help="Override webserver backend (cherrypy or gunicorn).",
    )

    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        required=False,
        action="store",
        help="Override number of worker processes (Gunicorn).",
    )

    parser.add_argument(
        "-t",
        "--threads",
        type=int,
        required=False,
        action="store",
        help="Override number of threads per worker (Gunicorn).",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        required=False,
        action="store",
        help="Override worker timeout in seconds (Gunicorn).",
    )

    args = parser.parse_args()

    if args.config_dir:
        crawler_settings = Settings(load_from_disk=True, default_dir=args.config_dir)
        os.environ["PANDA_CONFIG_DIR"] = args.config_dir
    else:
        crawler_settings = Settings(load_from_disk=True)

    backend = args.backend or crawler_settings.webserver.backend or "cherrypy"
    backend = backend.lower()

    if backend == "gunicorn" and sys.platform == "win32":
        print("WARNING: Gunicorn is not supported on Windows. Falling back to CherryPy backend.", file=sys.stderr)
        backend = "cherrypy"

    if backend == "gunicorn":
        run_gunicorn(crawler_settings, args)
    else:
        run_cherrypy(crawler_settings, args)

