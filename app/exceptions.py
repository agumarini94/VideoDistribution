"""
Excepciones tipadas del dominio de publicación.

Decisión de diseño: estas excepciones son el "contrato" entre los publishers
y la capa de orquestación (Celery). Un publisher nunca decide si algo se
reintenta o no; solo clasifica el error que tuvo. Quien decide qué hacer con
esa clasificación es app/tasks.py. Esto permite:
  - Testear publishers sin Celery (son funciones puras que solo pueden
    devolver un resultado o lanzar una de estas dos excepciones).
  - Agregar publishers reales (Twitter, LinkedIn, etc.) más adelante sin
    tocar la lógica de retry/DLQ: alcanza con que mapeen sus propios errores
    HTTP a TransientError o PermanentError.
"""


class PublishError(Exception):
    """Clase base de cualquier error de publicación."""


class TransientError(PublishError):
    """
    Error temporal (ej: HTTP 429 Too Many Requests, HTTP 5xx del proveedor).

    Se asume que reintentar más tarde puede resolver el problema, por eso
    la tarea Celery aplica backoff exponencial ante este tipo de error.
    """


class PermanentError(PublishError):
    """
    Error permanente (ej: HTTP 400 Bad Request: payload inválido, credenciales
    revocadas, plataforma inexistente).

    Reintentar no cambia el resultado, por eso la tarea Celery lo manda
    directo a la dead-letter queue sin gastar reintentos.
    """
