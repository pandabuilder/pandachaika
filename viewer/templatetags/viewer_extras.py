import html
import json
import re
from json import JSONDecodeError
from typing import Any, TypeVar, Union, ItemsView, Optional

from django import template
from django.template import RequestContext
from django.template.defaultfilters import stringfilter
from django.utils.encoding import punycode
from django.utils.functional import keep_lazy_text
from django.utils.html import smart_urlquote, Urlizer, escape
from django.utils.safestring import SafeText, mark_safe

from core.base.utilities import translate_tag, artist_from_title
from viewer.models import Archive, Tag
from viewer.utils.tags import sort_tags

register = template.Library()

T = TypeVar("T", float, int)


@register.simple_tag(takes_context=True)
def url_toggle(context: RequestContext, field: SafeText) -> str:
    dict_ = context["request"].GET.copy()

    if field in dict_:
        del dict_[field]
    else:
        dict_[field] = ""

    return dict_.urlencode()


@register.simple_tag(takes_context=True)
def url_replace(context: RequestContext, field: SafeText, value: SafeText) -> str:
    dict_ = context["request"].GET.copy()
    dict_[field] = str(value)
    return dict_.urlencode()


@register.simple_tag(takes_context=True)
def url_multi_replace(context: RequestContext, **kwargs: Any) -> str:
    dict_ = context["request"].GET.copy()
    for key, value in kwargs.items():
        dict_[key] = str(value)
    return dict_.urlencode()


@register.filter
def subtract(value: T, arg: T) -> T:
    return value - arg


@register.filter
def revert_order(order: str) -> str:
    if order == "desc":
        order = "asc"
    elif order == "asc":
        order = "desc"
    else:
        order = "desc"
    return order


@register.filter
def color_percent(fraction: int, total: int) -> str:
    if total == 0:
        return ""
    try:
        fraction_i = float(fraction)
        if float(fraction_i) > float(total):
            fraction_i = float(fraction_i) - 2 * (float(fraction_i) - float(total))
        result = (float(fraction_i) / float(total)) * 100
        if result >= 100:
            return "total"
        elif result > 90:
            return "very-high"
        elif result > 80:
            return "high"
        else:
            return ""
    except ValueError:
        return ""


@register.filter
def mark_color(mark_priority: float) -> str:
    if mark_priority >= 4:
        return "mark-high"
    elif 4 > mark_priority >= 1:
        return "mark-mid"
    else:
        return "mark-low"


@register.filter
def format_setting_value(value: T) -> Union[T, ItemsView[str, Any]]:
    if hasattr(value, "__dict__"):
        return vars(value).items()
    elif hasattr(value, "__slots__"):
        return {k: getattr(value, k) for k in value.__slots__}.items()
    else:
        return value


SPECIAL_RE = re.compile(r"\(special-link\):\((.*?)\)?\((.*?)\)")
SPECIAL_FINAL = r"<a href=\2>\1</a>"

SPECIAL_RE_OLD = re.compile(r"special-link:(/archive/\d+/?)")
SPECIAL_FINAL_OLD = r"<a href=\1>\1</a>"

POPOVER_IMG_RE = re.compile(r"\(popover-img\):\((.*?)\)?\((.*?)\)")
POPOVER_IMG_FINAL = r'<a href="#" class="img-preview" data-image-url=\2 rel="popover">\1</a>'


@register.filter(is_safe=True)
@stringfilter
def convert_special_urls(value: str) -> str:
    value = re.sub(SPECIAL_RE, SPECIAL_FINAL, value)
    value = re.sub(SPECIAL_RE_OLD, SPECIAL_FINAL_OLD, value)
    value = re.sub(POPOVER_IMG_RE, POPOVER_IMG_FINAL, value)
    return value


class UrlizerNoRef(Urlizer):
    """
    Convert any URLs in text into clickable links.

    Work on http://, https://, www. links, and also on links ending in one of
    the original seven gTLDs (.com, .edu, .gov, .int, .mil, .net, and .org).
    Links can have trailing punctuation (periods, commas, close-parens) and
    leading punctuation (opening parens) and it'll still do the right thing.
    """

    def handle_word(
        self,
        word,
        *,
        safe_input,
        trim_url_limit=None,
        nofollow=False,
        autoescape=False,
    ):
        if "." in word or "@" in word or ":" in word:
            # lead: Punctuation trimmed from the beginning of the word.
            # middle: State of the word.
            # trail: Punctuation trimmed from the end of the word.
            lead, middle, trail = self.trim_punctuation(word)
            # Make URL we want to point to.
            url = None
            nofollow_attr = ' rel="noopener noreferrer nofollow"' if nofollow else ""
            if self.simple_url_re.match(middle):
                url = smart_urlquote(html.unescape(middle))
            elif self.simple_url_2_re.match(middle):
                url = smart_urlquote("http://%s" % html.unescape(middle))
            elif ":" not in middle and self.is_email_simple(middle):
                local, domain = middle.rsplit("@", 1)
                try:
                    domain = punycode(domain)
                except UnicodeError:
                    return word
                url = self.mailto_template.format(local=local, domain=domain)
                nofollow_attr = ""
            # Make link.
            if url:
                trimmed = self.trim_url(middle, limit=trim_url_limit)
                if autoescape and not safe_input:
                    lead, trail = escape(lead), escape(trail)
                    trimmed = escape(trimmed)
                middle = self.url_template.format(
                    href=escape(url),
                    attrs=nofollow_attr,
                    url=trimmed,
                )
                return mark_safe(f"{lead}{middle}{trail}")
            else:
                if safe_input:
                    return mark_safe(word)
                elif autoescape:
                    return escape(word)
        elif safe_input:
            return mark_safe(word)
        elif autoescape:
            return escape(word)
        return word


urlizer_all_rel = UrlizerNoRef()


@register.filter(is_safe=True, needs_autoescape=True)
@stringfilter
@keep_lazy_text
def urlize_all_rel(text, trim_url_limit=None, nofollow=True, autoescape=True):
    return mark_safe(urlizer_all_rel(text, trim_url_limit=trim_url_limit, nofollow=nofollow, autoescape=autoescape))


@register.filter
def archive_title_class(archive: Archive) -> str:
    if not archive.crc32:
        title_class = "archive-incomplete"
    elif archive.gallery and archive.gallery.filesize and archive.filesize != archive.gallery.filesize:
        title_class = "archive-diff"
    else:
        title_class = ""
    return title_class


@register.filter
def get_list(dictionary, key):
    return dictionary.getlist(key)


@register.filter
def artist_name_from_str(title: Optional[str]) -> str:
    if title:
        return translate_tag(artist_from_title(title))
    else:
        return ""


ARCHIVE_INCLUDED_FIELDS = (
    "title",
    "title_jpn",
    "source_type",
    "reason",
    "public_date",
    "public",
    "gallery",
    "filecount",
    "filesize",
    "extracted",
    "details",
    "crc32",
    "binned",
)


@register.filter
def changes_archive_delta(new_record):
    prev_record = new_record.prev_record
    if prev_record is None:
        return []
    delta = new_record.diff_against(prev_record, included_fields=ARCHIVE_INCLUDED_FIELDS)
    return delta.changes


GALLERY_INCLUDED_FIELDS = (
    "title",
    "title_jpn",
    "category",
    "uploader",
    "comment",
    "posted",
    "filecount",
    "filesize",
    "expunged",
    "disowned",
    "rating",
    "fjord",
    "public",
    "dl_type",
    "reason",
    "thumbnail_url",
    "status",
)

GALLERY_PUBLIC_INCLUDED_FIELDS = (
    "title",
    "title_jpn",
    "category",
    "uploader",
    "comment",
    "posted",
    "filecount",
    "filesize",
    "expunged",
    "disowned",
    "rating",
)


@register.filter
def changes_gallery_delta(new_record, is_authenticated=False):
    prev_record = new_record.prev_record
    if prev_record is None:
        return []
    delta = new_record.diff_against(
        prev_record, included_fields=GALLERY_INCLUDED_FIELDS if is_authenticated else GALLERY_PUBLIC_INCLUDED_FIELDS
    )
    return delta.changes


@register.filter
def tag_query_to_tag_lists(tag_query) -> list[tuple[str, list[Tag]]]:
    return sort_tags(tag_query)


@register.filter
def tag_history_to_tag_lists(history) -> list[tuple[str, list[Tag]]]:
    tag_query = history.historicalgallery_tags_set.all().prefetch_related("tag")
    return sort_tags([x.tag for x in tag_query])


@register.filter
def gallery_history_fields(new_record, is_authenticated=False):

    fields_to_check = GALLERY_INCLUDED_FIELDS if is_authenticated else GALLERY_PUBLIC_INCLUDED_FIELDS

    history_fields = {gallery_field: getattr(new_record, gallery_field) for gallery_field in fields_to_check}

    history_fields["tags"] = ""

    return history_fields.items()


@register.filter
def format_json(maybe_json: Optional[str]) -> str:
    if maybe_json is None:
        return "None"
    try:
        json_data = json.loads(maybe_json)

        json_formatted = json.dumps(
            json_data,
            indent=2,
            sort_keys=True,
            ensure_ascii=False,
        )

        return json_formatted

    except JSONDecodeError:
        return maybe_json


@register.filter
def escape_colon(word: str):
    return word.replace(":", "%3A")
