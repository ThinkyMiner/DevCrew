"""Static-serving smoke tests for the buildless web frontend (Task 6.1).

The frontend is hard to unit-test in Python; these assert the wiring only:

* ``GET /`` returns the SPA shell (200, ``text/html``) carrying a known marker.
* ``GET /static/...`` serves a real asset from ``app/web``.
* The static mount does NOT shadow the REST/health routes (regression guard for
  the ``app.mount`` ordering in the composition root).
"""

from __future__ import annotations


def test_index_serves_html_shell(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    # Marker present in app/web/index.html (the JS bootstrap mount point).
    assert "team-app" in resp.text


def test_static_assets_served(client):
    css = client.get("/static/css/app.css")
    assert css.status_code == 200
    assert css.headers["content-type"].startswith("text/css")

    js = client.get("/static/js/main.js")
    assert js.status_code == 200
    # ES module served as JS (content-type varies by platform; just confirm body).
    assert js.text


def test_static_mount_does_not_shadow_api(client):
    # The mount is added LAST; explicit routes/routers must still win.
    assert client.get("/health").status_code == 200
    assert client.get("/rooms").status_code == 200
    # A missing static file 404s (not a 200 SPA fallback) — assets are explicit.
    assert client.get("/static/does-not-exist.js").status_code == 404
