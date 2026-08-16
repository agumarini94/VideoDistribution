"""
Modelo de datos del job de publicación.

Decisión de diseño: el estado del job (JobStatus) modela explícitamente la
state machine del spec: queued -> processing -> published / failed. No usamos
strings sueltos ("queued", "processing", ...) desperdigados por el código;
todo pasa por este enum para que sea imposible, por ejemplo, escribir
"pending" en un lugar y "queued" en otro.
"""

import enum
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Enum, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class JobStatus(str, enum.Enum):
    """
    Estados de la state machine del job.

    queued     -> job creado, esperando a que un worker lo tome.
    processing -> un worker está intentando publicarlo ahora mismo.
    published  -> se publicó con éxito. Estado terminal.
    failed     -> se agotaron los reintentos (error transitorio) o el error
                  fue permanente. Estado terminal; el job queda además
                  enrutado a la dead-letter queue para revisión manual.
    """

    QUEUED = "queued"
    PROCESSING = "processing"
    PUBLISHED = "published"
    FAILED = "failed"


class Job(Base):
    """
    Representa un pedido de publicación de contenido en una red social.

    payload es JSON libre (texto, imágenes, hashtags, etc.) porque cada
    plataforma requiere campos distintos; validar su forma es responsabilidad
    del publisher correspondiente, no de este modelo.
    """

    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # Nombre de la red social destino (ej: "twitter", "instagram"). Es un
    # string libre y no un enum porque agregar una plataforma nueva no debería
    # requerir una migración de esquema.
    platform: Mapped[str] = mapped_column(String(50), nullable=False)

    payload: Mapped[dict] = mapped_column(JSON, nullable=False)

    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False, length=20),
        nullable=False,
        default=JobStatus.QUEUED,
    )

    # Cantidad de intentos de publicación realizados (incluye el primero,
    # no solo los reintentos). Sirve para auditoría y para decidir backoff.
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # Mensaje del último error visto (transitorio o permanente). Null si
    # todavía no hubo ningún intento fallido.
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Momento en el que el job debería procesarse. Hoy se usa igual a
    # created_at al encolar, pero queda preparado para programar publicaciones
    # a futuro sin cambiar el modelo.
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - solo para debugging
        return f"Job(id={self.id}, platform={self.platform!r}, status={self.status.value})"
