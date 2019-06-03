from typing import Any, TypeVar, Union, ItemsView

from django import template
from django.template import RequestContext
from django.utils.safestring import SafeText

register = template.Library()

T = TypeVar('T', float, int)


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
def format_setting_value(value: T) -> Union[T, ItemsView[str, Any]]:
    if hasattr(value, '__dict__'):
        return vars(value).items()
    else:
        return value
