import re


def valid_sha1_string(s: str) -> bool:
    return re.match("^[a-fA-F0-9]{40}$", s) is not None


def clean_up_referer(s: str) -> str:
    return s.replace("Referer: ", "")