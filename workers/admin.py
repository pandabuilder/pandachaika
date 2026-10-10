# -*- coding: utf-8 -*-
from django.contrib import admin
from workers.models import TaskQueue, WorkerProcess, SchedulerState


@admin.register(TaskQueue)
class TaskQueueAdmin(admin.ModelAdmin):
    list_display = ("id", "task_type", "status", "priority", "user", "created_at", "started_at", "finished_at")
    list_filter = ("status", "task_type", "created_at")
    search_fields = ("args", "reason", "error_message")
    readonly_fields = ("created_at", "started_at", "finished_at", "traceback")


@admin.register(WorkerProcess)
class WorkerProcessAdmin(admin.ModelAdmin):
    list_display = ("pid", "hostname", "worker_type", "status", "started_at", "last_heartbeat")
    list_filter = ("status", "worker_type")


@admin.register(SchedulerState)
class SchedulerStateAdmin(admin.ModelAdmin):
    list_display = ("name", "is_running", "worker_pid", "last_run", "next_run", "last_heartbeat", "command_requested")
    list_filter = ("is_running", "schedule_type")
    search_fields = ("name", "display_name")

