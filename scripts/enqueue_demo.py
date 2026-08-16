"""
Script de demo: crea 10 jobs de prueba y los encola para publicación.

Se ejecuta desde la raíz del proyecto con:
    python -m scripts.enqueue_demo

Decisión de diseño: este script vive fuera de app/ porque no es parte del
motor en sí, es una herramienta de operación/demo. Usa las mismas piezas
(Job, SessionLocal, publish_job) que usaría cualquier otro punto de entrada
real (una API HTTP, por ejemplo), para probar el flujo end-to-end.
"""

from app.db import SessionLocal, init_db
from app.models import Job, JobStatus
from app.tasks import publish_job

PLATFORMS = ["twitter", "instagram", "linkedin", "facebook"]


def build_demo_payload(index: int) -> dict:
    return {
        "text": f"Post de prueba #{index}",
        "hashtags": ["demo", "distribution-engine"],
    }


def main() -> None:
    # Idempotente: si las tablas ya existen no hace nada.
    init_db()

    db = SessionLocal()
    try:
        jobs = []
        for i in range(10):
            job = Job(
                platform=PLATFORMS[i % len(PLATFORMS)],
                payload=build_demo_payload(i),
                status=JobStatus.QUEUED,
            )
            db.add(job)
            jobs.append(job)

        db.commit()

        # Recién acá tenemos los ids (autoincrement), así que encolamos
        # después del commit.
        for job in jobs:
            publish_job.delay(job.id)

        print(f"Se crearon y encolaron {len(jobs)} jobs de prueba.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
