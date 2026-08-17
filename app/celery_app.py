"""
Celery is a Python library for running tasks in the background, outside of
the main program.

Celery app instance.

Design decision: the Celery app lives in its own module, separate from
tasks.py. That way, `celery -A app.celery_app worker` doesn't need to import
the tasks first (avoids cycles), and any script that just needs to dispatch
tasks (like scripts/enqueue_demo.py) can import app.tasks without surprises.
"""

from celery import Celery

from app.config import settings

celery_app = Celery(
    "distribution_engine",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["app.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    # Explicitly route the dead-letter task to its own queue. A normal
    # worker (`-Q celery`) never processes it; a worker dedicated to the
    # "dlq" queue (or the same worker listening to both queues) is needed
    # for someone to handle permanent errors.
    task_routes={
        "app.tasks.handle_dead_letter": {"queue": settings.dlq_queue_name},
    },
)
