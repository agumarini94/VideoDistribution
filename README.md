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
| `STALL_THRESHOLD_MINUTES` | `30` | Minutos que un job puede estar en `queued`/`processing` sin actualizarse antes de considerarse estancado (Fase 14) |
| `STALL_REALERT_MINUTES` | `120` | Minutos de espera antes de volver a alertar sobre un job que sigue estancado |
| `R2_ENDPOINT_URL` | *(vacío, opcional)* | Endpoint S3-compatible del bucket de Cloudflare R2 |
| `R2_ACCESS_KEY_ID` | *(vacío, opcional)* | Access key de un API token de R2 |
| `R2_SECRET_ACCESS_KEY` | *(vacío, opcional)* | Secret key del mismo API token de R2 |
| `R2_BUCKET_NAME` | *(vacío, opcional)* | Nombre del bucket de R2 |
| `TIME_SLOTS` | *(vacío, usa los defaults)* | Override de horarios por plataforma, formato `twitter=09:00,13:00,18:00;tiktok=12:00,19:00` |
| `X_API_KEY` | *(vacío, opcional)* | Consumer key de la app de X (Twitter), nivel app, no por cuenta |
| `X_API_SECRET` | *(vacío, opcional)* | Consumer secret de la app de X, nivel app |
| `X_ACCESS_TOKEN` | *(vacío, opcional)* | Access token de fallback para jobs sin `account_id` (modo single-account) |
| `X_ACCESS_TOKEN_SECRET` | *(vacío, opcional)* | Access token secret de fallback, mismo caso que arriba |
| `TIKTOK_CLIENT_KEY` | *(vacío, opcional)* | Client key de la app de TikTok (Developer Portal) |
| `TIKTOK_CLIENT_SECRET` | *(vacío, opcional)* | Client secret de la app de TikTok |
| `TIKTOK_REDIRECT_URI` | *(sin default)* | Redirect URI registrada en el Developer Portal (página pública HTTPS, ver más abajo — **no** puede ser localhost) |
| `TIKTOK_LOCAL_CALLBACK_PORT` | `8910` | Puerto local donde `scripts/authorize_tiktok.py` escucha el callback relayado por la página pública |
| `TIKTOK_WEBHOOK_SKIP_SIGNATURE` | *(vacío)* | `1` deshabilita la verificación de firma en `POST /webhooks/tiktok` — solo para pruebas locales con curl, nunca en producción (ver Fase 10b) |
| `TIKTOK_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS` | `300` | Antigüedad máxima aceptada del timestamp firmado en un webhook de TikTok, contra replay |
| `DASHBOARD_USERNAME` | *(vacío)* | Usuario para HTTP Basic auth del dashboard; si falta este o `DASHBOARD_PASSWORD`, el dashboard queda sin proteger (ver Fase 11) |
| `DASHBOARD_PASSWORD` | *(vacío)* | Password para HTTP Basic auth del dashboard, mismo caso que arriba |

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

### TikTok — Sandbox (Fase 10)

`app/publishers/tiktok.py` implementa la **Content Posting API** de TikTok,
pero solo en **modo Sandbox**: la revisión de la app en el Developer Portal
todavía no pasó, así que solo está disponible el scope `video.upload` (no
`video.publish`). Esto tiene dos consecuencias importantes:

- Se usa el flujo de **inbox upload** (`POST
  /v2/post/publish/inbox/video/init/` + subida en chunks a `upload_url`), no
  Direct Post: el video llega como **borrador al inbox de TikTok** de la
  cuenta, no se publica solo — el dueño de la cuenta tiene que abrir la app
  de TikTok y publicarlo a mano.
- Solo funciona contra cuentas dadas de alta como **Sandbox testers** de
  esta app en el Developer Portal.

El día que se apruebe `video.publish`, pasar a Direct Post es un cambio
chico y contenido: el endpoint de init y el body de la request están
aislados en `tiktok.py` (`_INBOX_INIT_URL` / `_DIRECT_POST_INIT_URL` /
`_build_init_body`, con un comentario marcando el swap) — la subida en
chunks no cambia.

A diferencia de `youtube.py`/`twitter.py`, **no hay modo single-account**
para TikTok (no existe un equivalente a `token.json` o `X_ACCESS_TOKEN`):
todo job `platform="tiktok"` necesita `account_id`.

**El Developer Portal rechaza redirect URIs `localhost`/`127.0.0.1`.**
`TIKTOK_REDIRECT_URI` tiene que ser una página pública HTTPS que no hace
nada más que reenviar el callback a la máquina local vía JS
(`location.replace`). El truco más simple es GitHub Pages: un repo público
con un `callback.html` como este,

```html
<!doctype html>
<script>
  location.replace("http://localhost:8910/callback" + location.search);
</script>
```

publicado en `https://<usuario>.github.io/<repo>/callback.html` — esa URL
es la que se registra en el Developer Portal y la que va en
`TIKTOK_REDIRECT_URI`. El `8910` del snippet tiene que coincidir con
`TIKTOK_LOCAL_CALLBACK_PORT` (default `8910`); si lo cambiás, actualizá
también el `callback.html` publicado.

`scripts/authorize_tiktok.py` nunca toca `TIKTOK_REDIRECT_URI` directamente
más que para mandárselo a TikTok (URL de autorización + exchange de
token, donde tiene que matchear exacto con el Portal) — el servidor HTTP
local de un solo uso siempre escucha en `localhost:TIKTOK_LOCAL_CALLBACK_PORT`,
independientemente del valor de `TIKTOK_REDIRECT_URI`.

Para dar de alta una cuenta (corre el flujo OAuth interactivo):

```bash
python -m scripts.authorize_tiktok --account "Cuenta cliente X"
```

El refresh proactivo de tokens (mismo mecanismo que YouTube, Fase 8) también
cubre TikTok — ver `_TOKEN_REFRESH_MODULES_BY_PLATFORM` en `app/tasks.py`.

### Webhook de TikTok (Fase 10b)

`POST /webhooks/tiktok` (agregado a la misma app FastAPI del dashboard,
`dashboard/api.py`) recibe los callbacks de estado del Content Posting API
de TikTok. Como el Developer Portal todavía no deja registrar una callback
URL real de Sandbox (mismo bloqueo que el resto de la Fase 10), está armado
para poder probarse **enteramente en local**, con curl o con
`scripts/simulate_tiktok_webhook.py`.

**Cómo corre:**

- `dashboard/api.py::tiktok_webhook` verifica la firma, parsea el envelope
  y guarda un registro de auditoría en la tabla `webhook_events`
  (`app/models.py::WebhookEvent`) — y ahí responde `200` enseguida.
- El trabajo real (buscar el `Job`, cambiar su estado, mandar la alerta a
  Discord/Slack) pasa a una task de Celery,
  `app.tasks.handle_tiktok_webhook_event`, así una alerta lenta nunca
  demora la respuesta que TikTok espera. **Corré el worker de Celery** para
  que esta parte se procese (ver "Correr todo" más arriba).

**Verificación de firma:** el header `TikTok-Signature` viene como
`t=<timestamp>,s=<hmac>`; se verifica con HMAC-SHA256 usando
`TIKTOK_CLIENT_SECRET` sobre `"<timestamp>.<body crudo>"`, más una
tolerancia de antigüedad del timestamp
(`TIKTOK_WEBHOOK_TIMESTAMP_TOLERANCE_SECONDS`, default 300s) contra replay.
Una firma inválida devuelve `401` (a propósito: enmascararla como `200`
ocultaría un problema real de configuración o un intento de spoof).

**Para probar en local sin firma real**, seteá en `.env`:

```bash
TIKTOK_WEBHOOK_SKIP_SIGNATURE=1
```

El dashboard imprime un warning bien visible al arrancar mientras esto esté
seteado — **nunca dejarlo así en producción**.

**Con `scripts/simulate_tiktok_webhook.py`** (firma cualquier request con
`TIKTOK_CLIENT_SECRET`, no necesita `TIKTOK_WEBHOOK_SKIP_SIGNATURE`):

```bash
# evento de éxito (video.publish.completed) — matchea si el publish_id
# coincide con el external_id de algún Job platform="tiktok"
python -m scripts.simulate_tiktok_webhook --scenario delivered --publish-id v_pub_url~v2.123

# evento de falla (video.upload.failed) — marca el Job como failed y manda
# la alerta configurada en ALERT_WEBHOOK_URL
python -m scripts.simulate_tiktok_webhook --scenario failed --publish-id v_pub_url~v2.123

# publish_id que no matchea ningún Job — se audita y se responde 200,
# sin hacer que TikTok reintente
python -m scripts.simulate_tiktok_webhook --scenario delivered --publish-id no-existe

# --skip-signature para probar TIKTOK_WEBHOOK_SKIP_SIGNATURE=1
python -m scripts.simulate_tiktok_webhook --scenario delivered --publish-id v_pub_url~v2.123 --skip-signature
```

**O con curl directo** (requiere `TIKTOK_WEBHOOK_SKIP_SIGNATURE=1` en el
server, porque curl no firma el body):

```bash
curl -X POST http://localhost:8000/webhooks/tiktok \
  -H "Content-Type: application/json" \
  -d '{"event":"video.publish.completed","create_time":1735689600,"content":"{\"publish_id\":\"v_pub_url~v2.123\"}"}'

curl -X POST http://localhost:8000/webhooks/tiktok \
  -H "Content-Type: application/json" \
  -d '{"event":"video.upload.failed","create_time":1735689600,"content":"{\"publish_id\":\"v_pub_url~v2.123\",\"fail_reason\":\"video_format_check_failed\"}"}'
```

**Matching (`Job.external_id`):** los publishers ya devolvían un
`external_id` en su resultado, pero antes se descartaba —
`app/tasks.py::_persist_external_id` ahora lo persiste en `Job.external_id`
después de un publish exitoso, que es contra lo que matchea el webhook.
**Paso manual de esquema** (mismo patrón que `account_id` en la Fase 6):
`init_db()` crea la tabla nueva `webhook_events` sola, pero no agrega la
columna a la tabla `jobs` existente. Correr una vez, a mano, contra Neon:

```sql
ALTER TABLE jobs ADD COLUMN external_id VARCHAR(255);
```

**Nombres de evento — advertencia:** los únicos eventos de Content Posting
que la documentación oficial (`developers.tiktok.com/doc/webhooks-events`)
lista hoy son `video.upload.failed` y `video.publish.completed` — no
`post.publish.failed` / `post.publish.inbox.delivered` como aparece en
otras partes de la documentación/marketing de TikTok. Como todavía no se
puede registrar un webhook real de Sandbox para observar un payload real,
`app/webhooks/tiktok.py::classify_event` clasifica por substring
(`"failed"`/`"fail"` → falla, `"completed"`/`"delivered"`/`"success"` →
éxito) en vez de una lista cerrada — debería seguir funcionando ante
cualquiera de los dos esquemas de nombres, pero conviene reverificarlo
contra un evento real apenas se pueda.

## Dashboard de monitoreo (extra, fuera del spec)

`dashboard/` es un panel de solo lectura sobre el estado de los jobs (salvo
por la acción de reintentar un job `failed` y, desde la Fase 10b, recibir
el webhook de TikTok — ver arriba). No modifica ni depende de lógica nueva
en `app/` — solo lee de la misma base y llama a `publish_job.delay` /
`handle_tiktok_webhook_event.delay` para el trabajo real.

```bash
uvicorn dashboard.api:app --reload --port 8000
```

Abrí `http://localhost:8000` — sirve el frontend (`dashboard/static/index.html`)
y expone la API en `/api/jobs`, `/api/stats`, `POST /api/jobs/{id}/retry`
(esta última solo funciona sobre jobs en estado `failed`; devuelve 409 en
cualquier otro caso) y `POST /webhooks/tiktok` (Fase 10b, ver arriba). No
requiere el worker de Celery corriendo para mostrar datos, pero sí para que
un retry o un webhook de TikTok se procesen de verdad (el endpoint del
webhook responde 200 igual, pero el matching/alerta no ocurre sin worker).

**Protegido con HTTP Basic auth** (Fase 11) si `DASHBOARD_USERNAME` y
`DASHBOARD_PASSWORD` están seteados — ver más abajo.

### Autenticación del dashboard (Fase 11)

Con `DASHBOARD_USERNAME` y `DASHBOARD_PASSWORD` seteados en `.env`, **toda**
ruta de `dashboard/api.py` pide HTTP Basic auth: `/api/*`, el frontend
estático (`/`), y `/docs`/`/redoc`/`/openapi.json`. La única excepción es
`POST /webhooks/tiktok`: los servidores de TikTok le pegan directo y no
pueden mandar las credenciales del dashboard — ese endpoint ya tiene su
propia autenticación (la verificación de `TikTok-Signature`, Fase 10b), así
que exponerlo sin Basic auth no baja la seguridad.

Si falta cualquiera de las dos variables, el dashboard sigue arrancando
(para no trabar el desarrollo local) pero imprime un warning bien visible al
arrancar — mismo estilo que el de `TIKTOK_WEBHOOK_SKIP_SIGNATURE`. **Nunca
desplegar así**: sin ambas variables seteadas, cualquiera que llegue al
proceso puede leer jobs/cuentas y disparar retries.

```bash
# .env
DASHBOARD_USERNAME=admin
DASHBOARD_PASSWORD=una-contraseña-fuerte
```

```bash
curl -u admin:una-contraseña-fuerte http://localhost:8000/api/jobs
```

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

## Tests

Suite de pytest (`tests/`) sobre las piezas más críticas de lógica pura y de
orquestación: verificación de firma/parseo/clasificación del webhook de
TikTok (`app/webhooks/tiktok.py`), clasificación de errores de los
publishers de TikTok y Twitter/X (`app/publishers/tiktok.py`,
`app/publishers/twitter.py`, incluida una subida chunked completa contra
HTTP mockeado), la subida chunked de media y los threads de X
(`tests/test_publisher_twitter_media.py`, Fase 17), la validación de
Shorts y la asignación a playlist de YouTube (`tests/test_publisher_youtube.py`,
`tests/test_media_probe.py`, Fase 15), generación del par PKCE
(`scripts/authorize_tiktok.py`), y la task `handle_tiktok_webhook_event`
(`app/tasks.py`) contra una base SQLite descartable. No hace falta Redis, un
worker de Celery corriendo, ni la `DATABASE_URL` real de Neon: todo el HTTP
está mockeado (`responses` / monkeypatch) y `tests/conftest.py` apunta
`DATABASE_URL` a un SQLite temporal antes de importar cualquier módulo de
`app/`.

```bash
pip install -r requirements-dev.txt
pytest
```

## Guardas de payload de X (Fase 13)

`app/publishers/twitter.py::_validate_text` rechaza en pre-flight (con
`PermanentError`, sin llamar a la API) cualquier `text` de más de 280
caracteres, contando con `len()` simple. Esto es una aproximación: X cuenta
cada URL como 23 caracteres fijos (su wrapper `t.co`), sin importar la
longitud real — un tweet con URLs muy largas puede pasar esta validación y
igual ser rechazado por la API real. Aceptable por ahora; ver el comentario
en el código.

## Detección de jobs estancados (Fase 14)

Nueva task de Celery Beat, `detect_stalled_jobs` (`app/tasks.py`), corre
cada 10 minutos (`beat_schedule` en `app/celery_app.py`) — no hace falta un
proceso aparte, comparte el mismo `celery -A app.celery_app beat` que
`dispatch_due_jobs` y `refresh_expiring_tokens`.

Busca jobs en estado `queued` o `processing` cuyo `updated_at` no cambió en
más de `STALL_THRESHOLD_MINUTES` (default 30) — típicamente un worker caído,
un Beat que dejó de correr, o una task que quedó colgada — y manda **una
sola** alerta (mismo `ALERT_WEBHOOK_URL` que la dead-letter queue) listando
todos los jobs encontrados, en vez de una alerta por job.

Para no floodear el canal de alertas si el estancamiento sigue varias
corridas seguidas, cada `Job` tiene una columna `last_stall_alert_at`
(nullable) que se actualiza solo cuando se manda una alerta por ese job —
no se vuelve a alertar sobre el mismo job hasta que pasen
`STALL_REALERT_MINUTES` (default 120) desde la última alerta.

**Paso manual de esquema** (mismo patrón que `account_id` en la Fase 6 y
`external_id` en la Fase 10b): `init_db()`/`create_all` no agrega columnas
a una tabla `jobs` ya existente. Correr una vez, a mano, contra Neon:

```sql
ALTER TABLE jobs ADD COLUMN last_stall_alert_at TIMESTAMPTZ;
```

## Shorts y playlists de YouTube (Fase 15)

`app/publishers/youtube.py` gana dos capacidades opcionales por payload,
retrocompatibles (si no se mandan, el comportamiento es exactamente el de
antes):

- **`"shorts": true`** — antes de subir el video, se lo probea con
  `ffprobe` (nuevo módulo `app/media_probe.py`, subprocess puro, pensado
  para que `tiktok.py` lo reutilice el día que necesite sus propias
  validaciones de duración/aspecto). Si dura más de 60s o no es vertical
  (`height <= width`), `PermanentError` nombrando la duración/dimensión
  real — **sin subir nada**. Si `"shorts"` no viene o es `false`, no se
  probea el archivo en absoluto.
- **`"playlist_id": "PL..."`** — después de un `videos.insert` exitoso, se
  llama a `playlistItems.insert` para sumar el video a esa playlist.
  **Importante**: en ese punto el video ya está subido a YouTube, así que
  un error en la playlist (id inválido, scope insuficiente, error
  transitorio de la API) **nunca** hace fallar el job ni dispara un
  reintento — eso re-subiría el video. El error se loguea como warning y
  se devuelve en `result["playlist_error"]`, y el resultado sigue
  reportando `external_id` como éxito normal.

**Requiere `ffmpeg` instalado localmente** (trae `ffprobe`) para que
`"shorts": true` funcione — `brew install ffmpeg` en Mac. Si falta,
`PermanentError` con el mensaje de instalación en vez de un crash.

**Cambio de scope**: `playlistItems.insert` necesita el scope
`https://www.googleapis.com/auth/youtube` (más amplio que
`youtube.upload`, el único que se pedía hasta la Fase 14).
`SCOPES` en `youtube.py` ahora pide los dos. **Cualquier cuenta autorizada
antes de esta fase** (`Account` o `token.json` single-account) **solo tiene
`youtube.upload`** y hay que re-autorizarla:

```bash
python -m scripts.authorize_youtube --account "Canal cliente X"
```

Mientras eso no pase, un job con `playlist_id` contra esa cuenta sigue
subiendo el video normalmente, pero `playlist_error` siempre va a traer un
error de scope insuficiente.

**Dashboard** (`dashboard/`, no `app/`): en la pestaña "New Job", cuando
`platform=youtube` aparecen tres campos opcionales — privacy
(private/unlisted/public, default private), checkbox "Shorts", e input de
texto para el playlist ID. `dashboard/api.py::create_job` los pasa tal
cual al payload del job; toda la validación real vive en `youtube.py`,
igual que el resto de los campos específicos por plataforma.

## Media y threads de X (Fase 17)

`app/publishers/twitter.py` gana dos capacidades que eran los huecos más
grandes contra el spec, manteniendo el contrato de publisher puro (nada de
Celery/DB, solo `TransientError`/`PermanentError`) y la resolución de
credenciales multi-cuenta exacta de la Fase 6. Seguimos sin credenciales
reales de X (el cliente todavía está creando la cuenta de developer), así
que todo esto está probado con HTTP completamente mockeado
(`tests/test_publisher_twitter_media.py`).

- **Adjuntar media (`payload["media_paths"]`)**: lista de rutas de archivo
  locales. Cada una se sube a X vía el endpoint v1.1
  (`upload.twitter.com/1.1/media/upload.json`, INIT -> APPEND en chunks de
  4 MiB -> FINALIZE), firmado OAuth 1.0a igual que el tweet en sí — para
  esto se reutiliza `tweepy.API`/`OAuth1UserHandler` (mismas credenciales de
  acceso por cuenta que ya resuelve `_resolve_credentials`), pero el módulo
  maneja el chunking, la categoría de media y el polling de estado a mano,
  en vez de usar el combinador `chunked_upload()` de tweepy — así un fallo
  de procesamiento asíncrono (video/gif) se puede clasificar y reportar
  igual que cualquier otro error de este módulo. Videos/gifs quedan en
  `processing_info.state` = pending/in_progress hasta terminar de procesarse
  server-side; se hace polling a `GET .../media/upload.json?command=STATUS`
  hasta `succeeded` o `failed` (este último -> `PermanentError` con el
  motivo). Imágenes finalizan sync, sin `processing_info` — no hay polling.
  Límite de X validado en pre-flight, antes de subir nada: 4 imágenes o 1
  video por tweet, nunca mezclados.
- **Threads (`payload["thread"]`)**: lista ordenada de `{"text", opcional
  "media_paths"}`, publicados secuencialmente, cada uno encadenado al
  anterior vía `in_reply_to_tweet_id`. **Se valida todo el thread antes de
  publicar el tweet #1** — el límite de 280 caracteres de cada texto (Fase
  13) y los límites de media de cada tweet — para nunca dejar un thread a
  medio publicar por un error que se podía detectar de antemano. Si falla
  una llamada a mitad del thread, el error (`Transient`/`PermanentError`)
  se re-lanza con cuántos tweets se llegaron a publicar y el id del último
  tweet exitoso (info para `error_message`/la alerta de DLQ). `"thread"` y
  `"text"` a nivel raíz del payload son mutuamente excluyentes:
  `PermanentError` si vienen los dos o ninguno.
- **Resultado**: `external_id` es el id del primer tweet; los threads además
  devuelven `"tweet_ids"` con el id de cada tweet publicado, en orden.
- Los archivos de `media_paths` son rutas locales — todavía no pasan por
  `app/storage.py` (R2), ver la nota de "Qué falta" más abajo.

## Qué falta (a propósito, fuera de alcance de esta etapa)

- Migraciones reales con Alembic (hoy `init_db()` usa `create_all`, alcanza mientras el esquema es chico).
- Deploy real en Fly.io (el `fly.toml` y el Dockerfile están listos, pero nadie corrió `fly deploy` — falta la cuenta del cliente).
- Conectar `app/storage.py` (R2) a los publishers cuando exista un flujo real de media (incluido X: hoy `media_paths` son rutas locales).
- Timezone real por cuenta/plataforma para el scheduling (hoy es naive local time, ver `app/config.py`).
- TikTok Direct Post (`video.publish`, pendiente de la revisión de la app).
- Registrar la callback URL real del webhook de TikTok en el Developer Portal (bloqueado por el mismo motivo que el resto del setup de Sandbox — ver Fase 10b) y confirmar los nombres de evento reales contra un payload real la primera vez que llegue uno.
- Credenciales reales de X (Developer Portal): la Fase 17 (media + threads) sigue sin poder probarse contra la API real hasta que el cliente las provea.
