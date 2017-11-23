from django import template

register = template.Library()


@register.simple_tag(takes_context=True)
def url_replace(context, field, value):
    dict_ = context['request'].GET.copy()
    dict_[field] = value
    return dict_.urlencode()


@register.simple_tag(takes_context=True)
def url_multi_replace(context, **kwargs):
    dict_ = context['request'].GET.copy()
    for key, value in kwargs.items():
        dict_[key] = value
    return dict_.urlencode()


@register.filter
def subtract(value, arg):
    return value - arg


@register.filter
def revert_order(order):
    if order == 'desc':
        order = 'asc'
    elif order == 'asc':
        order = 'desc'
    else:
        order = 'desc'
    return order


@register.filter
def color_percent(fraction, total):
    if total == 0:
        return ''
    try:
        if float(fraction) > float(total):
            fraction = float(fraction) - 2 * (float(fraction) - float(total))
        result = ((float(fraction) / float(total)) * 100)
        if result == 100:
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
def format_setting_value(value):
    if hasattr(value, '__dict__'):
        return vars(value).items()
    else:
        return value
