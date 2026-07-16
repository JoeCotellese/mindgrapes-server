"""Health view — the walking skeleton's liveness probe.

The site root is now the login-gated Brain dashboard (openbrain.brain, #101); its
auth gate is covered in openbrain/brain/tests/unit/test_views.py.
"""

import pytest
from django.urls import reverse

pytestmark = pytest.mark.django_db


def test_health_endpoint(client):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.content == b"ok"


def test_health_url_name_resolves(client):
    resp = client.get(reverse("health"))
    assert resp.status_code == 200
    assert resp.content == b"ok"
