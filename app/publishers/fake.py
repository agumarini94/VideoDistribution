"""
Publisher simulado (fake), para desarrollar y probar el motor sin depender
de ninguna API real de redes sociales todavía.

Decisión de diseño clave del proyecto: un publisher es una función pura.
- No conoce Celery: no sabe qué es un retry, una cola, ni un job_id de
  base de datos. Solo recibe los datos mínimos para "publicar" y devuelve
  un resultado o lanza una excepción tipada.
- No imprime nada a pantalla: si necesitara loggear, sería responsabilidad
  de quien lo llama (la tarea Celery), no del publisher.
Esto hace que sea trivial testear el publisher en un test unitario común
(sin levantar Redis ni un worker) y que, el día que se agregue un publisher
real (app/publishers/twitter.py, por ejemplo), tenga exactamente la misma
forma: publish(platform_payload) -> dict | raise TransientError/PermanentError.
"""

import random

from app.exceptions import PermanentError, TransientError

# Probabilidades fijas según el spec: 30% error transitorio (429),
# 10% error permanente (400), 60% éxito.
_PROBABILITY_TRANSIENT = 0.30
_PROBABILITY_PERMANENT = 0.10


def publish(platform: str, payload: dict) -> dict:
    """
    Simula la publicación de `payload` en `platform`.

    Devuelve un dict con el resultado simulado si "publica" con éxito.
    Lanza TransientError (simulando HTTP 429) o PermanentError (simulando
    HTTP 400) según el resultado aleatorio, para poder ejercitar la lógica
    de retry/backoff y dead-letter queue del resto del sistema.
    """
    roll = random.random()

    if roll < _PROBABILITY_TRANSIENT:
        raise TransientError(f"429 Too Many Requests: rate limit alcanzado en {platform}")

    if roll < _PROBABILITY_TRANSIENT + _PROBABILITY_PERMANENT:
        raise PermanentError(f"400 Bad Request: payload inválido para {platform}")

    return {"platform": platform, "external_id": f"fake-{random.randint(100000, 999999)}"}
