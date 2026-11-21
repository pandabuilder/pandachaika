"""JSON API views for external tools (userscript, happypanda, image viewer, other instances, etc.)."""

from viewer.views.api.filters import filter_galleries_no_request, simple_archive_filter
from viewer.views.api.routes import (
    api_login,
    api_logout,
    api_v1,
    api_v1_docs,
    api_v1_openapi,
    api_v1_remote,
    json_api,
    json_parser,
)

__all__ = [
    "api_login",
    "api_logout",
    "api_v1",
    "api_v1_docs",
    "api_v1_openapi",
    "api_v1_remote",
    "json_api",
    "json_parser",
    "simple_archive_filter",
    "filter_galleries_no_request",
]
