"""Readable display names for apps in the mail and merged timeline.

Clients upload raw process names (e.g. "dwrg", "deadbydaylight-win64-shipping").
Those are fine as merge keys, but they look bad in the daily mail and the
merged timeline. This module maps the ugly ones to a readable name and
shortens the rest, at render time only — the stored app_name never changes,
so history and the phone/PC merge are untouched.
"""

# Exact key (lower-cased, ".exe" stripped) -> readable name.
_DISPLAY_NAMES = {
    "dwrg": "第五人格",
    "deadbydaylight": "黎明杀机",
    "deadbydaylight-win64-shipping": "黎明杀机",
}

# Build suffixes stripped from a raw process name, longest first.
_SUFFIXES = (
    "-win64-shipping",
    "-win32-shipping",
    "-win64",
    "-win32",
    "-shipping",
)


def _strip_suffixes(name):
    low = name.lower()
    for suffix in _SUFFIXES:
        if low.endswith(suffix):
            return name[: -len(suffix)]
    return name


def display_name_for(app_name, uploaded=None):
    """Readable name for an app in the mail / merged timeline.

    Explicit mappings win (dwrg -> 第五人格， deadbydaylight-win64-shipping
    -> 黎明杀机）; otherwise the uploaded display_name is kept when present,
    with ".exe" and build suffixes stripped; the raw app_name is the last
    resort. Chinese names pass through untouched.
    """
    key = (app_name or "").strip().lower()
    if key.endswith(".exe"):
        key = key[:-4]
    if key in _DISPLAY_NAMES:
        return _DISPLAY_NAMES[key]
    stripped_key = _strip_suffixes(key)
    if stripped_key in _DISPLAY_NAMES:
        return _DISPLAY_NAMES[stripped_key]
    candidate = (uploaded or app_name or "").strip()
    if candidate.lower().endswith(".exe"):
        candidate = candidate[:-4]
    candidate = _strip_suffixes(candidate)
    return candidate or "(未知)"
