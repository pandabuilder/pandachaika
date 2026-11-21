"""OpenAPI 3.1 document for ``/api/v1/``, generated from the command registry.

The API routes on query string commands (``/api/v1/?archive=5``), so each command is listed under a path key that
names it (``/api/v1/?archive``). Swagger UI merges that key with the operation's own query parameters when it sends a
request; strict validators may warn about the ``?`` in the path key.
"""

import json
from functools import cache
from pathlib import Path
from typing import Any

from django.conf import settings
from django.urls import reverse

from viewer.views.api.handlers.remote import REMOTE_OPERATIONS
from viewer.views.api.registry import COMMANDS, Command, Param

# Copy of the document committed to the repository, which GitLab renders with Swagger UI. Regenerate it with
# ``manage.py openapi_schema --update`` (a test fails when it is out of date).
STATIC_DOCUMENT_PATH = Path(__file__).resolve().parents[3] / "docs" / "api" / "openapi.json"
STATIC_DOCUMENT_SERVERS = [{"url": "https://panda.chaika.moe", "description": "Public instance"}]

ERROR_RESPONSES = {
    "400": ("BadRequest", "The request is malformed: unknown command, invalid JSON, missing or invalid parameter."),
    "401": ("Unauthorized", "Authentication is required, or the bearer token is invalid or expired."),
    "403": ("Forbidden", "The user lacks the permission, or the token lacks the scope, this command needs."),
    "404": ("NotFound", "The object does not exist, or is not visible to the caller."),
}

SCHEMAS: dict[str, Any] = {
    "Error": {
        "type": "object",
        "required": ["error"],
        "properties": {
            "error": {
                "type": "object",
                "required": ["code", "message"],
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Stable machine-readable code.",
                        "examples": [
                            "bad_request",
                            "invalid_json",
                            "unknown_command",
                            "missing_field",
                            "not_authenticated",
                            "invalid_token",
                            "insufficient_scope",
                            "forbidden",
                            "not_found",
                            "method_not_allowed",
                            "internal_error",
                        ],
                    },
                    "message": {"type": "string", "description": "Human-readable explanation."},
                },
            }
        },
    },
    "TaskReference": {
        "type": "object",
        "description": "Added to responses of commands that queue background tasks. Commands that queue one task "
        "return task_id; commands that may queue several return task_ids. status_url is null when nothing was queued.",
        "properties": {
            "task_id": {"type": ["integer", "null"]},
            "task_ids": {"type": "array", "items": {"type": "integer"}},
            "status_url": {"type": ["string", "null"], "examples": ["/api/v1/?task=42"]},
        },
    },
    "Task": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            "task_type": {"type": "string"},
            "status": {"type": "string", "enum": ["pending", "processing", "completed", "failed", "cancelled"]},
            "active": {"type": "boolean", "description": "Pending or processing."},
            "label": {"type": "string"},
            "done_label": {"type": "string"},
            "reason": {"type": "string"},
            "created_at": {"type": "integer", "description": "Unix timestamp."},
            "started_at": {"type": ["integer", "null"]},
            "finished_at": {"type": ["integer", "null"]},
            "not_before": {"type": ["integer", "null"], "description": "Delayed until (e.g. provider ban)."},
            "attempts": {"type": "integer"},
            "max_attempts": {"type": "integer"},
            "error_message": {"type": "string"},
            "result": {
                "type": ["object", "null"],
                "description": "Null while the task is active. outcome, targets (the task's arguments), the "
                "deletion options for delete tasks, crawl_stats for crawler tasks and details (what the worker "
                "recorded on completion, for the task types that record something).",
            },
            "poll_delay_ms": {"type": "integer", "description": "Suggested wait before polling again (0 when done)."},
            "worker_running": {"type": "boolean"},
            "status_url": {"type": "string"},
        },
    },
    "TaskBatch": {
        "type": "object",
        "properties": {
            "total": {"type": "integer"},
            **{
                status: {"type": "integer"}
                for status in ("pending", "processing", "completed", "failed", "cancelled", "missing")
            },
            "active": {"type": "boolean"},
            "done": {"type": "integer"},
            "worker_running": {"type": "boolean"},
            "poll_delay_ms": {"type": "integer"},
            "status_url": {"type": "string"},
            "tasks": {
                "type": "array",
                "items": {
                    "oneOf": [
                        {"$ref": "#/components/schemas/Task"},
                        {
                            "type": "object",
                            "properties": {
                                "id": {"type": "integer"},
                                "task_type": {"const": ""},
                                "status": {"const": "missing"},
                                "active": {"const": False},
                                "label": {"type": "string"},
                                "done_label": {"type": "string"},
                                "error_message": {"type": "string"},
                            },
                        },
                    ]
                },
            },
        },
    },
}

DESCRIPTION = """\
JSON API of Panda Backup.

**Commands.** Requests are routed by a command key in the query string: `GET /api/v1/?archive=5`,
`DELETE /api/v1/?wanted-gallery=3`. If a request has several command keys, the first one in this document's order
wins. `GET /api/v1/` without parameters returns links to this document.

**Authentication.** Anonymous requests can read public data. Send `Authorization: Bearer <token>` (create tokens
on your profile page) or use a session login. Tokens have scopes: `read` for GET, `write` for POST/PUT/DELETE,
`remote` for `/api/v1/remote/`. On `/api/v1/`, a bad or expired token is an error (401) rather than an anonymous
request. Write commands also need the Django permission listed on each operation.

**Errors.** Errors use one envelope, `{"error": {"code": "...", "message": "..."}}`, with a matching HTTP status.

**Background tasks.** Commands that queue worker tasks return `task_id` (or `task_ids`) and a `status_url`;
poll it (`GET ?task=<id>` or `GET ?tasks=<id1>,<id2>`) until `active` is false.

**Legacy routes.** `/api/`, `/api` and `/jsearch/` accept the same commands; their errors also carry the
message in `result`. `/r-api/...` and `/admin-api/` (or `/jsonapi/`) are deprecated: their responses have
`Deprecation` and `Link: rel="successor-version"` headers naming the `/api/v1/` replacement.
"""


def _param_schema(param: Param) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": param.type}
    if param.multi:
        return {"type": "array", "items": schema}
    return schema


def _parameter(param: Param) -> dict[str, Any]:
    parameter: dict[str, Any] = {
        "name": param.name,
        "in": "query",
        "required": param.required,
        "schema": _param_schema(param),
    }
    if param.description:
        parameter["description"] = param.description
    if param.multi:
        parameter.update({"style": "form", "explode": True})
    if param.example is not None:
        parameter["example"] = param.example
    return parameter


def _key_parameter(command: Command) -> dict[str, Any]:
    if command.key_param is not None:
        return _parameter(command.key_param)
    return {
        "name": command.key,
        "in": "query",
        "required": True,
        "allowEmptyValue": True,
        "description": "Command flag; its value is ignored.",
        "schema": {"type": "string", "default": ""},
    }


def _ref_response(name: str) -> dict[str, Any]:
    return {"$ref": f"#/components/responses/{name}"}


def _security(requires_auth: bool) -> list[dict[str, list[str]]]:
    authenticated: list[dict[str, list[str]]] = [{"bearerAuth": []}, {"sessionAuth": []}]
    return authenticated if requires_auth else [{}, *authenticated]


def _operation(command: Command) -> dict[str, Any]:
    scope = "read" if command.method == "GET" else "write"
    notes: list[str] = []
    if command.description:
        notes.append(command.description)
    if command.perm:
        perms = [command.perm] if isinstance(command.perm, str) else list(command.perm)
        notes.append(f"Permission: {' or '.join(f'`{perm}`' for perm in perms)}.")
    notes.append(f"Token scope: `{scope}`." if command.requires_auth else "Anonymous callers only see public data.")
    if command.queues_tasks:
        notes.append("Queues background tasks: poll the returned `status_url`.")

    success_schema: dict[str, Any] = command.response or {"type": ["object", "array"]}
    if command.queues_tasks:
        success_schema = {"allOf": [{"type": "object"}, {"$ref": "#/components/schemas/TaskReference"}]}

    responses: dict[str, Any] = {
        "200": {"description": "Success.", "content": {"application/json": {"schema": success_schema}}},
        "400": _ref_response("BadRequest"),
        "401": _ref_response("Unauthorized"),
    }
    if command.requires_auth or command.perm:
        responses["403"] = _ref_response("Forbidden")
    if command.key_param is not None:
        responses["404"] = _ref_response("NotFound")

    operation: dict[str, Any] = {
        "operationId": f"{command.method.lower()}_{command.key.replace('-', '_')}",
        "tags": [command.tag],
        "summary": command.summary,
        "description": "\n\n".join(notes),
        "parameters": [_key_parameter(command), *(_parameter(param) for param in command.params)],
        "responses": responses,
        "security": _security(command.requires_auth),
        "x-token-scope": scope,
    }
    if command.perm:
        operation["x-required-permission"] = command.perm
    if command.body is not None:
        operation["requestBody"] = {
            "required": False,
            "content": {"application/json": {"schema": command.body}},
        }
    return operation


def _remote_paths() -> dict[str, Any]:
    security: list[dict[str, list[str]]] = [{"bearerAuth": []}, {"sessionAuth": []}]
    errors = {code: _ref_response(name) for code, (name, _) in ERROR_RESPONSES.items()}
    description = (
        "Needs the `viewer.use_remote_api` permission and, with a token, the `remote` scope. "
        "Deprecated aliases: `/admin-api/`, `/jsonapi/`."
    )
    return {
        reverse("viewer:api-v1-remote"): {
            "post": {
                "operationId": "post_remote_operation",
                "tags": ["Remote"],
                "summary": "Run a remote operation (queue links, match archives)",
                "description": description,
                "requestBody": {
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {
                                "type": "object",
                                "required": ["operation", "args"],
                                "properties": {
                                    "operation": {"type": "string", "enum": list(REMOTE_OPERATIONS)},
                                    "args": {
                                        "description": "webcrawler: {links, link, downloader, reason, parentLink, "
                                        "action} or a list of links. match_archive: {archive, match_filter}. "
                                        "match_archive_internally: {archive, provider, clear}."
                                    },
                                    "ignore_global_filters": {"type": "boolean"},
                                },
                            }
                        }
                    },
                },
                "responses": {
                    "200": {
                        "description": "Operations that queue a crawl also return task_id and status_url.",
                        "content": {"application/json": {"schema": {"type": "object"}}},
                    },
                    **errors,
                },
                "security": security,
                "x-token-scope": "remote",
            },
            "get": {
                "operationId": "get_remote_galleries",
                "tags": ["Remote"],
                "summary": "Gallery search with crawler metadata, including private galleries",
                "description": description,
                "parameters": [
                    {
                        "name": "gc",
                        "in": "query",
                        "required": True,
                        "allowEmptyValue": True,
                        "schema": {"type": "string", "default": ""},
                    }
                ],
                "responses": {
                    "200": {
                        "description": "Galleries.",
                        "content": {"application/json": {"schema": {"type": "array", "items": {"type": "object"}}}},
                    },
                    **errors,
                },
                "security": security,
                "x-token-scope": "remote",
            },
        }
    }


@cache
def build_openapi_document() -> dict[str, Any]:
    base = reverse("viewer:api-v1")
    paths: dict[str, Any] = {}
    tags: list[str] = []
    for method, commands in COMMANDS.items():
        for command in commands:
            path = paths.setdefault(f"{base}?{command.key}", {})
            path[method.lower()] = _operation(command)
            if command.tag not in tags:
                tags.append(command.tag)
    paths.update(_remote_paths())

    return {
        "openapi": "3.1.0",
        "info": {"title": "Panda Backup API", "version": "1", "description": DESCRIPTION},
        "servers": [{"url": "/", "description": "This server"}],
        "tags": [{"name": tag} for tag in [*tags, "Remote"]],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "description": "Long-lived token from the profile page, with read/write/remote scopes.",
                },
                "sessionAuth": {"type": "apiKey", "in": "cookie", "name": settings.SESSION_COOKIE_NAME},
            },
            "schemas": SCHEMAS,
            "responses": {
                name: {
                    "description": description,
                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Error"}}},
                }
                for name, description in ERROR_RESPONSES.values()
            },
        },
    }


def render_openapi_document(servers: list[dict[str, str]] | None = None) -> str:
    """The document as JSON text, as served at /api/v1/openapi.json. ``servers`` replaces the relative server URL."""
    document = build_openapi_document()
    if servers is not None:
        document = {**document, "servers": servers}
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"
