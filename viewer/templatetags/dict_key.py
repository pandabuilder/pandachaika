from django.template.defaultfilters import register


@register.filter(name="dict_key")
def dict_key(d, k):
    """Returns the given key from a dictionary."""
    if d is None or k not in d:
        return ""
    return d[k]
