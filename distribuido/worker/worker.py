"""
Worker del analizador de chats. Se ejecuta en cada computadora que aporta capacidad.

Toma sub-tareas de RabbitMQ y devuelve el resultado a la cola de resultados:
  texto    → estadísticas parciales de un bloque de mensajes (estadisticas.parcial)
             + detección de agresiones en cada mensaje (detectores.analizar_textos)
  imagen   → SHA-256 + metadatos + YOLO (armas)
  audio    → SHA-256 + metadatos + Whisper + detector de texto sobre cada segmento
  adjuntos → videos y documentos: SHA-256 (cadena de custodia) y metadatos con ffprobe

Basado en worker/worker.py del I Proyecto: cada sub-tarea corre en su propio hilo y
el hilo principal solo atiende la conexión (pika no es thread-safe); los hilos le
piden publicar y confirmar con add_callback_threadsafe. El ACK va DESPUÉS de publicar
el resultado: si el worker muere a mitad, RabbitMQ re-entrega la sub-tarea a otro.

Uso (desde la raíz del proyecto):
  python distribuido/worker/worker.py
Configuración (variables de entorno o distribuido/config.env):
  RABBIT_HOST, RABBIT_USER, RABBIT_PASS   conexión a RabbitMQ (en la PC del coordinador)
  COORDINADOR_URL                         p. ej. http://192.168.1.10:8101 (descarga de adjuntos)
  WORKER_TOKEN                            el mismo que usa el coordinador
  WORKER_ID, WORKER_POOLS=texto,imagen,audio,adjuntos, WORKER_CONCURRENCIA=1
  WA_DETECCION=1   (0 = sin modelos: solo estadísticas y verificación; útil para medir la infraestructura)
  Modelos: ver detectores.py (WA_DISPOSITIVO, WA_WHISPER_MODELO, WA_YOLO_MODELO…)
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from urllib.parse import quote

import pika

try:
    import psutil
except ImportError:          # sin psutil el latido no lleva CPU/RAM
    psutil = None

AQUI = Path(__file__).resolve().parent
# En el repositorio: distribuido/worker/worker.py; en Docker todo queda en /app
for ruta in (AQUI.parent, AQUI.parents[1] / "mensajeria", AQUI / "mensajeria"):
    if ruta.is_dir():
        sys.path.insert(0, str(ruta))

from comun import (COLA_RESULTADOS, EXCHANGE_CONTROL, EXCHANGE_LATIDOS, INTERVALO_LATIDO, PERSISTENTE,  # noqa: E402
                   POOLS, ConfiguracionIncompleta, config, declarar, parametros_rabbit)
from estadisticas import parcial  # noqa: E402
from whatsapp_formato import Mensaje  # noqa: E402

VERSION = 2
WORKER_ID = config("WORKER_ID", f"worker-{socket.gethostname()}")
MIS_POOLS = [p.strip() for p in config("WORKER_POOLS", ",".join(POOLS)).split(",") if p.strip()]
CONCURRENCIA = max(1, int(config("WORKER_CONCURRENCIA", "1")))
COORDINADOR_URL = config("COORDINADOR_URL", "http://localhost:8101").rstrip("/")
TOKEN = config("WORKER_TOKEN")
DIR_TRABAJO = Path(config("WORKER_DIR", tempfile.gettempdir())) / f"wa-{WORKER_ID}"
# Solo para demostraciones en vivo: segundos extra por sub-tarea para que se vea el reparto
# y se pueda provocar una caída a mitad. 0 en mediciones.
RETARDO_DEMO = float(config("WA_RETARDO_DEMO", "0"))
DETECCION = config("WA_DETECCION", "1") not in ("0", "no", "false")

try:   # los modelos son opcionales: sin ellos el worker sigue haciendo estadísticas y verificación
    import detectores
    FALTA_DETECCION = None
except ImportError as e:
    detectores, FALTA_DETECCION = None, f"falta {e.name}"


def detectar(funcion, *args):
    """Corre un detector sin tumbar la sub-tarea si los modelos no están: → (resultado, estado)."""
    if not DETECCION:
        return None, "desactivada (WA_DETECCION=0)"
    if detectores is None:
        return None, f"no disponible: {FALTA_DETECCION}"
    try:
        return funcion(*args), "ok"
    except detectores.DeteccionNoDisponible as e:
        return None, f"no disponible: {e}"

for p in MIS_POOLS:
    if p not in POOLS:
        raise SystemExit(f"Pool desconocido: {p}. Opciones: {', '.join(POOLS)}")

candado = threading.Lock()
contadores = {"en_curso": 0, "completadas": 0, "fallidas": 0}
cancelados: set[str] = set()
consumidor = {"conexion": None, "canal": None}


class Cancelada(Exception):
    """El coordinador canceló el trabajo de esta sub-tarea."""


class ErrorPermanente(Exception):
    """Reintentar no lo arreglaría (p. ej. archivo inexistente o token inválido)."""


def revisar_cancelacion(sub: dict) -> None:
    if sub["trabajo"] in cancelados:
        raise Cancelada()


def esperar_demo(sub: dict) -> None:
    fin = time.time() + RETARDO_DEMO
    while time.time() < fin:
        revisar_cancelacion(sub)
        time.sleep(0.1)


# ============================================================ procesamiento
def procesar_texto(sub: dict) -> dict:
    mensajes, anteriores = [], {}
    for d in sub["mensajes"]:
        mensajes.append(Mensaje(d["id"], d["linea"], d["fecha_hora"], d["autor"], d["texto"], d["tipo"], d.get("adjunto")))
        if d.get("previo"):
            anteriores[d["id"]] = tuple(d["previo"])
    esperar_demo(sub)
    revisar_cancelacion(sub)
    resultado = {"parcial": parcial(mensajes, anteriores), "hallazgos": []}
    textos = [m for m in mensajes if m.tipo == "texto" and m.texto.strip()]
    t0 = time.perf_counter()
    salida, estado = detectar(detectores.analizar_textos if detectores else None, [m.texto for m in textos])
    for m, detecciones in zip(textos, salida or []):
        if detecciones:
            resultado["hallazgos"].append({"id": m.id, "linea": m.linea, "autor": m.autor, "fecha_hora": m.fecha_hora,
                                           "texto": m.texto, "detecciones": detecciones})
    resultado["deteccion"] = {"estado": estado, "analizados": len(textos),
                              "segundos": round(time.perf_counter() - t0, 4)}
    return resultado


def metadatos(ruta: Path) -> dict:
    """Duración, formato, códecs y dimensiones con ffprobe (si está instalado)."""
    if not shutil.which("ffprobe"):
        return {"ffprobe": False}
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=duration,format_name:stream=codec_type,codec_name,width,height",
                            "-of", "json", str(ruta)], capture_output=True, text=True, timeout=60)
        datos = json.loads(r.stdout or "{}")
    except (subprocess.SubprocessError, ValueError, OSError) as e:
        return {"ffprobe": True, "error": str(e)[:200]}
    info: dict = {"ffprobe": True, "formato": (datos.get("format") or {}).get("format_name")}
    dur = (datos.get("format") or {}).get("duration")
    es_imagen = (info["formato"] or "").startswith(("image2", "png_pipe", "jpeg_pipe", "webp_pipe"))
    if dur and dur != "N/A" and not es_imagen:      # una foto no tiene duración
        info["duracion_s"] = round(float(dur), 2)
    for s in datos.get("streams", []):
        if s.get("codec_type") == "video" and s.get("width"):
            info.setdefault("ancho", s["width"])
            info.setdefault("alto", s["height"])
            info.setdefault("codec_video", s.get("codec_name"))
        elif s.get("codec_type") == "audio":
            info.setdefault("codec_audio", s.get("codec_name"))
    return info


def procesar_adjunto(sub: dict) -> dict:
    if not TOKEN:
        raise ErrorPermanente("WORKER_TOKEN no está configurado en este worker")
    url = f"{COORDINADOR_URL}/archivo/{quote(sub['caso'])}/{quote(sub['archivo'])}"
    carpeta = DIR_TRABAJO / sub["subtarea"]
    carpeta.mkdir(parents=True, exist_ok=True)
    destino = carpeta / Path(sub["archivo"]).name
    h, total = hashlib.sha256(), 0
    try:
        try:
            pedido = urllib.request.Request(url, headers={"X-Token": TOKEN})
            with urllib.request.urlopen(pedido, timeout=30) as r, open(destino, "wb") as f:
                while bloque := r.read(1024 * 1024):
                    revisar_cancelacion(sub)          # cancelación real: corta la descarga
                    h.update(bloque)
                    f.write(bloque)
                    total += len(bloque)
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404):
                raise ErrorPermanente(f"El coordinador respondió {e.code} para {sub['archivo']}") from e
            raise
        esperar_demo(sub)
        calculado = h.hexdigest()
        resultado = {"sha256_registrado": sub.get("sha256"), "sha256_calculado": calculado,
                     "coincide": calculado == sub.get("sha256"), "bytes": total, "metadatos": metadatos(destino)}
        funcion = {"imagen": "analizar_imagen", "voz": "analizar_audio", "audio": "analizar_audio"}.get(sub.get("tipo_archivo"))
        if funcion:
            revisar_cancelacion(sub)
            t0 = time.perf_counter()
            salida, estado = detectar(getattr(detectores, funcion) if detectores else None, str(destino))
            analisis, detecciones = salida or (None, [])
            resultado.update(analisis=analisis, detecciones=detecciones,
                             deteccion={"estado": estado, "segundos": round(time.perf_counter() - t0, 4)})
        return resultado
    finally:
        shutil.rmtree(carpeta, ignore_errors=True)      # libera el disco también si falla o se cancela


# ============================================================ consumo
def manejar(conexion, canal, etiqueta, reentregada: bool, cuerpo: bytes) -> None:
    """Hilo de trabajo de una sub-tarea."""
    sub = json.loads(cuerpo)

    def publicar(msg: dict, ack: bool = False) -> None:
        def _hacer():
            canal.basic_publish(exchange="", routing_key=COLA_RESULTADOS,
                                body=json.dumps({**msg, "trabajo": sub["trabajo"], "subtarea": sub["subtarea"],
                                                 "worker": WORKER_ID}, ensure_ascii=False).encode(),
                                properties=pika.BasicProperties(delivery_mode=PERSISTENTE))
            if ack:
                canal.basic_ack(delivery_tag=etiqueta)
        try:
            conexion.add_callback_threadsafe(_hacer)
        except Exception as e:  # noqa: BLE001
            print(f"  [!] Conexión perdida; RabbitMQ re-entregará {sub['subtarea']}: {e}")

    if sub["trabajo"] in cancelados:
        publicar({"evento": "cancelada"}, ack=True)
        return
    with candado:
        contadores["en_curso"] += 1
    descripcion = f"{len(sub['mensajes'])} mensajes" if sub["tipo"] == "texto" else f"{sub.get('tipo_archivo')}: {sub['archivo']}"
    print(f"  [←] {sub['subtarea']} ({sub['tipo']}: {descripcion}){' · re-entregada' if reentregada else ''}")
    publicar({"evento": "inicio", "reentregada": reentregada})
    t0 = time.perf_counter()
    try:
        resultado = procesar_texto(sub) if sub["tipo"] == "texto" else procesar_adjunto(sub)
        fin = {"evento": "fin", "estado": "completed", "resultado": resultado}
        clave = "completadas"
    except Cancelada:
        fin, clave = {"evento": "cancelada"}, None
        print(f"  [■] {sub['subtarea']} cancelada; recursos liberados")
    except ErrorPermanente as e:
        fin, clave = {"evento": "fin", "estado": "failed", "reintentable": False, "error": str(e)}, "fallidas"
    except Exception as e:  # noqa: BLE001 — red, disco…: transitorio, el coordinador puede reintentar
        fin = {"evento": "fin", "estado": "failed", "reintentable": True, "error": f"{type(e).__name__}: {str(e)[:300]}"}
        clave = "fallidas"
    fin["duracion"] = round(time.perf_counter() - t0, 4)
    with candado:
        contadores["en_curso"] -= 1
        if clave:
            contadores[clave] += 1
    if fin.get("estado"):
        print(f"  [{'✓' if fin['estado'] == 'completed' else '✗'}] {sub['subtarea']} en {fin['duracion']} s"
              f"{'' if fin['estado'] == 'completed' else ' — ' + fin['error']}")
    publicar(fin, ack=True)


def al_mensaje(canal, metodo, _props, cuerpo) -> None:
    threading.Thread(target=manejar, daemon=True,
                     args=(consumidor["conexion"], canal, metodo.delivery_tag, metodo.redelivered, cuerpo)).start()


def al_control(_canal, _metodo, _props, cuerpo) -> None:
    try:
        orden = json.loads(cuerpo)
    except ValueError:
        return
    if orden.get("cancelar"):
        cancelados.add(orden["cancelar"])
        print(f"  [■] Trabajo {orden['cancelar']} cancelado por el coordinador")


def consumir() -> None:
    while True:
        try:
            conexion = pika.BlockingConnection(parametros_rabbit())
            canal = conexion.channel()
            declarar(canal)
            canal.basic_qos(prefetch_count=CONCURRENCIA, global_qos=True)   # máx. sub-tareas sin confirmar
            cola_control = canal.queue_declare(queue="", exclusive=True).method.queue
            canal.queue_bind(queue=cola_control, exchange=EXCHANGE_CONTROL)
            canal.basic_consume(queue=cola_control, on_message_callback=al_control, auto_ack=True)
            for p in MIS_POOLS:
                canal.basic_consume(queue=POOLS[p], on_message_callback=al_mensaje)
            consumidor["conexion"], consumidor["canal"] = conexion, canal
            print(f"[*] {WORKER_ID} esperando sub-tareas de: {', '.join(MIS_POOLS)} (hasta {CONCURRENCIA} a la vez)")
            while True:
                conexion.process_data_events(time_limit=1)
        except KeyboardInterrupt:
            print(f"\n[*] {WORKER_ID} detenido. Lo que no terminó vuelve a la cola.")
            try:
                conexion.close()
            except Exception:  # noqa: BLE001
                pass
            return
        except ConfiguracionIncompleta as e:
            raise SystemExit(f"[✗] {e}") from e
        except Exception as e:  # noqa: BLE001
            print(f"[!] Sin conexión con RabbitMQ ({type(e).__name__}). Reintentando en 5 s…")
            time.sleep(5)


# ============================================================ latidos
def maquina() -> dict:
    cpu = platform.processor() or ""
    try:
        with open("/proc/cpuinfo") as f:
            cpu = next((l.split(":", 1)[1].strip() for l in f if l.startswith("model name")), cpu)
    except OSError:
        pass
    en_docker = os.path.exists("/.dockerenv")
    return {"hostname": config("HOST_NAME") or socket.gethostname(), "cpu": re.sub(r"\s+", " ", cpu)[:80] or "desconocido",
            "nucleos": os.cpu_count(), "ram_gb": round(psutil.virtual_memory().total / 2 ** 30, 1) if psutil else None,
            "so": f"{platform.system()} {platform.release()}" + (" (Docker)" if en_docker else ""),
            "usuario": "root" if hasattr(os, "geteuid") and os.geteuid() == 0 else "sin privilegios"}


def latidos() -> None:
    """Cada INTERVALO_LATIDO s publica sus métricas (conexión propia)."""
    info = maquina()
    if psutil:
        psutil.cpu_percent(interval=None)
    while True:
        try:
            conexion = pika.BlockingConnection(parametros_rabbit())
            canal = conexion.channel()
            declarar(canal)
            while True:
                with candado:
                    datos = dict(contadores)
                proceso = psutil.Process() if psutil else None
                canal.basic_publish(exchange=EXCHANGE_LATIDOS, routing_key="", body=json.dumps({
                    "worker": WORKER_ID, "version": VERSION, "pools": MIS_POOLS, "concurrencia": CONCURRENCIA,
                    **datos, "maquina": info,
                    "cpu": psutil.cpu_percent(interval=None) if psutil else None,
                    "ram": psutil.virtual_memory().percent if psutil else None,
                    "rss_mb": round(proceso.memory_info().rss / 2 ** 20, 1) if proceso else None,
                    "deteccion": ("desactivada" if not DETECCION else f"no disponible: {FALTA_DETECCION}"
                                  if detectores is None else "ok"),
                    "dispositivo": detectores.dispositivo() if detectores and DETECCION else None,
                    "modelos": detectores.modelos_cargados() if detectores else {},
                }).encode())
                conexion.sleep(INTERVALO_LATIDO)            # atiende los heartbeats mientras espera
        except ConfiguracionIncompleta:
            return
        except Exception:  # noqa: BLE001
            time.sleep(5)


if __name__ == "__main__":
    m = maquina()
    print("=" * 60)
    print(f"WORKER {WORKER_ID} (v{VERSION}) · pools: {', '.join(MIS_POOLS)} · concurrencia {CONCURRENCIA}")
    print(f"Máquina: {m['hostname']} · {m['cpu']} · {m['nucleos']} núcleos · {m['so']} · {m['usuario']}")
    print(f"Coordinador (adjuntos): {COORDINADOR_URL}" + ("" if TOKEN else "  [!] sin WORKER_TOKEN: no podrá verificar adjuntos"))
    print(f"Detección: {'desactivada' if not DETECCION else FALTA_DETECCION or 'ok, dispositivo ' + detectores.dispositivo()}")
    if RETARDO_DEMO:
        print(f"[!] Modo demostración: +{RETARDO_DEMO:g} s por sub-tarea (no usar para medir)")
    print("=" * 60)
    DIR_TRABAJO.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=latidos, daemon=True).start()
    consumir()
