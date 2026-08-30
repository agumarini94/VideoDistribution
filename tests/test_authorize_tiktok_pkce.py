"""
Tests for _generate_pkce_pair() in scripts/authorize_tiktok.py.

Covers the charset/length of the verifier and the deliberate RFC 7636
deviation documented in that function: TikTok's Login Kit for Desktop wants
the challenge as the raw hex digest of SHA-256(verifier), not the standard
BASE64URL(SHA256(verifier)) — this locks that behavior in so it doesn't get
"corrected" back to base64url later.
"""

import base64
import hashlib
import re

from scripts.authorize_tiktok import _generate_pkce_pair

_URLSAFE_CHARSET = re.compile(r"^[A-Za-z0-9_-]+$")
_HEX_CHARSET = re.compile(r"^[0-9a-f]+$")


class TestGeneratePkcePair:
    def test_verifier_charset_is_urlsafe(self):
        verifier, _ = _generate_pkce_pair()
        assert _URLSAFE_CHARSET.match(verifier)

    def test_verifier_length_within_rfc7636_range(self):
        # RFC 7636 requires the verifier to be 43-128 characters.
        verifier, _ = _generate_pkce_pair()
        assert 43 <= len(verifier) <= 128

    def test_challenge_is_hex_sha256_of_verifier(self):
        verifier, challenge = _generate_pkce_pair()
        assert challenge == hashlib.sha256(verifier.encode("ascii")).hexdigest()
        assert _HEX_CHARSET.match(challenge)
        assert len(challenge) == 64

    def test_challenge_is_not_standard_base64url_form(self):
        # Deliberate deviation from RFC 7636 (see the function's docstring):
        # TikTok wants hex, not BASE64URL(SHA256(verifier)). Locking this in
        # so the "standard" form doesn't get restored by mistake.
        verifier, challenge = _generate_pkce_pair()
        standard_challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).rstrip(b"=").decode("ascii")
        assert challenge != standard_challenge

    def test_pairs_are_not_reused_across_calls(self):
        verifier_a, challenge_a = _generate_pkce_pair()
        verifier_b, challenge_b = _generate_pkce_pair()
        assert verifier_a != verifier_b
        assert challenge_a != challenge_b
