"""
WSGI config for pandabackup project.

It exposes the WSGI callable as a module-level variable named ``application``.
"""

import os

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pandabackup.settings")

from django.core.wsgi import get_wsgi_application

application = get_wsgi_application()

from pandabackup import settings

settings.WORKERS.start_workers(settings.CRAWLER_SETTINGS)
settings.CRAWLER_SETTINGS.create_missing_directories()
