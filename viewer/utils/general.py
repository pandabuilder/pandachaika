import re
from typing import TYPE_CHECKING
from django.http import HttpRequest

if TYPE_CHECKING:
    from core.base.setup import Settings


def valid_sha1_string(s: str) -> bool:
    return re.match("^[a-fA-F0-9]{40}$", s) is not None


def clean_up_referer(s: str) -> str:
    return s.replace("Referer: ", "")


def should_use_refined_views(request: HttpRequest, crawler_settings: "Settings") -> bool:
    """Determine whether to use the refined archive/gallery templates.

    The refined views are opt-in via crawler_settings.refined_archive_gallery_views.
    Public (anonymous) users always see the legacy/old design.
    Authenticated users see the refined design when enabled on the instance,
    and can optionally toggle their preference via the 'view_style' GET parameter
    or session/cookie ('old' vs 'new').
    """
    if not getattr(crawler_settings, "refined_archive_gallery_views", False):
        return False
    if not getattr(request, "user", None) or not request.user.is_authenticated:
        return False

    view_pref = request.GET.get("view_style")
    if view_pref in ("old", "new"):
        if hasattr(request, "session"):
            request.session["archive_gallery_view_style"] = view_pref
        return view_pref == "new"

    if hasattr(request, "session"):
        session_pref = request.session.get("archive_gallery_view_style")
        if session_pref in ("old", "new"):
            return session_pref == "new"

    cookie_pref = request.COOKIES.get("archive_gallery_view_style")
    if cookie_pref in ("old", "new"):
        return cookie_pref == "new"

    return True
