"""
Typed exceptions for the publication domain.

Design decision: these exceptions are the "contract" between publishers and
the orchestration layer (Celery). A publisher never decides whether
something gets retried; it only classifies the error it hit. Whoever
decides what to do with that classification is app/tasks.py. This enables:
  - Testing publishers without Celery (they are pure functions that can
    only return a result or raise one of these two exceptions).
  - Adding real publishers (Twitter, LinkedIn, etc.) later without touching
    the retry/DLQ logic: it's enough for them to map their own HTTP errors
    to TransientError or PermanentError.
"""


class PublishError(Exception):
    """Base class for any publication error."""


class TransientError(PublishError):
    """
    Temporary error (e.g. HTTP 429 Too Many Requests, HTTP 5xx from the
    provider).

    Retrying later is assumed to possibly fix the problem, which is why the
    Celery task applies exponential backoff for this kind of error.
    """


class PermanentError(PublishError):
    """
    Permanent error (e.g. HTTP 400 Bad Request: invalid payload, revoked
    credentials, nonexistent platform).

    Retrying doesn't change the outcome, which is why the Celery task routes
    it straight to the dead-letter queue without spending retries.
    """
