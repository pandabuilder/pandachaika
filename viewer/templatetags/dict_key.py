from django.template.defaultfilters import register


@register.filter(name='dict_key')
def dict_key(d, k):
    """Returns the given key from a dictionary."""
    if d is None:
        return None
    return d[k]
