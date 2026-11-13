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
import ssl
from pathlib import Path
from urllib.parse import urlparse

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
SIN_SENAL = 30                # s sin latido para considerar un worker desconectado (latido cada 5 s: tolera 5 perdidos)
MAX_REINTENTOS = int(config("WA_MAX_REINTENTOS", "3"))
ESPERA_REINTENTO = float(config("WA_ESPERA_REINTENTO", "2"))   # s; se multiplica por el n.º de intento


class ConfiguracionIncompleta(Exception):
    pass


def parametros_rabbit() -> pika.ConnectionParameters:
    url = config("RABBIT_URL")
    if url:
        # RabbitMQ en la nube (p. ej. CloudAMQP): amqps://usuario:clave@servidor/vhost → conexión cifrada con TLS
        p = pika.URLParameters(url)
        if url.lower().startswith("amqps://"):
            # verifica el certificado del servidor (pika no indica el nombre del servidor por defecto)
            p.ssl_options = pika.SSLOptions(ssl.create_default_context(), server_hostname=p.host)
        p.heartbeat, p.blocked_connection_timeout, p.connection_attempts, p.socket_timeout = 30, 300, 1, 15
        return p
    usuario, clave = config("RABBIT_USER"), config("RABBIT_PASS")
    if not usuario or not clave:
        raise ConfiguracionIncompleta(
            "Faltan RABBIT_USER y RABBIT_PASS. Cópielos en distribuido/config.env "
            "(ver distribuido/config.env.ejemplo) o defínalos como variables de entorno.")
    return pika.ConnectionParameters(
        host=config("RABBIT_HOST", "localhost"), port=int(config("RABBIT_PORT", "5672")),
        credentials=pika.PlainCredentials(usuario, clave),
        heartbeat=30, blocked_connection_timeout=300, connection_attempts=1, socket_timeout=5)


_url_publica = {"valor": None, "hasta": 0.0}


def url_publica() -> str | None:
    """Dirección por la que los workers de otras redes descargan los adjuntos.
    WA_URL_PUBLICA fija, o la del túnel rápido de Cloudflare que esté corriendo en esta PC (cloudflared
    publica su dirección en su servidor de métricas local, puertos 20241–20245). Se consulta cada 30 s:
    si el túnel se reinicia con otra dirección, las tareas nuevas ya llevan la nueva."""
    fija = config("WA_URL_PUBLICA")
    if fija:
        return fija.rstrip("/")
    import json
    import time
    import urllib.request
    if time.time() < _url_publica["hasta"]:
        return _url_publica["valor"]
    valor = None
    for puerto in range(20241, 20246):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{puerto}/quicktunnel", timeout=0.5) as r:
                host = json.loads(r.read()).get("hostname")
                if host:
                    valor = f"https://{host}"
                    break
        except Exception:  # noqa: BLE001 — no hay túnel en ese puerto
            continue
    _url_publica.update(valor=valor, hasta=time.time() + 30)
    return valor


def destino_rabbit() -> str:
    """Dónde está RabbitMQ, para mostrar (sin la contraseña)."""
    url = config("RABBIT_URL")
    if url:
        u = urlparse(url)
        return f"{u.hostname}:{u.port or (5671 if u.scheme == 'amqps' else 5672)}" + (" (TLS)" if u.scheme == "amqps" else "")
    return f'{config("RABBIT_HOST", "localhost")}:{config("RABBIT_PORT", "5672")}'


def declarar(canal) -> None:
    """Colas y exchanges (idempotente: coordinador y workers pueden declararlos)."""
    for cola in POOLS.values():
        canal.queue_declare(queue=cola, durable=True, arguments=ARGS_COLA)
    canal.queue_declare(queue=COLA_RESULTADOS, durable=True)
    canal.exchange_declare(exchange=EXCHANGE_LATIDOS, exchange_type="fanout")
    canal.exchange_declare(exchange=EXCHANGE_CONTROL, exchange_type="fanout")


PERSISTENTE = 2   # delivery_mode: el mensaje sobrevive a un reinicio de RabbitMQ
