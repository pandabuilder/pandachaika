from django.template.defaultfilters import register


@register.filter(name="dict_key")
def dict_key(d, k):
    """Returns the given key or index from a dictionary, sequence, or object."""
    if d is None:
        return ""
    if isinstance(d, dict):
        return d.get(k, "")
    try:
        idx = int(k)
        return d[idx]
    except (ValueError, TypeError, IndexError, KeyError):
        pass
    if hasattr(d, str(k)):
        return getattr(d, str(k))
    try:
        return d[k]
    except Exception:
        return ""
