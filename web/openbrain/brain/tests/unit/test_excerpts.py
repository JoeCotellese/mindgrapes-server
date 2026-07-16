"""Unit tests for excerpts.format_excerpt (#101).

Pins the excerpt-centering, <mark> wrapping, and HTML-escaping behavior.
"""

from openbrain.brain.excerpts import format_excerpt


def test_short_content_no_surface_form_returns_escaped_content():
    assert format_excerpt("hello world", None) == "hello world"


def test_short_content_with_match_wraps_in_mark():
    # Match is case-insensitive but the wrapped text keeps the original case.
    assert format_excerpt("Hello World", "world") == "Hello <mark>World</mark>"


def test_short_content_escapes_surrounding_and_matched_text():
    content = 'a <b> & "c"'
    assert (
        format_excerpt(content, "<b>") == "a <mark>&lt;b&gt;</mark> &amp; &quot;c&quot;"
    )


def test_short_content_surface_not_found_returns_escaped_content():
    assert format_excerpt("plain & simple", "missing") == "plain &amp; simple"


def test_long_content_centers_window_with_lead_and_tail_ellipsis():
    content = "0123456789ABCDEFGHIJ" + "needle" + "KLMNOPQRSTUVWXYZ0987"
    assert (
        format_excerpt(content, "needle", max_len=20)
        == "…DEFGHIJ<mark>needle</mark>KLMNOPQ…"
    )


def test_long_content_no_surface_form_returns_head_excerpt_with_ellipsis():
    content = "x" * 50
    assert format_excerpt(content, None, max_len=20) == "x" * 20 + "…"


def test_long_content_match_at_start_has_no_lead_ellipsis():
    content = "needle" + "x" * 40
    assert (
        format_excerpt(content, "needle", max_len=20) == "<mark>needle</mark>xxxxxxx…"
    )


def test_long_content_surface_not_found_returns_head_excerpt():
    content = "y" * 50
    assert format_excerpt(content, "absent", max_len=20) == "y" * 20 + "…"
