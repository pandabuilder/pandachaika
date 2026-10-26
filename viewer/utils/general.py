import re
from typing import TYPE_CHECKING
from django.http import HttpRequest

if TYPE_CHECKING:
    from core.base.setup import Settings


def valid_sha1_string(s: str) -> bool:
    return re.match("^[a-fA-F0-9]{40}$", s) is not None


def clean_up_referer(s: str) -> str:
    return s.replace("Referer: ", "")


def should_use_refined_views(request: HttpRequest, crawler_settings: "Settings | None" = None) -> bool:
    """Determine whether to use the refined archive/gallery templates.

    Refined views are accessed exclusively via the 'view_style=new' GET parameter.
    Otherwise, the default legacy/old templates are used.
    """
    return request.GET.get("view_style") == "new"
