# distribution-engine

Motor de distribución de contenido a redes sociales basado en colas (Celery + Redis),
con jobs persistidos en base de datos (SQLAlchemy) y una state machine simple:

```
scheduled -> queued -> processing -> published
                                   -> failed  (agotó reintentos, o error permanente -> dead-letter queue)
```

`scheduled` es opcional: los jobs urgentes o creados sin `--schedule` arrancan directo en `queued`.

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

Necesitás 4 terminales, todas paradas en la raíz del proyecto con el venv activado.

**1. Worker de Celery** (procesa la cola de prioridad, la cola normal y la dead-letter queue;
el orden en `-Q` es el orden de preferencia de consumo):

```bash
celery -A app.celery_app worker --loglevel=info -Q priority,celery,dlq
```

**2. Beat de Celery** (dispara `dispatch_due_jobs` cada 60s para despachar los jobs
`scheduled` que ya llegaron a su horario):

```bash
celery -A app.celery_app beat --loglevel=info
```

**3. Crear y encolar jobs de prueba:**

```bash
python -m scripts.enqueue_demo                        # 10 jobs, inmediato, cola normal (default)
python -m scripts.enqueue_demo --schedule              # jobs en estado scheduled, a su próximo horario
python -m scripts.enqueue_demo --urgent 3              # los primeros 3 van inmediato por la cola priority
python -m scripts.enqueue_demo --schedule --urgent 3   # 3 urgentes ahora, los otros 7 scheduled
```

Esto crea las tablas en Postgres si no existen e inserta 10 jobs de prueba.

**4. Ver el resultado:**

El publisher fake falla al azar (30% HTTP 429 transitorio, 10% HTTP 400 permanente,
60% éxito), así que vas a ver en los logs del worker una mezcla de:

- Jobs publicados al primer intento.
- Jobs que reintentan con backoff exponencial (1s, 2s, 4s) tras un 429 y terminan
  publicados o, si agotan los 3 reintentos, en `failed` + dead-letter.
- Jobs con un 400 que van directo a `failed` + dead-letter, sin reintentar.

Para inspeccionar el estado final en la base, sin abrir Neon:

```bash
python -m scripts.show_jobs
```

Muestra id, platform, status, attempts y scheduled_at en una tabla — útil para ver
jobs `scheduled` esperando su horario, o confirmar que `dispatch_due_jobs` los pasó a `queued`.

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
| `R2_ENDPOINT_URL` | *(vacío, opcional)* | Endpoint S3-compatible del bucket de Cloudflare R2 |
| `R2_ACCESS_KEY_ID` | *(vacío, opcional)* | Access key de un API token de R2 |
| `R2_SECRET_ACCESS_KEY` | *(vacío, opcional)* | Secret key del mismo API token de R2 |
| `R2_BUCKET_NAME` | *(vacío, opcional)* | Nombre del bucket de R2 |
| `TIME_SLOTS` | *(vacío, usa los defaults)* | Override de horarios por plataforma, formato `twitter=09:00,13:00,18:00;tiktok=12:00,19:00` |

Defaults de `TIME_SLOTS` (ver `PLATFORM_TIME_SLOTS` en `app/config.py`): twitter
09:00/13:00/18:00, tiktok 12:00/19:00, youtube 15:00, cualquier otra plataforma 12:00.
Son horarios naive en la hora local del proceso, sin soporte de timezone por
cuenta/plataforma todavía (ver el comentario en `app/config.py`).

`app/storage.py` (subida, URL firmada y borrado de media en R2) todavía no está conectado
a ningún publisher; probalo de forma aislada con `python -m scripts.test_storage` una vez
que tengas las credenciales. Ver `CLAUDE.md` (Fase 3) para el checklist completo.

## Dashboard de monitoreo (extra, fuera del spec)

`dashboard/` es un panel de solo lectura sobre el estado de los jobs (salvo
por la acción de reintentar un job `failed`). No modifica ni depende de
lógica nueva en `app/` — solo lee de la misma base y usa `publish_job.delay`
para reintentar.

```bash
uvicorn dashboard.api:app --reload --port 8000
```

Abrí `http://localhost:8000` — sirve el frontend (`dashboard/static/index.html`)
y expone la API en `/api/jobs`, `/api/stats` y `POST /api/jobs/{id}/retry`
(esta última solo funciona sobre jobs en estado `failed`; devuelve 409 en
cualquier otro caso). No requiere el worker de Celery corriendo para mostrar
datos, pero sí para que un retry se procese de verdad.

## Qué falta (a propósito, fuera de alcance de esta etapa)

- Publishers reales para cada red social (hoy solo existe YouTube además del fake).
- Migraciones reales con Alembic (hoy `init_db()` usa `create_all`, alcanza mientras el esquema es chico).
- Deploy en Fly.io (todavía no implementado).
- Conectar `app/storage.py` (R2) a los publishers cuando exista un flujo real de media.
- Timezone real por cuenta/plataforma para el scheduling (hoy es naive local time, ver `app/config.py`).
