import re
from typing import TYPE_CHECKING
from django.http import HttpRequest, HttpResponseRedirect, QueryDict
from django.utils.http import url_has_allowed_host_and_scheme

if TYPE_CHECKING:
    from core.base.setup import Settings


def valid_sha1_string(s: str) -> bool:
    return re.match("^[a-fA-F0-9]{40}$", s) is not None


def get_safe_referer(request: HttpRequest, fallback: str) -> str:
    """Return the Referer URL if it points back to this site, otherwise ``fallback``.

    The header is optional: browsers omit it under ``Referrer-Policy: no-referrer``
    and privacy settings, so callers must never index ``request.META`` for it.
    A Referer for another host (or plain HTTP when the request is HTTPS) is
    treated as missing, so this never redirects off-site.
    """
    # Some clients have sent the header name inside its value.
    referer = request.META.get("HTTP_REFERER", "").strip().removeprefix("Referer: ")
    if referer and url_has_allowed_host_and_scheme(
        url=referer,
        allowed_hosts={request.get_host()},
        require_https=request.is_secure(),
    ):
        return referer
    return fallback


def safe_redirect_back(request: HttpRequest, fallback: str) -> HttpResponseRedirect:
    """Redirect to the page the request came from, or to ``fallback`` (see ``get_safe_referer``)."""
    return HttpResponseRedirect(get_safe_referer(request, fallback))


def should_use_refined_views(request: HttpRequest, crawler_settings: "Settings | None" = None) -> bool:
    """Determine whether to use the refined archive/gallery templates.

    Refined views are accessed exclusively via the 'view_style=new' GET parameter.
    Otherwise, the default legacy/old templates are used.
    """
    return request.GET.get("view_style") == "new"


PAGE_SIZE_OPTIONS = (25, 50, 100, 200, 500)


def page_size(params: "QueryDict | dict[str, str]", default: int = 100, maximum: int = 500, key: str = "limit") -> int:
    """Page size from a request's query params, between 1 and maximum."""
    try:
        return min(maximum, max(1, int(params.get(key, default) or default)))
    except (TypeError, ValueError):
        return default
