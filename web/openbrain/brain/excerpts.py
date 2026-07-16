"""Server-side excerpt formatting for the Brain UI.

Builds a ~max_len-char excerpt for search results (#101):
centered on the matched surface form, with the match wrapped in <mark>. Every
segment is HTML-escaped before assembly, so the output is safe to drop into a
template with |safe. Falls back to a head-of-content excerpt when the surface
form isn't located.
"""

import re

# The minimal escape set needed before output is marked |safe (#101).
HTML_ESCAPES = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
}
_ESCAPE_RE = re.compile(r"[&<>\"']")


def _escape_html(s: str) -> str:
    return _ESCAPE_RE.sub(lambda m: HTML_ESCAPES[m.group(0)], s)


def format_excerpt(content: str, surface_form: str | None, max_len: int = 200) -> str:
    if len(content) <= max_len:
        if not surface_form:
            return _escape_html(content)
        idx = content.lower().find(surface_form.lower())
        if idx < 0:
            return _escape_html(content)
        matched = content[idx : idx + len(surface_form)]
        return (
            _escape_html(content[:idx])
            + "<mark>"
            + _escape_html(matched)
            + "</mark>"
            + _escape_html(content[idx + len(surface_form) :])
        )

    idx = content.lower().find(surface_form.lower()) if surface_form else -1

    if idx < 0:
        return _escape_html(content[:max_len]) + "…"

    matched_len = len(surface_form)
    matched = content[idx : idx + matched_len]
    half_window = max(0, (max_len - matched_len) // 2)
    start = max(0, idx - half_window)
    end = min(len(content), idx + matched_len + half_window)

    lead_ellipsis = "…" if start > 0 else ""
    tail_ellipsis = "…" if end < len(content) else ""

    return (
        lead_ellipsis
        + _escape_html(content[start:idx])
        + "<mark>"
        + _escape_html(matched)
        + "</mark>"
        + _escape_html(content[idx + matched_len : end])
        + tail_ellipsis
    )
