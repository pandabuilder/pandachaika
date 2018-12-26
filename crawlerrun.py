#!/usr/bin/env python3.6
# -*- coding: utf-8 -*-
import os
import sys

import django
import logging

from django.conf import settings

sys.path.append(os.path.dirname(__file__))

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "pandabackup.settings")

django.setup()

from core.web.crawler import WebCrawler

crawler_settings = settings.CRAWLER_SETTINGS
logger = logging.getLogger('viewer.webcrawler')

h = logging.StreamHandler(stream=sys.stdout)
h.setLevel(logging.DEBUG)
logger.addHandler(h)


if __name__ == "__main__":
    argv = sys.argv[1:]

    web_crawler = WebCrawler(crawler_settings, logger)

    web_crawler.start_crawling(argv)
