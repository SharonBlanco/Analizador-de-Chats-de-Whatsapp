"""
Configuración y nombres compartidos entre el coordinador (mensajeria/web.py) y los workers.

Flujo de tareas reutilizado del I Proyecto (coordinador/app.py y worker/worker.py):
colas durables con prioridad, ACK después del resultado (si un worker muere,
RabbitMQ re-entrega la sub-tarea a otro), cola de resultados que escucha el
coordinador, reintentos con espera creciente y cierre por barrier/join.

Credenciales: nunca en el código. Se leen de variables de entorno o, si faltan,
de distribuido/config.env (excluido de Git). Ver config.env.ejemplo.
"""

from __future__ import annotations

import os
from pathlib import Path

import pika

AQUI = Path(__file__).resolve().parent
ARCHIVO_CONFIG = AQUI / "config.env"


def _leer_archivo(ruta: Path) -> dict:
    datos = {}
    try:
        for linea in ruta.read_text(encoding="utf-8").splitlines():
            linea = linea.strip()
            if linea and not linea.startswith("#") and "=" in linea:
                k, v = linea.split("=", 1)
                datos[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return datos


_archivo = _leer_archivo(ARCHIVO_CONFIG)


def config(clave: str, defecto: str | None = None) -> str | None:
    """Variable de entorno > distribuido/config.env > valor por defecto."""
    return os.environ.get(clave) or _archivo.get(clave) or defecto


# ---------------------------------------------------------------- nombres de colas
PREFIJO = config("WA_PREFIJO_COLAS", "wa.")            # separa estas colas de las del I Proyecto
POOLS = {   # routing por tipo de contenido (B.4): cada worker elige qué colas atiende (WORKER_POOLS)
    "texto": f"{PREFIJO}tareas.texto",                  # bloques de mensajes → estadísticas + detector de texto
    "imagen": f"{PREFIJO}tareas.imagen",                # fotos → SHA-256 + YOLO (armas)
    "audio": f"{PREFIJO}tareas.audio",                  # notas de voz → SHA-256 + Whisper + detector de texto
    "adjuntos": f"{PREFIJO}tareas.adjuntos",            # videos y documentos → SHA-256 y metadatos
}
TIPOS_POOL = {"imagen": "imagen", "voz": "audio", "audio": "audio"}   # tipo de adjunto → pool (el resto: adjuntos)
COLA_RESULTADOS = f"{PREFIJO}resultados"               # workers → coordinador (inicio / fin de sub-tareas)
EXCHANGE_LATIDOS = f"{PREFIJO}latidos"                  # workers → coordinador (fanout, métricas cada 5 s)
EXCHANGE_CONTROL = f"{PREFIJO}control"                  # coordinador → workers (fanout, cancelaciones)
ARGS_COLA = {"x-max-priority": 10}                      # prioridad 1 (baja) a 10 (alta)

INTERVALO_LATIDO = 5          # s entre latidos de un worker
SIN_SENAL = 15                # s sin latido para considerar un worker desconectado
MAX_REINTENTOS = int(config("WA_MAX_REINTENTOS", "3"))
ESPERA_REINTENTO = float(config("WA_ESPERA_REINTENTO", "2"))   # s; se multiplica por el n.º de intento


class ConfiguracionIncompleta(Exception):
    pass


def parametros_rabbit() -> pika.ConnectionParameters:
    usuario, clave = config("RABBIT_USER"), config("RABBIT_PASS")
    if not usuario or not clave:
        raise ConfiguracionIncompleta(
            "Faltan RABBIT_USER y RABBIT_PASS. Cópielos en distribuido/config.env "
            "(ver distribuido/config.env.ejemplo) o defínalos como variables de entorno.")
    return pika.ConnectionParameters(
        host=config("RABBIT_HOST", "localhost"), port=int(config("RABBIT_PORT", "5672")),
        credentials=pika.PlainCredentials(usuario, clave),
        heartbeat=30, blocked_connection_timeout=300, connection_attempts=1, socket_timeout=5)


def declarar(canal) -> None:
    """Colas y exchanges (idempotente: coordinador y workers pueden declararlos)."""
    for cola in POOLS.values():
        canal.queue_declare(queue=cola, durable=True, arguments=ARGS_COLA)
    canal.queue_declare(queue=COLA_RESULTADOS, durable=True)
    canal.exchange_declare(exchange=EXCHANGE_LATIDOS, exchange_type="fanout")
    canal.exchange_declare(exchange=EXCHANGE_CONTROL, exchange_type="fanout")


PERSISTENTE = 2   # delivery_mode: el mensaje sobrevive a un reinicio de RabbitMQ
