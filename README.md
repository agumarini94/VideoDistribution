# distribution-engine

Motor de distribución de contenido a redes sociales basado en colas (Celery + Redis),
con jobs persistidos en base de datos (SQLAlchemy) y una state machine simple:

```
queued -> processing -> published
                      -> failed  (agotó reintentos, o error permanente -> dead-letter queue)
```

## Diseño

- `app/publishers/fake.py` es un **publisher puro**: no conoce Celery, no imprime nada.
  Solo devuelve un resultado o lanza `TransientError` / `PermanentError` (`app/exceptions.py`).
- `app/tasks.py` es la única pieza que conoce tanto a Celery como a los publishers.
  Decide qué hacer con cada excepción: reintentar con backoff exponencial
  (errores transitorios, ej. HTTP 429/5xx) o enrutar a la dead-letter queue
  (errores permanentes, ej. HTTP 400, o transitorios que agotaron sus reintentos).
- Cada cambio de estado del `Job` se persiste en la base antes de continuar.

Esta separación permite agregar publishers reales (Twitter, LinkedIn, etc.) más
adelante sin tocar la lógica de retry/DLQ, y testear los publishers sin
levantar Redis ni un worker.

## Stack

- Python 3.11+
- Celery (broker: Redis)
- SQLAlchemy + PostgreSQL (Neon)

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env  # completar DATABASE_URL con tu connection string de Neon
```

Redis debe estar corriendo en `localhost:6379`. `DATABASE_URL` es obligatoria: la app
falla al arrancar si no está seteada (ver `app/config.py`).

## Correr todo

Necesitás 3 terminales, todas paradas en la raíz del proyecto con el venv activado.

**1. Worker de Celery** (procesa la cola por defecto y la dead-letter queue):

```bash
celery -A app.celery_app worker --loglevel=info -Q celery,dlq
```

**2. Crear y encolar 10 jobs de prueba:**

```bash
python -m scripts.enqueue_demo
```

Esto crea las tablas en Postgres si no existen, inserta 10 jobs en estado `queued`
y los despacha con `publish_job.delay(job.id)`.

**3. Ver el resultado:**

El publisher fake falla al azar (30% HTTP 429 transitorio, 10% HTTP 400 permanente,
60% éxito), así que vas a ver en los logs del worker una mezcla de:

- Jobs publicados al primer intento.
- Jobs que reintentan con backoff exponencial (1s, 2s, 4s) tras un 429 y terminan
  publicados o, si agotan los 3 reintentos, en `failed` + dead-letter.
- Jobs con un 400 que van directo a `failed` + dead-letter, sin reintentar.

Para inspeccionar el estado final en la base:

```bash
python -c "
from app.db import SessionLocal
from app.models import Job
db = SessionLocal()
for job in db.query(Job).order_by(Job.id):
    print(job.id, job.platform, job.status.value, job.attempts, job.error_message)
"
```

## Variables de entorno

Ver `app/config.py` / `.env.example`:

| Variable | Default | Descripción |
|---|---|---|
| `REDIS_URL` | `redis://localhost:6379/0` | Redis usado como broker y result backend |
| `DATABASE_URL` | *(sin default, obligatoria)* | Connection string de Postgres (Neon) |
| `MAX_RETRIES` | `3` | Reintentos máximos ante error transitorio |
| `RETRY_BACKOFF_BASE` | `2` | Base del backoff exponencial (segundos) |
| `DLQ_QUEUE_NAME` | `dlq` | Nombre de la cola de dead-letter |
| `ALERT_WEBHOOK_URL` | *(vacío, opcional)* | Webhook de Discord o Slack para alertas de dead-letter; si está vacío, no se envían alertas |

## Qué falta (a propósito, fuera de alcance de esta etapa)

- Publishers reales para cada red social (hoy solo existe YouTube además del fake).
- Migraciones reales con Alembic (hoy `init_db()` usa `create_all`, alcanza mientras el esquema es chico).
- Deploy en Fly.io y almacenamiento de media en Cloudflare R2 (decisiones de stack para la Fase 2a, ver `CLAUDE.md`; todavía no implementadas).
