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
| `X_API_KEY` | *(vacío, opcional)* | Consumer key de la app de X (Twitter), nivel app, no por cuenta |
| `X_API_SECRET` | *(vacío, opcional)* | Consumer secret de la app de X, nivel app |
| `X_ACCESS_TOKEN` | *(vacío, opcional)* | Access token de fallback para jobs sin `account_id` (modo single-account) |
| `X_ACCESS_TOKEN_SECRET` | *(vacío, opcional)* | Access token secret de fallback, mismo caso que arriba |

Defaults de `TIME_SLOTS` (ver `PLATFORM_TIME_SLOTS` en `app/config.py`): twitter
09:00/13:00/18:00, tiktok 12:00/19:00, youtube 15:00, cualquier otra plataforma 12:00.
Son horarios naive en la hora local del proceso, sin soporte de timezone por
cuenta/plataforma todavía (ver el comentario en `app/config.py`).

`app/storage.py` (subida, URL firmada y borrado de media en R2) todavía no está conectado
a ningún publisher; probalo de forma aislada con `python -m scripts.test_storage` una vez
que tengas las credenciales. Ver `CLAUDE.md` (Fase 3) para el checklist completo.

`app/publishers/twitter.py` (X API v2, posteo de texto) tampoco fue probado contra la
API real todavía — faltan las credenciales del cliente. Ver `CLAUDE.md` (Fase 6) para
el checklist completo, incluida la migración manual de la tabla `jobs` (no hay Alembic
todavía, así que `account_id` no aparece solo con `init_db()` en una base ya existente).

Para dar de alta o actualizar una cuenta (multi-account, Fase 6):

```bash
python -m scripts.add_account --platform twitter --name "Cuenta principal" \
    access_token=xxx access_token_secret=yyy
```

### YouTube multi-account (Fase 7)

`app/publishers/youtube.py` ahora soporta credenciales por cuenta, igual que
twitter.py: si el job tiene `account_id`, arma las credenciales OAuth2 desde
`Account.credentials` en vez de leer `token.json`. El JSON de credenciales
(tanto en `Account.credentials` como en `token.json`) tiene el mismo shape
que devuelve `Credentials.to_json()` de Google:

```json
{
  "token": "...",
  "refresh_token": "...",
  "token_uri": "https://oauth2.googleapis.com/token",
  "client_id": "...",
  "client_secret": "...",
  "scopes": ["https://www.googleapis.com/auth/youtube.upload"]
}
```

Para dar de alta una cuenta de YouTube (corre el flujo OAuth interactivo y
guarda el resultado en una fila `Account` en vez de `token.json`):

```bash
python -m scripts.authorize_youtube --account "Canal cliente X"
```

Sin `--account`, el comportamiento es el de siempre (single-account, escribe
`token.json`).

Como los publishers son funciones puras y no pueden escribir en la base, si
`publish()` refresca el token contra `account_credentials`, lo devuelve en el
resultado bajo la clave `"refreshed_credentials"` (solo cuando el refresh
ocurrió) — `app/tasks.py::publish_job` lo detecta después de un publish
exitoso y, si el job tiene `account_id`, lo persiste en esa fila `Account`.
En modo single-account (sin `account_id`) el token refrescado se sigue
escribiendo directo a `token.json`, como antes.

Para probar una subida real sin escribir `python -c` a mano:

```bash
python -m scripts.enqueue_youtube_test --video /ruta/al/video.mp4
python -m scripts.enqueue_youtube_test --video /ruta/al/video.mp4 --account "Canal cliente X"
```

Crea un job `platform="youtube"` privado con un título de prueba generado, y
lo despacha de inmediato.

### Refresh proactivo de tokens OAuth (Fase 8)

Además del refresh reactivo de la Fase 7 (durante un publish), ahora hay un
**tercer proceso de Celery Beat**, `refresh_expiring_tokens`, que corre cada
30 minutos y refresca de forma proactiva los tokens OAuth de las cuentas
activas en plataformas cuyos tokens expiran (hoy solo `youtube`; los access
tokens OAuth 1.0a de Twitter no expiran). Si el token de una cuenta vence en
los próximos 45 minutos (o no se puede leer su expiry), lo refresca y guarda
las credenciales nuevas.

Si el refresh falla porque el refresh token es inválido o fue revocado, la
cuenta se marca `is_active=False` (deja de recibir jobs) y se manda una
alerta a Discord/Slack (mismo `ALERT_WEBHOOK_URL` que la dead-letter queue)
pidiendo re-autorización. Para reactivarla:

```bash
python -m scripts.authorize_youtube --account "Canal cliente X"
```

(re-autorizar reactiva la cuenta automáticamente — `upsert_account` deja
`is_active=True` tanto al crear como al actualizar).

Corré este tercer proceso junto a los otros tres (worker, beat, dashboard
opcional) — usa el mismo `celery -A app.celery_app beat`, no hace falta un
proceso aparte, ya que `dispatch_due_jobs` y `refresh_expiring_tokens`
comparten el mismo `beat_schedule`.

Para ver el estado de las cuentas (incluida la expiración del token) sin
abrir Neon:

```bash
python -m scripts.show_accounts
```

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

## Docker y Fly.io (Fase 9)

Toda la config sigue viniendo de variables de entorno (`app/config.py`) — la
imagen no tiene ningún secreto horneado adentro.

**Build de la imagen:**

```bash
docker build -t distribution-engine .
```

Base `python:3.13-slim`, usuario no-root, capas separadas para
`requirements.txt` (cache de pip) y el resto del código. `.dockerignore`
excluye `.venv`, `.env`, `.git`, `*.mp4`, `token.json`, `client_secret.json`
y los archivos de `celerybeat-schedule` — ninguno de esos debe terminar en
la imagen. `dashboard/` **sí** se incluye, porque también corre en
contenedor (proceso `api`, ver abajo).

**Correr todo localmente con Docker Compose** (en vez de las 3-4 terminales
manuales de más arriba):

```bash
docker compose up --build
```

Levanta `worker`, `beat` y `api`, los tres desde la misma imagen con
distintos comandos. Redis sigue corriendo directo en la Mac (no hay
servicio de Redis en el compose); `REDIS_URL` se pisa a
`redis://host.docker.internal:6379/0` en cada servicio para que los
contenedores lleguen al Redis del host — ver el comentario en
`docker-compose.yml`. `DATABASE_URL` y el resto de las variables siguen
viniendo de `.env` vía `env_file`. El dashboard queda expuesto en
`http://localhost:8000`.

**Checklist de deploy a Fly.io (pendiente — la cuenta es del cliente):**

`fly.toml` ya está armado (`[processes]` con `worker`, `beat` y `api`;
`internal_port = 8000` solo para `api`) pero no se usó todavía. Cuando
exista la cuenta:

1. Reemplazar el nombre placeholder `distribution-engine` en `fly.toml` por
   el nombre real de la app (`fly apps create ...` o editar el archivo).
2. `fly secrets set REDIS_URL=... DATABASE_URL=... ALERT_WEBHOOK_URL=...
   X_API_KEY=... X_API_SECRET=... X_ACCESS_TOKEN=...
   X_ACCESS_TOKEN_SECRET=... R2_...` — nunca en `fly.toml` ni en la imagen.
3. **YouTube en producción solo puede usar modo multi-account** (Fase 7):
   `token.json` / `client_secret.json` no viajan en la imagen (están en
   `.dockerignore`), así que cualquier cuenta de YouTube tiene que existir
   como fila `Account` en la base (`scripts/authorize_youtube.py
   --account NAME`) antes de correr en Fly.io.
4. `fly deploy`.

## Qué falta (a propósito, fuera de alcance de esta etapa)

- Publishers reales para cada red social (hoy solo existe YouTube además del fake).
- Migraciones reales con Alembic (hoy `init_db()` usa `create_all`, alcanza mientras el esquema es chico).
- Deploy real en Fly.io (el `fly.toml` y el Dockerfile están listos, pero nadie corrió `fly deploy` — falta la cuenta del cliente).
- Conectar `app/storage.py` (R2) a los publishers cuando exista un flujo real de media.
- Timezone real por cuenta/plataforma para el scheduling (hoy es naive local time, ver `app/config.py`).
