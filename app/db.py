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

# pool_pre_ping and pool_recycle guard against Neon's autosuspend: on the
# free tier, Neon suspends the database after a period of inactivity and
# drops existing connections. Without these, a pooled connection that
# survived a suspend looks fine to SQLAlchemy but fails on first use with
# "SSL connection has been closed unexpectedly". pool_pre_ping issues a
# cheap SELECT 1 before handing out a pooled connection and transparently
# reconnects if it's dead; pool_recycle proactively recycles connections
# older than 300s so we rarely rely on pre_ping alone.
engine = create_engine(settings.database_url, pool_pre_ping=True, pool_recycle=300)

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
