"""
Configuración centralizada, leída desde variables de entorno.

Decisión de diseño: ningún otro módulo debe llamar a os.environ directamente.
Al concentrar la lectura de configuración acá, cambiar de Redis a otro broker,
o de SQLite a Postgres, implica tocar un solo archivo (y las variables de
entorno del deploy), no el código de negocio.
"""

import os
from dataclasses import dataclass

# python-dotenv es opcional: si existe un .env en la raíz del proyecto lo carga,
# pero el proyecto funciona igual solo con variables de entorno del sistema.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass


@dataclass(frozen=True)
class Settings:
    # Redis ya corre en localhost:6379 según el enunciado; se usa como broker
    # y como result backend de Celery (nos alcanza para esta etapa, no hace
    # falta un backend de resultados separado).
    redis_url: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")

    celery_broker_url: str = os.getenv("CELERY_BROKER_URL", redis_url)
    celery_result_backend: str = os.getenv("CELERY_RESULT_BACKEND", redis_url)

    # SQLite como motor inicial (spec dice que más adelante migramos a Postgres).
    # Al usar SQLAlchemy con una URL de conexión, ese día alcanza con cambiar
    # esta variable de entorno: el resto del código (models.py, tasks.py)
    # no conoce el motor de base de datos concreto.
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./distribution_engine.db")

    # Cantidad máxima de reintentos ante errores transitorios (HTTP 429/5xx)
    # antes de mandar el job a la dead-letter queue.
    max_retries: int = int(os.getenv("MAX_RETRIES", "3"))

    # Base del backoff exponencial en segundos: intento N espera base**N.
    # Con base=2 y max_retries=3: 1s, 2s, 4s.
    retry_backoff_base: int = int(os.getenv("RETRY_BACKOFF_BASE", "2"))

    # Nombre de la cola a la que se enrutan los jobs con errores permanentes.
    dlq_queue_name: str = os.getenv("DLQ_QUEUE_NAME", "dlq")


settings = Settings()
