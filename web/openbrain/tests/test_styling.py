# ABOUTME: Tests the Bulma CSS wiring — vendored stylesheet, no CDN, base layout.
# ABOUTME: Guards #98 invariants so template restyling can't silently break them.

import re
from pathlib import Path

import pytest
from django.conf import settings

pytestmark = pytest.mark.django_db

BULMA_REL = "vendor/bulma.min.css"

# Django's {# #} comment is single-line only: its lexer's tag_re has no re.DOTALL,
# so a {# #} that wraps onto a second line is never tokenized as a comment and
# renders verbatim into the page (#145). Match the same way the lexer does — `.`
# without DOTALL — so a span that reaches its #} only by crossing a newline is
# invisible here, exactly as it is to Django, and surfaces as an offender.
_SINGLE_LINE_COMMENT = re.compile(r"\{#.*?#\}")


def _templates_root():
    return Path(settings.TEMPLATES[0]["DIRS"][0])


def _vendored(name):
    return Path(settings.STATICFILES_DIRS[0]) / name


@pytest.fixture
def authed_client(client, django_user_model):
    # `/` is now the login-gated dashboard (#101); these layout guards need an
    # authenticated member to render the base template instead of a login 302.
    user = django_user_model.objects.create_user(
        email="styling@example.com", password="x"
    )
    client.force_login(user)
    return client


def test_base_layout_links_vendored_bulma(authed_client):
    resp = authed_client.get("/")
    assert resp.status_code == 200
    # STATIC_URL is "static/", so the rendered href is /static/vendor/bulma.min.css.
    assert b"/static/vendor/bulma.min.css" in resp.content


def test_no_external_stylesheet_cdn_at_request_time(authed_client):
    resp = authed_client.get("/")
    body = resp.content
    # No known CDN host anywhere in the document.
    for needle in (b"cdn.jsdelivr", b"cdnjs", b"unpkg.com", b"cdn.bulma"):
        assert needle not in body
    # Assets ({% static %}) load from relative /static, never over http. Scope
    # the http check to <head> so an external link in the body later (docs,
    # support) doesn't trip this no-CDN guard.
    head = body.split(b"</head>", 1)[0]
    assert b'href="http' not in head


def test_vendored_bulma_present_and_real(client):
    path = _vendored(BULMA_REL)
    assert path.exists(), f"missing vendored Bulma at {path}"
    text = path.read_text(encoding="utf-8")
    # A real prebuilt Bulma stylesheet is large and defines its core selectors.
    assert len(text) > 100_000
    assert ".button" in text
    assert ".navbar" in text


def test_base_layout_wraps_content_in_bulma_container(authed_client):
    resp = authed_client.get("/")
    assert b"container" in resp.content


def test_login_button_uses_bulma_button_class(client):
    resp = client.get("/accounts/login/")
    assert resp.status_code == 200
    assert b'class="button' in resp.content


def test_no_multiline_hash_comments_in_templates():
    """Every {# #} comment must open and close on one line, or it leaks (#145).

    Walks the whole template tree. For each `{#`, a single-line `{# … #}` match
    must start there; if it doesn't, the `#}` is on a later line (or missing) and
    Django would render the comment text into the page. Multi-line notes belong
    in {% comment %}…{% endcomment %}, which is multi-line-safe.
    """
    offenders = []
    for path in sorted(_templates_root().rglob("*.html")):
        text = path.read_text(encoding="utf-8")
        for hit in re.finditer(r"\{#", text):
            if not _SINGLE_LINE_COMMENT.match(text, hit.start()):
                line = text.count("\n", 0, hit.start()) + 1
                rel = path.relative_to(_templates_root())
                offenders.append(f"{rel}:{line}")
    assert not offenders, (
        "Multi-line {# #} comments leak into rendered HTML; use "
        "{% comment %}…{% endcomment %} instead:\n  " + "\n  ".join(offenders)
    )
