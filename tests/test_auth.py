"""
Tests for app/auth.py's password hashing and session token helpers (Phase
28). Pure functions, no DB/network involved.
"""

from app.auth import create_session_token, hash_password, verify_password, verify_session_token


class TestPasswordHashing:
    def test_verify_password_roundtrip(self):
        hashed = hash_password("correct horse battery staple")
        assert verify_password("correct horse battery staple", hashed) is True

    def test_verify_password_rejects_wrong_password(self):
        hashed = hash_password("correct horse battery staple")
        assert verify_password("wrong password", hashed) is False

    def test_hash_password_is_never_plaintext(self):
        hashed = hash_password("hunter2")
        assert hashed != "hunter2"
        assert "hunter2" not in hashed

    def test_hash_password_is_salted_and_unique_per_call(self):
        assert hash_password("same password") != hash_password("same password")

    def test_verify_password_returns_false_for_malformed_hash(self):
        assert verify_password("anything", "not-a-real-bcrypt-hash") is False


class TestSessionTokens:
    def test_create_and_verify_roundtrip(self):
        token = create_session_token(42)
        assert verify_session_token(token) == 42

    def test_tampered_token_is_rejected(self):
        token = create_session_token(42)
        tampered = token[:-1] + ("a" if token[-1] != "a" else "b")
        assert verify_session_token(tampered) is None

    def test_garbage_token_is_rejected(self):
        assert verify_session_token("not-a-token-at-all") is None

    def test_empty_token_is_rejected(self):
        assert verify_session_token("") is None
