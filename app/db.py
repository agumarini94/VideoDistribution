"""
SQLAlchemy setup: engine, session and declarative Base.

Design decision: Base lives here (not in models.py) so db.py doesn't depend
on models.py. It's models.py that imports Base from here. This avoids a
circular import between "the module that defines the tables" and "the
module that knows how to connect to the database".
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

engine = create_engine(settings.database_url)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    """
    Creates the tables if they don't exist yet. Idempotent: safe to call on
    every startup. This is a stand-in for real migrations (Alembic) while
    the schema is still small and changing; once it stabilizes, this should
    be replaced by proper migrations instead of create_all.
    """
    # Local import to avoid the db -> models -> db cycle when this module is imported.
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
