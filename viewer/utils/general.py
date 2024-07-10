import re


def valid_sha1_string(s: str) -> bool:
    return re.match("^[a-fA-F0-9]{40}$", s) is not None
