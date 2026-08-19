"""It's like a fake YouTube: it simulates uploading the data, 6 out of 10
succeed, 3 come back with "I'm overloaded", and 1 errors out. It's basically
a simulator to use while I don't have YouTube connected yet."""
"""
Fake publisher, used to develop and test the engine without depending on
any real social network API yet.

Key design decision of this project: a publisher is a pure function.
- It doesn't know about Celery: it has no idea what a retry, a queue, or a
  database job_id is. It only receives the minimum data needed to
  "publish" and returns a result or raises a typed exception.
- It doesn't print anything to screen: if logging were needed, that would
  be the caller's responsibility (the Celery task), not the publisher's.
This makes it trivial to test the publisher in a plain unit test (without
spinning up Redis or a worker), and means that the day a real publisher
gets added (app/publishers/twitter.py, for example), it will have exactly
the same shape: publish(platform, payload) -> dict | raise
TransientError/PermanentError.
"""

import random

from app.exceptions import PermanentError, TransientError

# Fixed probabilities per the spec: 30% transient error (429),
# 10% permanent error (400), 60% success.
_PROBABILITY_TRANSIENT = 0.30
_PROBABILITY_PERMANENT = 0.10


def publish(platform: str, payload: dict, account_credentials: dict | None = None) -> dict:
    """
    Simulates publishing `payload` on `platform`.

    Returns a dict with the simulated result if it "publishes" successfully.
    Raises TransientError (simulating HTTP 429) or PermanentError (simulating
    HTTP 400) based on the random roll, so the rest of the system's
    retry/backoff and dead-letter queue logic can be exercised.

    account_credentials is accepted for signature parity with the other
    publishers (see app/tasks.py, which resolves it uniformly for every
    platform) but unused here: the fake publisher never checks credentials.
    """
    roll = random.random()

    if roll < _PROBABILITY_TRANSIENT:
        raise TransientError(f"429 Too Many Requests: rate limit reached on {platform}")

    if roll < _PROBABILITY_TRANSIENT + _PROBABILITY_PERMANENT:
        raise PermanentError(f"400 Bad Request: invalid payload for {platform}")

    return {"platform": platform, "external_id": f"fake-{random.randint(100000, 999999)}"}
