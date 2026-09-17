"""
Tests for Arscor's public marketing site (public_pages.py), mounted onto
the dashboard app at "/", "/terms", "/privacy" (dashboard/api.py). Uses
FastAPI's TestClient like tests/test_dashboard_auth.py, since the point
here is confirming these three paths are actually reachable and public
through the real app + middleware stack, not just that public_pages.py's
own functions return a string.
"""

from fastapi.testclient import TestClient

from dashboard import api as dashboard_api


def _client():
    return TestClient(dashboard_api.app)


class TestPublicPages:
    def test_landing_page_is_public_and_returns_html(self):
        resp = _client().get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        assert "Arscor" in resp.text

    def test_terms_page_is_public(self):
        resp = _client().get("/terms")
        assert resp.status_code == 200
        assert "Terms of Service" in resp.text

    def test_privacy_page_is_public(self):
        resp = _client().get("/privacy")
        assert resp.status_code == 200
        assert "Privacy Policy" in resp.text

    def test_landing_nav_links_point_at_the_spa_not_a_backend_login_page(self):
        # public_pages.py has no /login or /signup routes of its own — the
        # SPA at /dashboard is what actually renders Login/Register
        # (Phase 28). See dashboard/api.py's public_router comment and
        # public_pages.py::_page's docstring-comment for why.
        resp = _client().get("/")
        assert 'href="/dashboard"' in resp.text
        assert 'href="/dashboard?auth=signup"' in resp.text
        assert 'href="/login"' not in resp.text
        assert 'href="/signup"' not in resp.text

    def test_dashboard_spa_still_mounted_and_public(self):
        # Moved from "/" to "/dashboard" alongside the public site landing
        # here — see tests/test_dashboard_auth.py::TestPublicPaths for the
        # full public-path/auth-gating assertions.
        resp = _client().get("/dashboard")
        assert resp.status_code == 200
