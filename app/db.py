"""
Configuración de SQLAlchemy: engine, sesión y Base declarativa.

Decisión de diseño: Base vive acá (no en models.py) para que db.py no dependa
de models.py. Es models.py quien importa Base desde acá. Así evitamos import
circular entre "el módulo que define las tablas" y "el módulo que sabe cómo
conectarse a la base".
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

# check_same_thread=False es necesario para SQLite cuando la conexión puede
# ser usada desde distintos threads (workers de Celery, por ejemplo). No
# aplica a otros motores, así que solo lo agregamos si estamos en SQLite.
connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}

engine = create_engine(settings.database_url, connect_args=connect_args)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    """
    Crea las tablas si no existen. Idempotente: se puede llamar en cada
    arranque sin riesgo. Cuando migremos a Postgres, esto se reemplaza por
    migraciones reales (Alembic); por ahora, para una SQLite de desarrollo,
    alcanza con create_all.
    """
    # Import local para evitar el ciclo db -> models -> db al importar el módulo.
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
