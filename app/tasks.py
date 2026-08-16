"""
Tareas Celery: la capa de orquestación del motor de distribución.

Decisión de diseño central del proyecto: este módulo es el ÚNICO que conoce
tanto a Celery como a los publishers. La tarea `publish_job`:
  1. Persiste cada cambio de estado del job en la base (queued -> processing
     -> published/failed), como pide el spec.
  2. Llama al publisher (una función pura, ver app/publishers/fake.py) y
     traduce las excepciones tipadas que puede lanzar en decisiones de
     infraestructura: reintentar con backoff exponencial (TransientError) o
     mandar a la dead-letter queue (PermanentError, o TransientError que
     agotó sus reintentos).
El publisher no sabe nada de esto; si mañana cambia la política de retries
(por ejemplo, 5 intentos en vez de 3), se toca este archivo y ningún otro.
"""

from app.celery_app import celery_app
from app.config import settings
from app.db import SessionLocal
from app.exceptions import PermanentError, TransientError
from app.models import Job, JobStatus
from app.publishers import fake as fake_publisher


def _mark_failed_and_deadletter(db, job: Job, error: Exception) -> None:
    """
    Transición común a los dos caminos que terminan en dead-letter:
    error permanente, o error transitorio que agotó sus reintentos.
    Queda en un solo lugar para que ambos caminos persistan y enruten
    exactamente igual.
    """
    job.status = JobStatus.FAILED
    job.error_message = str(error)
    db.commit()

    handle_dead_letter.apply_async(args=[job.id, str(error)])


@celery_app.task(bind=True, max_retries=settings.max_retries)
def publish_job(self, job_id: int) -> None:
    db = SessionLocal()
    try:
        job = db.get(Job, job_id)
        if job is None:
            # El job no existe (por ejemplo, se borró de la base a mano).
            # No hay nada que reintentar ni dónde persistir un error.
            return

        job.status = JobStatus.PROCESSING
        job.attempts += 1
        db.commit()

        try:
            fake_publisher.publish(job.platform, job.payload)
        except TransientError as exc:
            job.error_message = str(exc)
            db.commit()

            # Chequeamos el límite ANTES de llamar a self.retry(): es más
            # explícito y evita depender de capturar MaxRetriesExceededError,
            # cuya semántica exacta (qué excepción llega hasta acá) varía
            # entre ejecución normal y modo eager. self.request.retries es
            # la cantidad de reintentos ya realizados para esta tarea.
            if self.request.retries >= self.max_retries:
                # Se agotaron los reintentos: un error transitorio persistente
                # se trata como permanente y se manda a la dead-letter queue.
                _mark_failed_and_deadletter(db, job, exc)
            else:
                # Backoff exponencial: intento 0 -> 1s, intento 1 -> 2s,
                # intento 2 -> 4s (con retry_backoff_base=2, el default).
                countdown = settings.retry_backoff_base**self.request.retries
                self.retry(exc=exc, countdown=countdown)
        except PermanentError as exc:
            # Errores permanentes no se reintentan nunca: van directo a DLQ.
            _mark_failed_and_deadletter(db, job, exc)
        else:
            job.status = JobStatus.PUBLISHED
            db.commit()
    finally:
        db.close()


@celery_app.task
def handle_dead_letter(job_id: int, reason: str) -> None:
    """
    Tarea que recibe los jobs enrutados a la cola "dlq".

    El job ya quedó persistido como FAILED por publish_job antes de llegar
    acá; esta tarea es el punto de extensión para lo que se quiera hacer
    con errores permanentes más adelante (alertar, notificar a un humano,
    reintentar manualmente vía un endpoint, etc.). Por ahora no hace nada
    más que existir como destino explícito de la cola "dlq", separado del
    procesamiento normal.
    """
    return None
