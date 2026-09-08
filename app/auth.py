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
    try:
        data = _serializer.loads(token, max_age=SESSION_MAX_AGE_SECONDS)
    except (BadSignature, SignatureExpired):
        return None
    if not isinstance(data, dict):
        return None
    user_id = data.get("user_id")
    return user_id if isinstance(user_id, int) else None
