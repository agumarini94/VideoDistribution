"""
Password hashing and signed session tokens for dashboard user accounts
(Phase 28: self-registration + admin approval, see CLAUDE.md).

Kept in app/ rather than dashboard/ because it's User-domain logic — the
User model itself lives in app/models.py for the same reason — even though
today only dashboard/api.py calls it. dashboard/api.py still owns every
HTTP-layer decision (cookie name/flags, which routes require a session,
middleware wiring); this module only knows about passwords and tokens, the
same "pure helper, caller owns policy" split as app/publishers/*.py.
"""

import base64
import logging
import secrets

import bcrypt
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import settings

logger = logging.getLogger(__name__)

# How long a session cookie stays valid, enforced both here (signature
# verification rejects an older token outright) and by the cookie's own
# Max-Age (dashboard/api.py) so an expired cookie also stops being sent.
SESSION_MAX_AGE_SECONDS = 7 * 24 * 3600

# itsdangerous salts the HMAC so this module's tokens can never be replayed
# against some other signer that happens to reuse the same secret key.
_SESSION_SALT = "distribution-engine-dashboard-session"

if settings.session_secret_key:
    _SESSION_SECRET_KEY = settings.session_secret_key
else:
    # Dev-convenience fallback, same spirit as dashboard/api.py's unset
    # DASHBOARD_USERNAME/PASSWORD warning: the app still runs, but every
    # session is invalidated on the next process restart (a new random key
    # is generated each time), and two worker processes sharing no key
    # would reject each other's cookies. Never acceptable in production.
    _SESSION_SECRET_KEY = secrets.token_hex(32)
    logger.warning(
        "\n"
        + "!" * 78
        + "\nSESSION_SECRET_KEY not set: using an ephemeral, per-process random key.\n"
        "Every dashboard login session will be invalidated on the next restart, "
        "and\nmultiple processes (e.g. a multi-machine deploy) won't recognize "
        "each other's\nsession cookies. Set SESSION_SECRET_KEY in production.\n"
        + "!" * 78
    )

_serializer = URLSafeTimedSerializer(_SESSION_SECRET_KEY, salt=_SESSION_SALT)

# Signs the OAuth "state" param for in-browser platform-connect flows (Phase
# 29a: YouTube self-service onboarding, dashboard/api.py's
# /api/oauth/youtube/start + /callback). A separate salt from the session
# token above means the two are cryptographically distinct even though they
# share _SESSION_SECRET_KEY — a session cookie can't be replayed as OAuth
# state or vice versa. Short-lived (10 minutes) since it only needs to
# survive one round trip through Google's consent screen, not a login
# session.
_OAUTH_STATE_SALT = "distribution-engine-oauth-state"
_oauth_state_serializer = URLSafeTimedSerializer(_SESSION_SECRET_KEY, salt=_OAUTH_STATE_SALT)
OAUTH_STATE_MAX_AGE_SECONDS = 600


def hash_password(password: str) -> str:
    """Never store plaintext — bcrypt's own random salt makes each hash unique."""
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, hashed: str) -> bool:
    """
    True only for a matching password. Returns False (rather than raising)
    for a malformed/corrupt stored hash — that's a data problem, not a
    reason to 500 a login attempt.
    """
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed.encode("utf-8"))
    except ValueError:
        return False


def _is_canonical_base64url(segment: str) -> bool:
    """
    True iff `segment` is the *canonical* URL-safe base64 encoding (no
    padding) of the bytes it decodes to, i.e. re-encoding those bytes
    reproduces `segment` exactly.

    Needed because itsdangerous signs with HMAC-SHA1 (a 20-byte digest) and
    base64url-encodes each token segment the standard way
    (base64.urlsafe_b64encode(...).rstrip(b"=")). 20 isn't a multiple of 3,
    so the final base64 group encodes only 4 real bits in its last
    character, alongside 2 bits that the encoder always sets to zero but
    that Python's base64 decoder never validates on the way back in — it
    just discards them. That means several distinct last characters decode
    to byte-identical output, so several distinct *token strings* carry the
    exact same signature bytes. It's not an HMAC forgery (nobody can derive
    a valid signature for different content this way), but it does mean a
    single-character tamper of a token's trailing character isn't
    guaranteed to change what it decodes to, so itsdangerous's signature
    check can't be relied on alone to reject it. Rejecting any non-canonical
    encoding up front closes that gap.
    """
    padded = segment + "=" * (-len(segment) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, TypeError):
        return False
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii") == segment


def _has_canonical_encoding(token: str) -> bool:
    """Every dot-separated segment of a token must be canonically encoded — see _is_canonical_base64url."""
    return bool(token) and all(_is_canonical_base64url(part) for part in token.split("."))


def create_session_token(user_id: int) -> str:
    """Signed, timestamped token encoding which User this session belongs to."""
    return _serializer.dumps({"user_id": user_id})


def verify_session_token(token: str) -> int | None:
    """
    Returns the encoded user_id if the token's signature is valid and it
    hasn't exceeded SESSION_MAX_AGE_SECONDS, else None — a tampered,
    expired, or otherwise malformed token is treated as "not logged in"
    rather than raising, since the caller (dashboard/api.py's auth
    middleware) always wants a clean fallback to anonymous.
    """
    if not _has_canonical_encoding(token):
        return None
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict):
        return None
    user_id = data.get("user_id")
    return user_id if isinstance(user_id, int) else None


def create_oauth_state_token(data: dict) -> str:
    """Signed, timestamped token carrying arbitrary data (e.g. client_id) through an external OAuth redirect."""
    return _oauth_state_serializer.dumps(data)


def verify_oauth_state_token(token: str) -> dict | None:
    """
    Returns the encoded dict if the token's signature is valid and it hasn't
    exceeded OAUTH_STATE_MAX_AGE_SECONDS, else None — same "clean fallback,
    never raise" contract as verify_session_token, since the caller (an
    OAuth callback route) always wants to treat a tampered/expired/malformed
    state as a rejected request, not a 500.
    """
    if not _has_canonical_encoding(token):
        return None
    try:
        data = _oauth_state_serializer.loads(token, max_age=OAUTH_STATE_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    return data if isinstance(data, dict) else None
