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
  M1 (memoria, ver memoria.py): WA_ADMISION=1 (0 = sin control de admisión, para provocar OOM a propósito),
  WA_MEMORIA_MB (presupuesto fijo; si no, el límite del cgroup), WA_MARGEN_MEMORIA=0.10
  Modelos: ver detectores.py (WA_DISPOSITIVO, WA_WHISPER_MODELO, WA_YOLO_MODELO…)
"""

from __future__ import annotations

import hashlib
import ssl  # M4 HTTPS
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
from urllib.parse import quote, urlsplit
import http.client
from concurrent.futures import ThreadPoolExecutor

import pika

try:
    import psutil
except ImportError:          # sin psutil el latido no lleva CPU/RAM
    psutil = None

AQUI = Path(__file__).resolve().parent
# En el repositorio: distribuido/worker/worker.py; en Docker todo queda en /app
for ruta in (AQUI.parent, AQUI.parent.parent / "mensajeria", AQUI / "mensajeria"):
    if ruta.is_dir():
        sys.path.insert(0, str(ruta))

from comun import (COLA_RESULTADOS, EXCHANGE_CONTROL, EXCHANGE_LATIDOS, INTERVALO_LATIDO, PERSISTENTE,  # noqa: E402
                   POOLS, ConfiguracionIncompleta, config, declarar, parametros_rabbit)
import memoria  # noqa: E402
from estadisticas import parcial  # noqa: E402
from whatsapp_formato import Mensaje  # noqa: E402

VERSION = 2
WORKER_ID = config("WORKER_ID", f"worker-{socket.gethostname()}")
MIS_POOLS = [p.strip() for p in config("WORKER_POOLS", ",".join(POOLS)).split(",") if p.strip()]
CONCURRENCIA = max(1, int(config("WORKER_CONCURRENCIA", "1")))
COORDINADOR_URL = config("COORDINADOR_URL", "https://localhost:8101").rstrip("/")
TOKEN = config("WORKER_TOKEN")
DIR_TRABAJO = Path(config("WORKER_DIR", tempfile.gettempdir())) / f"wa-{WORKER_ID}"
# Solo para demostraciones en vivo: segundos extra por sub-tarea para que se vea el reparto
# y se pueda provocar una caída a mitad. 0 en mediciones.
RETARDO_DEMO = float(config("WA_RETARDO_DEMO", "0"))
DETECCION = config("WA_DETECCION", "1") not in ("0", "no", "false")
ADMISION = config("WA_ADMISION", "1") not in ("0", "no", "false")
REINICIOS = int(config("WA_REINICIOS", "0"))           # los pone supervisor.py
SUPERVISADO = config("WA_SUPERVISADO") == "1"

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
contadores = {"en_curso": 0, "completadas": 0, "fallidas": 0, "rechazadas_oom": 0}
PLAN: dict = {}                       # plan de memoria del nodo (memoria.planificar), se calcula al arrancar
admision: memoria.Admision | None = None
registro: memoria.Registro | None = None
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


# ============================================================ M3: transferencia por bloques
BLOQUE = int(config("WA_BLOQUE_KB", "1024")) * 1024          # tamaño de bloque (justificado en docs/modulo-M3-…)
PARALELAS = max(1, int(config("WA_DESCARGAS_PARALELAS", "4")))  # conexiones simultáneas por archivo
REINTENTOS_BLOQUE = 3


class ErrorBloque(Exception):
    pass


def _contexto_https_m4():
    contexto = ssl.create_default_context()
    ruta_ca = config("COORDINADOR_CA_FILE", str(Path(__file__).resolve().parents[1] / "certs" / "ca.crt"))
    if ruta_ca and Path(ruta_ca).is_file():
        contexto.load_verify_locations(cafile=str(ruta_ca))
    return contexto


def _conexion(url_base: str):
    u = urlsplit(url_base)
    clase = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    conexion = (clase(u.hostname, u.port, timeout=30, context=_contexto_https_m4())
                if u.scheme == "https" else clase(u.hostname, u.port, timeout=30))
    return conexion, u.path.rstrip("/")


def _pedir(con, ruta: str, cabeceras: dict) -> tuple[int, bytes]:
    con.request("GET", ruta, headers={"X-Token": TOKEN, **cabeceras})
    r = con.getresponse()
    return r.status, r.read()


def base_archivos(sub: dict) -> str:
    """Dónde descargar: la dirección que manda el coordinador en la tarea (su túnel actual) o COORDINADOR_URL.
    Un worker de la misma PC o la misma red prefiere COORDINADOR_URL si lo configuraron a mano (WA_PREFERIR_LOCAL=1)."""
    if config("WA_PREFERIR_LOCAL") == "1" or not sub.get("url_archivos"):
        return COORDINADOR_URL
    return sub["url_archivos"].rstrip("/")


def descargar_por_bloques(sub: dict, destino: Path) -> dict:
    """Descarga un adjunto en bloques de BLOQUE bytes con PARALELAS conexiones keep-alive:
    1) pide el manifiesto (SHA-256 de cada bloque); 2) cada hilo pide sus bloques con HTTP Range,
    verifica el hash del bloque (si no coincide, lo vuelve a pedir: solo ESE bloque) y lo escribe
    en su posición con os.pwrite (reensamblado); 3) verifica el SHA-256 del archivo completo.
    Devuelve métricas de la transferencia."""
    ruta = f"{quote(sub['caso'])}/{quote(sub['archivo'])}"
    origen = base_archivos(sub)
    con, base = _conexion(origen)
    try:
        estado, cuerpo = _pedir(con, f"{base}/manifiesto/{ruta}?bloque={BLOQUE}", {})
    finally:
        con.close()
    if estado in (401, 403):
        raise ErrorPermanente(f"El coordinador respondió {estado} para {sub['archivo']}")
    if estado != 200:          # 404: coordinador sin manifiesto (o archivo inexistente: lo dirá la descarga completa)
        raise ErrorBloque(f"manifiesto: HTTP {estado}")
    man = json.loads(cuerpo)
    n, tam, total = len(man["bloques"]), man["bloque"], man["bytes"]
    reintentos = [0]
    t0 = time.perf_counter()
    fd = os.open(destino, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.ftruncate(fd, total)                         # reserva el tamaño final: cada bloque va a su lugar

        def trabajar(indices: list[int]) -> None:
            con, base = _conexion(origen)     # una conexión keep-alive por hilo
            try:
                for i in indices:
                    for intento in range(REINTENTOS_BLOQUE + 1):
                        revisar_cancelacion(sub)
                        a, b = i * tam, min(total, (i + 1) * tam) - 1
                        try:
                            estado, datos = _pedir(con, f"{base}/archivo/{ruta}", {"Range": f"bytes={a}-{b}"})
                        except (OSError, http.client.HTTPException):
                            con.close()
                            con, base = _conexion(origen)
                            estado, datos = 0, b""
                        if estado == 206 and hashlib.sha256(datos).hexdigest() == man["bloques"][i]:
                            os.pwrite(fd, datos, a)
                            break
                        reintentos[0] += 1             # bloque dañado o corte: se repite SOLO este bloque
                    else:
                        raise ErrorBloque(f"el bloque {i} falló {REINTENTOS_BLOQUE + 1} veces")
            finally:
                con.close()
        grupos = [list(range(k, n, PARALELAS)) for k in range(min(PARALELAS, n))] or [[]]
        with ThreadPoolExecutor(len(grupos)) as ex:
            for f in [ex.submit(trabajar, g) for g in grupos]:
                f.result()
    finally:
        os.close(fd)
    segundos = time.perf_counter() - t0
    calculado = memoria_sha256(destino)
    return {"sha256_calculado": calculado, "bytes": total,
            "transferencia": {"bloque_bytes": tam, "bloques": n, "paralelas": PARALELAS, "reintentos_bloque": reintentos[0],
                              "segundos": round(segundos, 3), "mb_s": round(total / 2**20 / segundos, 2) if segundos else None}}


def memoria_sha256(ruta: Path) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        while b := f.read(2**20):
            h.update(b)
    return h.hexdigest()


def descargar_completo(url: str, sub: dict, destino: Path) -> tuple[str, int]:
    """Descarga de una sola vez (versión anterior a M3); se usa si el coordinador no tiene manifiesto."""
    h, total = hashlib.sha256(), 0
    try:
        pedido = urllib.request.Request(url, headers={"X-Token": TOKEN})
        with urllib.request.urlopen(pedido, timeout=30, context=_contexto_https_m4() if url.startswith("https://") else None) as r, open(destino, "wb") as f:
            while bloque := r.read(1024 * 1024):
                revisar_cancelacion(sub)          # cancelación real: corta la descarga
                h.update(bloque)
                f.write(bloque)
                total += len(bloque)
    except urllib.error.HTTPError as e:
        if e.code in (401, 403, 404):
            raise ErrorPermanente(f"El coordinador respondió {e.code} para {sub['archivo']}") from e
        raise
    return h.hexdigest(), total


def procesar_adjunto(sub: dict) -> dict:
    if not TOKEN:
        raise ErrorPermanente("WORKER_TOKEN no está configurado en este worker")
    url = f"{base_archivos(sub)}/archivo/{quote(sub['caso'])}/{quote(sub['archivo'])}"
    carpeta = DIR_TRABAJO / sub["subtarea"]
    carpeta.mkdir(parents=True, exist_ok=True)
    destino = carpeta / Path(sub["archivo"]).name
    try:
        transferencia = None
        try:   # M3: por bloques verificados (si el coordinador es de una versión sin manifiesto, de una vez)
            r = descargar_por_bloques(sub, destino)
            calculado, total, transferencia = r["sha256_calculado"], r["bytes"], r["transferencia"]
        except ErrorBloque:
            calculado, total = descargar_completo(url, sub, destino)
        esperar_demo(sub)
        resultado = {"sha256_registrado": sub.get("sha256"), "sha256_calculado": calculado,
                     "coincide": calculado == sub.get("sha256"), "bytes": total, "metadatos": metadatos(destino),
                     "transferencia": transferencia}
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
    # ---- M1: política de reencolado tras OOM (las cuentas las anota supervisor.py)
    ooms = registro.oom_de(sub["subtarea"]) if registro else 0
    if ooms >= memoria.MAX_OOM:
        print(f"  [✗] {sub['subtarea']} ya provocó {ooms} OOM en este nodo: se rechaza para que la tome otro")
        with candado:
            contadores["rechazadas_oom"] += 1
        publicar({"evento": "fin", "estado": "failed", "reintentable": True, "oom": ooms,
                  "error": f"OOM: superó la memoria de {WORKER_ID} ({memoria.limite_mb() or '?'} MB) {ooms} veces"}, ack=True)
        return
    exclusiva = ooms == 1               # 2.º intento en este nodo: corre sola, sin otras sub-tareas en paralelo
    pool = sub["tipo"] if sub["tipo"] == "texto" else memoria_pool(sub)
    # ---- M1: control de admisión (espera hasta que haya memoria para esta sub-tarea)
    reservado = admision.entrar(pool, exclusiva, cancelada=lambda: sub["trabajo"] in cancelados) if admision else 0
    if sub["trabajo"] in cancelados:
        if admision:
            admision.salir(reservado, exclusiva)
        publicar({"evento": "cancelada"}, ack=True)
        return
    if registro:
        registro.empezar(sub)
    with candado:
        contadores["en_curso"] += 1
    if exclusiva:
        print(f"  [M1] {sub['subtarea']} provocó un OOM antes: se procesa sola (modo exclusivo)")
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
    if admision:
        admision.salir(reservado, exclusiva)
    if registro:
        registro.terminar(sub)
    with candado:
        contadores["en_curso"] -= 1
        if clave:
            contadores[clave] += 1
    if fin.get("estado"):
        print(f"  [{'✓' if fin['estado'] == 'completed' else '✗'}] {sub['subtarea']} en {fin['duracion']} s"
              f"{'' if fin['estado'] == 'completed' else ' — ' + fin['error']}")
    publicar(fin, ack=True)


def memoria_pool(sub: dict) -> str:
    """Pool de un adjunto según su tipo (la huella de memoria depende del modelo que use)."""
    return {"imagen": "imagen", "voz": "audio", "audio": "audio"}.get(sub.get("tipo_archivo"), "adjuntos")


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
    if orden.get("reanudar"):
        cancelados.discard(orden["reanudar"])
        print(f"  [▶] Trabajo {orden['reanudar']} reanudado por el coordinador")


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
                # M5: prioridad de consumidor. RabbitMQ entrega cada mensaje al consumidor de mayor prioridad
                # que tenga lugar libre; uno de menor prioridad solo recibe cuando los de mayor están ocupados.
                # Así un nodo sin GPU ayuda con texto/fotos/audio solo cuando los nodos con GPU están llenos.
                canal.basic_consume(queue=POOLS[p], on_message_callback=al_mensaje,
                                    arguments={"x-priority": prioridad_consumidor(p)})
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
            print(f"[✗] {e}")
            raise SystemExit(78) from e       # EX_CONFIG: el supervisor no reinicia un error de configuración
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


def prioridad_consumidor(pool: str) -> int:
    """WA_PRIORIDAD_<POOL> (p. ej. WA_PRIORIDAD_TEXTO=0) o, por defecto, 10 si el nodo tiene GPU y 0 si no
    (los modelos son entre 5 y 25 veces más rápidos en GPU, ver docs/modulo-M5-heterogeneo.md).
    Los adjuntos (hash y metadatos, sin modelo) no tienen preferencia."""
    fijo = config(f"WA_PRIORIDAD_{pool.upper()}", "")
    if fijo.strip():
        return int(fijo)
    if pool == "adjuntos" or not (detectores and DETECCION):
        return 0
    return 10 if detectores.dispositivo() == "cuda" else 0


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
                    "memoria": {**memoria.estado_cgroup(), **(admision.estado() if admision else {}),
                                "rss_mb": round(proceso.memory_info().rss / 2 ** 20, 1) if proceso else None,
                                "pss_mb": memoria.proceso_mb().get("pss"),
                                "presupuesto_mb": PLAN.get("presupuesto_mb"), "persistente_mb": PLAN.get("persistente_mb"),
                                "admision": PLAN.get("admision"), "rechazados": PLAN.get("rechazados"),
                                "decisiones": PLAN.get("decisiones"), "reinicios": REINICIOS, "supervisado": SUPERVISADO},
                }).encode())
                conexion.sleep(float(config("WA_INTERVALO_LATIDO", str(INTERVALO_LATIDO))))            # atiende los heartbeats mientras espera
        except ConfiguracionIncompleta:
            return
        except Exception:  # noqa: BLE001
            time.sleep(5)


def preparar_memoria() -> None:
    """M1: con el límite del cgroup y las huellas medidas, decide qué acepta este nodo."""
    global PLAN, admision, registro, MIS_POOLS, CONCURRENCIA
    try:   # que el kernel elija a este proceso (y no al supervisor) si hay que matar a alguien por memoria
        Path("/proc/self/oom_score_adj").write_text("500")
    except OSError:
        pass
    presupuesto, origen = memoria.presupuesto_mb()
    disp = detectores.dispositivo() if detectores and DETECCION else "cpu"
    texto_modelo = config("WA_TEXTO_MODELO", "1") not in ("0", "no", "false")
    # Con Ollama el modelo de texto vive en otro proceso (el servidor de Ollama): este worker no carga
    # pysentimiento, así que el plan no le reserva esa memoria (y le queda más para YOLO o Whisper).
    ollama = bool(texto_modelo and detectores and DETECCION and "texto" in MIS_POOLS
                  and detectores.motor_texto() == "ollama" and detectores.OLLAMA_CANDIDATOS == "ollama")
    # (en el modo combinado RoBERTuito sí se carga en el worker: el plan le reserva su memoria como antes)
    PLAN = memoria.planificar(MIS_POOLS, presupuesto, memoria.proceso_mb().get("rss") or 60,
                              texto_modelo=texto_modelo and detectores is not None and not ollama,
                              whisper_preferido=detectores.tamano_whisper(disp) if detectores else "small",
                              deteccion=DETECCION and detectores is not None,
                              concurrencia_max=CONCURRENCIA, admision=ADMISION)
    PLAN["origen"] = origen
    if detectores and DETECCION:
        detectores.configurar(texto_modelo=PLAN["texto_modelo"] or ollama, whisper=PLAN["whisper"])
    PLAN["texto_motor"] = "ollama" if ollama else ("pysentimiento" if PLAN["texto_modelo"] else "reglas")
    MIS_POOLS, CONCURRENCIA = PLAN["pools"], PLAN["concurrencia"]
    admision = memoria.Admision(PLAN)
    registro = memoria.Registro(DIR_TRABAJO)
    lim = memoria.limite_mb()
    print(f"Memoria (M1): límite del cgroup {f'{lim:.0f} MB' if lim else 'sin límite'} · presupuesto "
          f"{f'{presupuesto:.0f} MB' if presupuesto else '?'} ({origen}) · "
          f"{'admisión activa' if ADMISION else 'ADMISIÓN DESACTIVADA (WA_ADMISION=0)'}")
    print(f"  plan: {', '.join(MIS_POOLS) or 'ningún pool'} · ~{PLAN['persistente_mb']:.0f} MB fijos (modelos) · "
          f"concurrencia {CONCURRENCIA} (por memoria: {PLAN['concurrencia_por_memoria']})"
          + (" · [huellas provisionales: correr distribuido/m1/medir_modelos.py]" if PLAN["huellas_provisionales"] else ""))
    for d in PLAN["decisiones"]:
        print(f"  degradado: {d}")
    for p, motivo in PLAN["rechazados"].items():
        print(f"  [!] no atiende «{p}»: {motivo}")
    if REINICIOS:
        print(f"  [supervisor] reinicio n.º {REINICIOS}")


if __name__ == "__main__":
    DIR_TRABAJO.mkdir(parents=True, exist_ok=True)
    preparar_memoria()
    m = maquina()
    print("=" * 60)
    print(f"WORKER {WORKER_ID} (v{VERSION}) · pools: {', '.join(MIS_POOLS)} · concurrencia {CONCURRENCIA}")
    print("Prioridad por cola (M5; mayor = recibe primero): "
          + ", ".join(f"{p} {prioridad_consumidor(p)}" for p in MIS_POOLS))
    print(f"Máquina: {m['hostname']} · {m['cpu']} · {m['nucleos']} núcleos · {m['so']} · {m['usuario']}")
    print(f"Coordinador (adjuntos): la dirección que mande el coordinador en cada tarea (si no, {COORDINADOR_URL})" + ("" if TOKEN else "  [!] sin WORKER_TOKEN: no podrá verificar adjuntos"))
    print(f"Detección: {'desactivada' if not DETECCION else FALTA_DETECCION or 'ok, dispositivo ' + detectores.dispositivo()}")
    if RETARDO_DEMO:
        print(f"[!] Modo demostración: +{RETARDO_DEMO:g} s por sub-tarea (no usar para medir)")
    print("=" * 60)
    threading.Thread(target=latidos, daemon=True).start()
    consumir()
