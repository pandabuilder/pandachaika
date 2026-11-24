"""The old mugimugi auto wanted sources (doujinshi.org XML API, ``S=objectSearch``) as doujinshi_mirror ones.

No Django imports: the data migration that converts the existing sources uses it too.
"""

from typing import Any

LEGACY_SLIST = {
    "slist_C": "circle",
    "slist_A": "author",
    "slist_P": "parody",
    "slist_H": "character",
    "slist_K": "contents",
    "slist_G": "genre",
    "slist_T": "type",
    "slist_N": "convention",
    "slist_O": "collections",
    "slist_L": "publisher",
    "slist_I": "imprint",
}
LEGACY_FLAGS = {"age": "adult", "anth": "anthology", "bcopy": "copyshi"}
LEGACY_ORDER = {
    "added": "id",
    "date": "released",
    "changed": "changed",
    "title": "title_en",
    "jtitle": "title_jp",
    "pages": "pages",
    # Not in the dump: the order they were added in.
    "page_views": "id",
    "score": "id",
}
LEGACY_MATCH = {"0": "words", "4": "words", "1": "prefix", "2": "suffix", "3": "exact"}


def params_from_legacy(legacy: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Translate a mugimugi source's parameters. Returns the parameters, and the ones that have no equivalent.

    The slist_<letter> values keep their | separators, which the JSON API reads as alternatives (either value).
    """
    params: dict[str, Any] = {}
    dropped: dict[str, Any] = {}
    types: list[str] = []
    for name, value in legacy.items():
        if value is None or value == "":
            continue
        text = str(value).strip()
        if name == "slist_T" or name == "flist":
            types.extend(x.strip() for x in text.replace(",", "|").split("|") if x.strip())
        elif name in LEGACY_SLIST:
            params[LEGACY_SLIST[name]] = text
        elif name in LEGACY_FLAGS and text.upper() in ("Y", "N"):
            params[LEGACY_FLAGS[name]] = "1" if text.upper() == "Y" else "0"
        elif name == "date":
            params["released_from"] = text
        elif name == "date2":
            params["released_to"] = text
        elif name == "order":
            if text in LEGACY_ORDER:
                params["sort"] = LEGACY_ORDER[text]
            else:
                dropped[name] = value
        elif name == "flow":
            params["order"] = "asc" if text.upper() == "ASC" else "desc"
        elif name == "sn":
            params["q"] = text
        elif name == "match" and text in LEGACY_MATCH:
            if LEGACY_MATCH[text] != "words":
                params["match"] = LEGACY_MATCH[text]
        else:
            # cont, sub, FREE, scen: not in the dump.
            dropped[name] = value
    if types:
        params["type"] = "|".join(dict.fromkeys(types))
    if "match" in params and "q" not in params:
        del params["match"]
    return params, dropped
