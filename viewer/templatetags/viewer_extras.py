import html
import re
from typing import Any, TypeVar, Union, ItemsView, Optional

from django import template
from django.template import RequestContext
from django.template.defaultfilters import stringfilter
from django.utils.encoding import punycode  # type: ignore
from django.utils.functional import keep_lazy_text
from django.utils.html import WRAPPING_PUNCTUATION, TRAILING_PUNCTUATION_CHARS, word_split_re, simple_url_re, \
    smart_urlquote, simple_url_2_re
from django.utils.safestring import SafeText, mark_safe, SafeData

from core.base.utilities import translate_tag, artist_from_title
from viewer.models import Archive

register = template.Library()

T = TypeVar('T', float, int)


@register.simple_tag(takes_context=True)
def url_toggle(context: RequestContext, field: SafeText) -> str:
    dict_ = context['request'].GET.copy()

    if field in dict_:
        del dict_[field]
    else:
        dict_[field] = ''

    return dict_.urlencode()


@register.simple_tag(takes_context=True)
def url_replace(context: RequestContext, field: SafeText, value: SafeText) -> str:
    dict_ = context['request'].GET.copy()
    dict_[field] = str(value)
    return dict_.urlencode()


@register.simple_tag(takes_context=True)
def url_multi_replace(context: RequestContext, **kwargs: Any) -> str:
    dict_ = context['request'].GET.copy()
    for key, value in kwargs.items():
        dict_[key] = str(value)
    return dict_.urlencode()


@register.filter
def subtract(value: T, arg: T) -> T:
    return value - arg


@register.filter
def revert_order(order: str) -> str:
    if order == 'desc':
        order = 'asc'
    elif order == 'asc':
        order = 'desc'
    else:
        order = 'desc'
    return order


@register.filter
def color_percent(fraction: int, total: int) -> str:
    if total == 0:
        return ''
    try:
        fraction_i = float(fraction)
        if float(fraction_i) > float(total):
            fraction_i = float(fraction_i) - 2 * (float(fraction_i) - float(total))
        result = ((float(fraction_i) / float(total)) * 100)
        if result >= 100:
            return 'total'
        elif result > 90:
            return 'very-high'
        elif result > 80:
            return 'high'
        else:
            return ''
    except ValueError:
        return ''


@register.filter
def mark_color(mark_priority: float) -> str:
    if mark_priority >= 4:
        return 'mark-high'
    elif 4 > mark_priority >= 1:
        return 'mark-mid'
    else:
        return 'mark-low'


@register.filter
def format_setting_value(value: T) -> Union[T, ItemsView[str, Any]]:
    if hasattr(value, '__dict__'):
        return vars(value).items()
    elif hasattr(value, '__slots__'):
        return {k: getattr(value, k) for k in value.__slots__}.items()  # type: ignore
    else:
        return value


SPECIAL_RE = re.compile(r"\(special-link\):\((.*?)\)?\((.*?)\)")
SPECIAL_FINAL = r"<a href=\2>\1</a>"

SPECIAL_RE_OLD = re.compile(r"special-link:(/archive/\d+/?)")
SPECIAL_FINAL_OLD = r"<a href=\1>\1</a>"


@register.filter(is_safe=True)
@stringfilter
def convert_special_urls(value: str) -> str:
    value = re.sub(SPECIAL_RE, SPECIAL_FINAL, value)
    value = re.sub(SPECIAL_RE_OLD, SPECIAL_FINAL_OLD, value)
    return value


@register.filter(is_safe=True, needs_autoescape=True)
@stringfilter
def urlize_all_rel(value, autoescape=True):
    """Convert URLs in plain text into clickable links."""
    return mark_safe(_urlize_all_text(value, nofollow=True, autoescape=autoescape))


@keep_lazy_text
def _urlize_all_text(text, trim_url_limit=None, nofollow=False, autoescape=False):
    """
    Convert any URLs in text into clickable links.

    Works on http://, https://, www. links, and also on links ending in one of
    the original seven gTLDs (.com, .edu, .gov, .int, .mil, .net, and .org).
    Links can have trailing punctuation (periods, commas, close-parens) and
    leading punctuation (opening parens) and it'll still do the right thing.

    If trim_url_limit is not None, truncate the URLs in the link text longer
    than this limit to trim_url_limit - 1 characters and append an ellipsis.

    If nofollow is True, give the links a rel="nofollow" attribute.

    If autoescape is True, autoescape the link text and URLs.
    """
    safe_input = isinstance(text, SafeData)

    def trim_url(x, limit=trim_url_limit):
        if limit is None or len(x) <= limit:
            return x
        return '%s…' % x[:max(0, limit - 1)]

    def trim_punctuation(lead, middle, trail):
        """
        Trim trailing and wrapping punctuation from `middle`. Return the items
        of the new state.
        """
        # Continue trimming until middle remains unchanged.
        trimmed_something = True
        while trimmed_something:
            trimmed_something = False
            # Trim wrapping punctuation.
            for opening, closing in WRAPPING_PUNCTUATION:
                if middle.startswith(opening):
                    middle = middle[len(opening):]
                    lead += opening
                    trimmed_something = True
                # Keep parentheses at the end only if they're balanced.
                if middle.endswith(closing) and middle.count(closing) == middle.count(opening) + 1:
                    middle = middle[:-len(closing)]
                    trail = closing + trail
                    trimmed_something = True
            # Trim trailing punctuation (after trimming wrapping punctuation,
            # as encoded entities contain ';'). Unescape entities to avoid
            # breaking them by removing ';'.
            middle_unescaped = html.unescape(middle)
            stripped = middle_unescaped.rstrip(TRAILING_PUNCTUATION_CHARS)
            if middle_unescaped != stripped:
                trail = middle[len(stripped):] + trail
                middle = middle[:len(stripped) - len(middle_unescaped)]
                trimmed_something = True
        return lead, middle, trail

    def is_email_simple(value):
        """Return True if value looks like an email address."""
        # An @ must be in the middle of the value.
        if '@' not in value or value.startswith('@') or value.endswith('@'):
            return False
        try:
            p1, p2 = value.split('@')
        except ValueError:
            # value contains more than one @.
            return False
        # Dot must be in p2 (e.g. example.com)
        if '.' not in p2 or p2.startswith('.'):
            return False
        return True

    words = word_split_re.split(str(text))
    for i, word in enumerate(words):
        if '.' in word or '@' in word or ':' in word:
            # lead: Current punctuation trimmed from the beginning of the word.
            # middle: Current state of the word.
            # trail: Current punctuation trimmed from the end of the word.
            lead, middle, trail = '', word, ''
            # Deal with punctuation.
            lead, middle, trail = trim_punctuation(lead, middle, trail)

            # Make URL we want to point to.
            url = None
            nofollow_attr = ' rel="noopener noreferrer nofollow"' if nofollow else ''
            if simple_url_re.match(middle):
                url = smart_urlquote(html.unescape(middle))
            elif simple_url_2_re.match(middle):
                url = smart_urlquote('http://%s' % html.unescape(middle))
            elif ':' not in middle and is_email_simple(middle):
                local, domain = middle.rsplit('@', 1)
                try:
                    domain = punycode(domain)  # type: ignore
                except UnicodeError:
                    continue
                url = 'mailto:%s@%s' % (local, domain)
                nofollow_attr = ''

            # Make link.
            if url:
                trimmed = trim_url(middle)
                if autoescape and not safe_input:
                    lead, trail = html.escape(lead), html.escape(trail)
                    trimmed = html.escape(trimmed)
                middle = '<a href="%s"%s>%s</a>' % (html.escape(url), nofollow_attr, trimmed)
                words[i] = mark_safe('%s%s%s' % (lead, middle, trail))
            else:
                if safe_input:
                    words[i] = mark_safe(word)
                elif autoescape:
                    words[i] = html.escape(word)
        elif safe_input:
            words[i] = mark_safe(word)
        elif autoescape:
            words[i] = html.escape(word)
    return ''.join(words)


@register.filter
def archive_title_class(archive: Archive) -> str:
    if not archive.crc32:
        title_class = 'archive-incomplete'
    elif archive.gallery and archive.gallery.filesize and archive.filesize != archive.gallery.filesize:
        title_class = 'archive-diff'
    else:
        title_class = ''
    return title_class


@register.filter
def get_list(dictionary, key):
    return dictionary.getlist(key)


@register.filter
def artist_name_from_str(title: Optional[str]) -> str:
    if title:
        return translate_tag(artist_from_title(title))
    else:
        return ''
