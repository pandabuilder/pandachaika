# -*- coding: utf-8 -*-
from django.apps import AppConfig


class WorkersConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "workers"
    verbose_name = "Background Workers"
