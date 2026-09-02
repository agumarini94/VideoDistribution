"""
Tests for scripts/authorize_meta.py (Phase 23): the full authorize chain
(happy path, no-linked-Instagram warning, multi-Page selection) and the
_choose_page prompt helper. exchange_code_for_user_token/
exchange_long_lived_token/list_pages/get_instagram_business_account are
monkeypatched (as imported into scripts.authorize_meta's namespace) so no
real HTTP or browser/local-server interaction happens — this exercises
_run_authorization directly, the same split main() uses after receiving a
valid OAuth callback.
"""

import pytest

from app.models import Account
from scripts import authorize_meta


ONE_PAGE = [{"id": "page-1", "name": "Only Page", "access_token": "page-tok-1"}]
TWO_PAGES = [
    {"id": "page-1", "name": "First Page", "access_token": "page-tok-1"},
    {"id": "page-2", "name": "Second Page", "access_token": "page-tok-2"},
]


@pytest.fixture(autouse=True)
def mock_oauth_chain(monkeypatch):
    monkeypatch.setattr(
        authorize_meta,
        "exchange_code_for_user_token",
        lambda code, redirect_uri: {"access_token": "short-tok", "expires_in": 3600},
    )
    monkeypatch.setattr(
        authorize_meta,
        "exchange_long_lived_token",
        lambda short_lived_token: {"access_token": "long-user-tok", "expires_at": "2026-11-01T00:00:00+00:00"},
    )


def _accounts(db_session, platform):
    return db_session.query(Account).filter(Account.platform == platform).all()


class TestChoosePage:
    def test_single_page_returned_without_prompting(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt: (_ for _ in ()).throw(AssertionError("should not prompt")))
        assert authorize_meta._choose_page(ONE_PAGE) == ONE_PAGE[0]

    def test_multiple_pages_prompts_and_returns_choice(self, monkeypatch):
        monkeypatch.setattr("builtins.input", lambda prompt: "1")
        assert authorize_meta._choose_page(TWO_PAGES) == TWO_PAGES[1]

    def test_invalid_choice_reprompts(self, monkeypatch):
        answers = iter(["not-a-number", "5", "0"])
        monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
        assert authorize_meta._choose_page(TWO_PAGES) == TWO_PAGES[0]


class TestRunAuthorizationHappyPath:
    def test_creates_facebook_and_instagram_accounts(self, db_session, monkeypatch):
        monkeypatch.setattr(authorize_meta, "list_pages", lambda user_token: ONE_PAGE)
        monkeypatch.setattr(authorize_meta, "get_instagram_business_account", lambda page_id, page_token: "ig-user-1")

        authorize_meta._run_authorization("auth-code", "https://example.com/callback", account_name=None)

        facebook_accounts = _accounts(db_session, "facebook")
        instagram_accounts = _accounts(db_session, "instagram")
        assert len(facebook_accounts) == 1
        assert len(instagram_accounts) == 1

        fb = facebook_accounts[0]
        assert fb.name == "Only Page"
        assert fb.credentials == {
            "page_id": "page-1",
            "page_token": "page-tok-1",
            "page_name": "Only Page",
            "user_token": "long-user-tok",
            "user_token_expires_at": "2026-11-01T00:00:00+00:00",
        }

        ig = instagram_accounts[0]
        assert ig.name == "Only Page"
        assert ig.credentials == {
            "ig_user_id": "ig-user-1",
            "page_id": "page-1",
            "page_token": "page-tok-1",
            "user_token": "long-user-tok",
            "user_token_expires_at": "2026-11-01T00:00:00+00:00",
        }

    def test_account_name_override(self, db_session, monkeypatch):
        monkeypatch.setattr(authorize_meta, "list_pages", lambda user_token: ONE_PAGE)
        monkeypatch.setattr(authorize_meta, "get_instagram_business_account", lambda page_id, page_token: None)

        authorize_meta._run_authorization("auth-code", "https://example.com/callback", account_name="Client X")

        facebook_accounts = _accounts(db_session, "facebook")
        assert len(facebook_accounts) == 1
        assert facebook_accounts[0].name == "Client X"
        # page_name in credentials still reflects the real Page name, only
        # the Account's own `name` column is overridden.
        assert facebook_accounts[0].credentials["page_name"] == "Only Page"

    def test_rerun_rotates_existing_accounts_in_place(self, db_session, monkeypatch):
        monkeypatch.setattr(authorize_meta, "list_pages", lambda user_token: ONE_PAGE)
        monkeypatch.setattr(authorize_meta, "get_instagram_business_account", lambda page_id, page_token: "ig-user-1")

        authorize_meta._run_authorization("auth-code", "https://example.com/callback", account_name=None)
        authorize_meta._run_authorization("auth-code", "https://example.com/callback", account_name=None)

        assert len(_accounts(db_session, "facebook")) == 1
        assert len(_accounts(db_session, "instagram")) == 1


class TestRunAuthorizationNoInstagramLinked:
    def test_only_creates_facebook_account_and_warns(self, db_session, monkeypatch, capsys):
        monkeypatch.setattr(authorize_meta, "list_pages", lambda user_token: ONE_PAGE)
        monkeypatch.setattr(authorize_meta, "get_instagram_business_account", lambda page_id, page_token: None)

        authorize_meta._run_authorization("auth-code", "https://example.com/callback", account_name=None)

        assert len(_accounts(db_session, "facebook")) == 1
        assert len(_accounts(db_session, "instagram")) == 0

        output = capsys.readouterr().out
        assert "WARNING" in output
        assert "Instagram Business" in output
        assert "Only Page" in output


class TestRunAuthorizationMultiplePages:
    def test_prompts_and_authorizes_the_chosen_page(self, db_session, monkeypatch):
        monkeypatch.setattr(authorize_meta, "list_pages", lambda user_token: TWO_PAGES)
        monkeypatch.setattr(authorize_meta, "get_instagram_business_account", lambda page_id, page_token: None)
        monkeypatch.setattr("builtins.input", lambda prompt: "1")

        authorize_meta._run_authorization("auth-code", "https://example.com/callback", account_name=None)

        facebook_accounts = _accounts(db_session, "facebook")
        assert len(facebook_accounts) == 1
        assert facebook_accounts[0].name == "Second Page"
        assert facebook_accounts[0].credentials["page_id"] == "page-2"

    def test_no_pages_returned_creates_nothing(self, db_session, monkeypatch, capsys):
        monkeypatch.setattr(authorize_meta, "list_pages", lambda user_token: [])

        authorize_meta._run_authorization("auth-code", "https://example.com/callback", account_name=None)

        assert _accounts(db_session, "facebook") == []
        assert "no Facebook Pages" in capsys.readouterr().out
