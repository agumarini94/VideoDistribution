"""
Instancia de Celery.

Decisión de diseño: la app de Celery vive en su propio módulo, separada de
tasks.py. Así, `celery -A app.celery_app worker` no necesita importar primero
las tareas (evita ciclos) y cualquier script que solo necesite despachar
tareas (como scripts/enqueue_demo.py) puede importar app.tasks sin sorpresas.
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
    # Enrutamos explícitamente la tarea de dead-letter a su propia cola.
    # Un worker normal (`-Q celery`) nunca la procesa; hace falta un worker
    # dedicado a la cola "dlq" (o el mismo worker escuchando ambas colas)
    # para que alguien se haga cargo de los errores permanentes.
    task_routes={
        "app.tasks.handle_dead_letter": {"queue": settings.dlq_queue_name},
    },
)
