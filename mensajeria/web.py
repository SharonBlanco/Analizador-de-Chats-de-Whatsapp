"""
Interfaz web local para probar el importador de chats de WhatsApp.

Usa el mismo código que la plataforma (whatsapp_formato.py + importar.py), así que lo
que se ve aquí es exactamente lo que recibirán el coordinador y los workers.
Solo biblioteca estándar: no requiere instalar nada.

Uso:
  python mensajeria/web.py                    → http://localhost:8100
  python mensajeria/web.py --puerto 9000 --salida dataset_whatsapp

Escucha solo en 127.0.0.1: los chats no salen de esta computadora.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import mimetypes
import re
import shutil
import sys
import tempfile
import threading
import uuid
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

AQUI = Path(__file__).resolve().parent
sys.path.insert(0, str(AQUI))
from importar import ErrorImportacion, importar  # noqa: E402
from whatsapp_formato import parsear_archivo  # noqa: E402
from estadisticas import calcular as calcular_estadisticas  # noqa: E402
from almacen import Almacen  # noqa: E402  (M3)
from receptor import Receptor  # noqa: E402  (M3)
from organizacion import ErrorOrganizacion, Organizacion  # noqa: E402
import telefono  # noqa: E402  (importar desde un teléfono Android por USB)

# Procesamiento en paralelo (distribuido/). Requiere pika; sin él, la interfaz funciona
# igual y la sección de workers explica qué falta.
sys.path.insert(0, str(AQUI.parent / "distribuido"))
try:
    from comun import config as config_distribuido  # noqa: E402
    from coordinador import ErrorTrabajo, Plataforma  # noqa: E402
    FALTA_DISTRIBUIDO = None
except ImportError as e:   # pragma: no cover
    FALTA_DISTRIBUIDO = f"Falta un módulo para el procesamiento en paralelo: {e.name} (pip install pika psutil)"

    class ErrorTrabajo(Exception):
        pass

INDEX = AQUI / "web" / "index.html"
SUBIDAS = Path(tempfile.gettempdir()) / "wa_import_web"
LOTE_VALIDO = re.compile(r"^[a-f0-9]{8,32}$")
FECHA_VALIDA = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SIN_CARPETA = "_sin"
MAX_SUBIDA = 4 * 2 ** 30
mimetypes.add_type("audio/ogg", ".opus")
mimetypes.add_type("image/webp", ".webp")

DATASET: Path = Path("dataset_whatsapp")
ORG: Organizacion = Organizacion(DATASET)
PLATAFORMA = None          # coordinador.Plataforma, se crea en main()

# Caché de chats parseados: {caso: (mtime_ns de chat.txt, mensajes)}.
# El panorama general recorre todos los chats; sin caché se releería cada .txt.
_cache: dict[str, tuple[int, list]] = {}
_cache_lock = threading.Lock()


def mensajes_de(carpeta: Path) -> list:
    chat = carpeta / "chat.txt"
    marca = chat.stat().st_mtime_ns
    with _cache_lock:
        guardado = _cache.get(carpeta.name)
        if guardado and guardado[0] == marca:
            return guardado[1]
    mensajes = parsear_archivo(chat)
    with _cache_lock:
        _cache[carpeta.name] = (marca, mensajes)
    return mensajes


def cargar_caso(caso: str) -> tuple[list, dict]:
    """Para el coordinador: mensajes parseados y metadata.json de un caso."""
    carpeta = DATASET / caso
    if not dentro(DATASET, carpeta) or not (carpeta / "metadata.json").is_file():
        raise ErrorTrabajo(f"El caso {caso} no existe")
    return mensajes_de(carpeta), json.loads((carpeta / "metadata.json").read_text(encoding="utf-8"))


def estado_plataforma() -> dict:
    if PLATAFORMA is None:
        return {"configurado": False, "conectado": False, "workers": [], "trabajos": [],
                "error": FALTA_DISTRIBUIDO or "La plataforma no se inició"}
    return PLATAFORMA.estado()


def carpetas_de_casos() -> list[Path]:
    if not DATASET.is_dir():
        return []
    return sorted((c for c in DATASET.iterdir() if c.is_dir() and (c / "metadata.json").is_file()),
                  key=lambda p: p.stat().st_mtime, reverse=True)


def dentro(base: Path, ruta: Path) -> bool:
    base, ruta = base.resolve(), ruta.resolve()
    return ruta == base or base in ruta.parents


def resumen_caso(carpeta: Path, asignaciones: dict | None = None) -> dict | None:
    try:
        m = json.loads((carpeta / "metadata.json").read_text(encoding="utf-8"))
        mensajes = mensajes_de(carpeta)
    except (OSError, json.JSONDecodeError):
        return None
    por_dia: dict[str, int] = {}
    for x in mensajes:
        if x.tipo != "sistema":
            por_dia[x.fecha_hora[:10]] = por_dia.get(x.fecha_hora[:10], 0) + 1
    if asignaciones is None:
        asignaciones = ORG.estado()["asignaciones"]
    # Peso del chat: el .txt más sus adjuntos (lo que ocupa en disco, sin metadata.json)
    bytes_chat = (carpeta / "chat.txt").stat().st_size
    bytes_adjuntos = sum(f.stat().st_size for f in carpeta.iterdir()
                         if f.is_file() and f.name not in ("chat.txt", "metadata.json"))
    return {"caso": carpeta.name, "chat": m.get("chat"), "fuente": m.get("fuente"),
            "bytes": bytes_chat + bytes_adjuntos, "bytes_chat": bytes_chat, "bytes_adjuntos": bytes_adjuntos,
            "mensajes": m.get("total_mensajes"), "participantes": len(m.get("participantes", [])),
            "nombres": m.get("participantes", []),
            "inicio": (m.get("periodo") or {}).get("inicio"), "fin": (m.get("periodo") or {}).get("fin"),
            "por_dia": por_dia,     # mensajes de participantes por fecha (búsqueda por fecha)
            "carpeta": asignaciones.get(carpeta.name),
            "importado": (m.get("importado") or {}).get("fecha"), "avisos": len(m.get("avisos", []))}


def estadisticas_generales(carpeta: str, desde: str, hasta: str) -> dict:
    """Estadísticas de todas las conversaciones (opcionalmente de una carpeta y un rango
    de fechas). Mismas métricas que por chat, salvo el tiempo de respuesta."""
    asignaciones = ORG.estado()["asignaciones"]
    todos, por_conversacion, adjuntos = [], [], 0
    for c in carpetas_de_casos():
        asignada = asignaciones.get(c.name)
        if carpeta == SIN_CARPETA and asignada is not None:
            continue
        if carpeta not in ("", SIN_CARPETA) and asignada != carpeta:
            continue
        try:
            meta = json.loads((c / "metadata.json").read_text(encoding="utf-8"))
            mensajes = mensajes_de(c)
        except (OSError, json.JSONDecodeError):
            continue
        elegidos = [m for m in mensajes
                    if (not desde or m.fecha_hora[:10] >= desde) and (not hasta or m.fecha_hora[:10] <= hasta)]
        if not elegidos:
            continue
        todos.extend(elegidos)
        adjuntos += sum(1 for m in elegidos if m.adjunto)
        usuarios = [m for m in elegidos if m.tipo != "sistema"]
        por_conversacion.append({
            "caso": c.name, "chat": meta.get("chat"), "mensajes": len(usuarios),
            "participantes": len({m.autor for m in usuarios}),
            "inicio": min(m.fecha_hora for m in elegidos), "fin": max(m.fecha_hora for m in elegidos)})
    e = calcular_estadisticas(todos, respuestas=False)
    por_conversacion.sort(key=lambda x: -x["mensajes"])
    e["por_conversacion"] = por_conversacion
    e["resumen"]["conversaciones"] = len(por_conversacion)
    e["resumen"]["adjuntos"] = adjuntos
    e["resumen"]["inicio"] = min((m.fecha_hora for m in todos), default=None)
    e["resumen"]["fin"] = max((m.fecha_hora for m in todos), default=None)
    e["filtro"] = {"carpeta": carpeta, "desde": desde, "hasta": hasta}
    return e


class Manejador(BaseHTTPRequestHandler):
    server_version = "ImportadorWA/1.0"

    # ---------------------------------------------------------------- utilidades
    def log_message(self, fmt, *args):
        sys.stderr.write(f"  {self.command} {self.path.split('?')[0]} → {args[1] if len(args) > 1 else ''}\n")

    def _json(self, datos, estado=200):
        cuerpo = json.dumps(datos, ensure_ascii=False).encode()
        self.send_response(estado)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(cuerpo)))
        self.end_headers()
        self.wfile.write(cuerpo)

    def _error(self, mensaje, estado=400):
        self._json({"error": mensaje}, estado)

    def _leer_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _ruta(self):
        u = urlparse(self.path)
        return [unquote(p) for p in u.path.strip("/").split("/") if p], parse_qs(u.query)

    def _caso(self, nombre: str) -> Path | None:
        carpeta = DATASET / nombre
        return carpeta if dentro(DATASET, carpeta) and (carpeta / "metadata.json").is_file() else None

    # ---------------------------------------------------------------- GET
    def do_GET(self):
        partes, qs = self._ruta()
        if not partes:
            cuerpo = INDEX.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(cuerpo)))
            self.end_headers()
            self.wfile.write(cuerpo)
        elif partes == ["api", "casos"]:
            org = ORG.estado()
            casos = [r for c in carpetas_de_casos() if (r := resumen_caso(c, org["asignaciones"]))]
            riesgos = PLATAFORMA.riesgo_por_caso() if PLATAFORMA else {}
            en_curso = PLATAFORMA.en_curso_por_caso() if PLATAFORMA else {}
            for r in casos:
                r["riesgo"] = riesgos.get(r["caso"])          # None = nunca se procesó con detección
                r["procesando"] = en_curso.get(r["caso"])     # avance si hay un procesamiento en curso
            conteo: dict[str, int] = {}
            for r in casos:
                conteo[r["carpeta"] or SIN_CARPETA] = conteo.get(r["carpeta"] or SIN_CARPETA, 0) + 1
            carpetas = [{**c, "conversaciones": conteo.get(c["id"], 0)}
                        for c in sorted(org["carpetas"], key=lambda c: c["nombre"].lower())]
            self._json({"carpeta": str(DATASET.resolve()), "casos": casos, "carpetas": carpetas,
                        "sin_carpeta": conteo.get(SIN_CARPETA, 0)})
        elif partes == ["api", "general"]:
            carpeta = (qs.get("carpeta") or [""])[0]
            desde, hasta = (qs.get("desde") or [""])[0], (qs.get("hasta") or [""])[0]
            if any(f and not FECHA_VALIDA.match(f) for f in (desde, hasta)):
                return self._error("Fecha inválida (use AAAA-MM-DD)")
            self._json(estadisticas_generales(carpeta, desde, hasta))
        elif len(partes) == 3 and partes[:2] == ["api", "casos"]:
            carpeta = self._caso(partes[2])
            if not carpeta:
                return self._error("Caso no encontrado", 404)
            meta = json.loads((carpeta / "metadata.json").read_text(encoding="utf-8"))
            parseados = mensajes_de(carpeta)
            mensajes = [m.a_dict() for m in parseados]
            for m in mensajes:
                m.pop("linea_cruda", None)
            self._json({"caso": carpeta.name, "metadata": meta, "mensajes": mensajes,
                        "estadisticas": calcular_estadisticas(parseados),
                        "carpeta": ORG.carpeta_de(carpeta.name)})
        elif partes == ["api", "telefono"]:
            self._json(telefono.dispositivos())
        elif partes == ["api", "telefono", "chats"]:
            serial = (qs.get("serial") or [""])[0]
            try:
                self._json({"chats": telefono.chats_recientes(serial)})
            except Exception as e:  # noqa: BLE001 — se muestra en la interfaz
                self._error(f"No se pudieron leer los chats del teléfono: {e}")
        elif len(partes) == 4 and partes[:3] == ["api", "telefono", "trabajos"]:
            t = telefono.estado(partes[3])
            self._json(t) if t else self._error("No existe", 404)
        elif partes == ["api", "hallazgos"]:
            existentes = {c.name for c in carpetas_de_casos()}
            datos = PLATAFORMA.hallazgos_globales() if PLATAFORMA else {"casos": {}, "hallazgos": []}
            # sin los casos que se borraron después de procesarlos
            datos["hallazgos"] = [h for h in datos["hallazgos"] if h["caso"] in existentes]
            datos["casos"] = {k: v for k, v in datos["casos"].items() if k in existentes}
            self._json(datos)
        elif partes == ["api", "plataforma"]:
            self._json(estado_plataforma())
        elif len(partes) == 4 and partes[:2] == ["api", "casos"] and partes[3] == "trabajos":
            if not self._caso(partes[2]):
                return self._error("Caso no encontrado", 404)
            self._json(PLATAFORMA.trabajos_de(partes[2]) if PLATAFORMA else [])
        elif len(partes) == 3 and partes[:2] == ["api", "trabajos"]:
            if not PLATAFORMA:
                return self._error(estado_plataforma()["error"], 503)
            try:
                self._json(PLATAFORMA.detalle(partes[2]))
            except ErrorTrabajo as e:
                self._error(str(e), 404)
        elif len(partes) == 3 and partes[0] == "media":
            carpeta = self._caso(partes[1])
            archivo = carpeta / partes[2] if carpeta else None
            if not archivo or not dentro(carpeta, archivo) or not archivo.is_file():
                return self._error("Archivo no encontrado", 404)
            self._enviar_archivo(archivo)
        else:
            self._error("No existe", 404)

    def _enviar_archivo(self, archivo: Path):
        """Soporta Range para poder adelantar audios y videos."""
        total = archivo.stat().st_size
        inicio, fin = 0, total - 1
        rango = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if rango and (rango.group(1) or rango.group(2)):
            if rango.group(1):
                inicio = int(rango.group(1))
                fin = int(rango.group(2)) if rango.group(2) else fin
            else:
                inicio = max(0, total - int(rango.group(2)))
            fin = min(fin, total - 1)
            self.send_response(HTTPStatus.PARTIAL_CONTENT)
            self.send_header("Content-Range", f"bytes {inicio}-{fin}/{total}")
        else:
            self.send_response(200)
        tipo = mimetypes.guess_type(archivo.name)[0] or "application/octet-stream"
        self.send_header("Content-Type", tipo)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(fin - inicio + 1))
        self.end_headers()
        with open(archivo, "rb") as f:
            f.seek(inicio)
            restante = fin - inicio + 1
            while restante > 0 and (bloque := f.read(min(1024 * 1024, restante))):
                try:
                    self.wfile.write(bloque)
                except (BrokenPipeError, ConnectionResetError):
                    return
                restante -= len(bloque)

    # ---------------------------------------------------------------- POST
    def do_POST(self):
        partes, qs = self._ruta()
        try:
            if partes == ["api", "subir"]:
                return self._subir(qs)
            if partes == ["api", "importar"]:
                return self._importar(self._leer_json())
            if partes == ["api", "pegar"]:
                return self._pegar(self._leer_json())
            if partes == ["api", "telefono", "importar"]:
                return self._importar_telefono(self._leer_json())
            if partes == ["api", "carpetas"]:
                return self._json(ORG.crear(self._leer_json().get("nombre", "")))
            if len(partes) == 4 and partes[:2] == ["api", "carpetas"] and partes[3] == "renombrar":
                return self._json(ORG.renombrar(partes[2], self._leer_json().get("nombre", "")))
            if len(partes) == 4 and partes[:2] == ["api", "casos"] and partes[3] == "paralelo":
                if not PLATAFORMA:
                    return self._error(estado_plataforma()["error"], 503)
                if not self._caso(partes[2]):
                    return self._error("Caso no encontrado", 404)
                d = self._leer_json()
                return self._json(PLATAFORMA.crear(partes[2], int(d.get("bloques") or 2), bool(d.get("adjuntos"))))
            if partes in (["api", "trabajos", "cancelar_todos"], ["api", "trabajos", "reanudar_todos"]):
                if not PLATAFORMA:
                    return self._error(estado_plataforma()["error"], 503)
                return self._json(PLATAFORMA.cancelar_todos() if partes[2] == "cancelar_todos" else PLATAFORMA.reanudar_todos())
            if len(partes) == 4 and partes[:2] == ["api", "trabajos"] and partes[3] == "cancelar":
                if not PLATAFORMA:
                    return self._error(estado_plataforma()["error"], 503)
                return self._json(PLATAFORMA.cancelar(partes[2]))
            if len(partes) == 4 and partes[:2] == ["api", "trabajos"] and partes[3] == "reanudar":
                if not PLATAFORMA:
                    return self._error(estado_plataforma()["error"], 503)
                return self._json(PLATAFORMA.reanudar(partes[2]))
            if partes == ["api", "mover"]:
                d = self._leer_json()
                casos = [c for c in d.get("casos") or [] if self._caso(str(c))]
                if not casos:
                    return self._error("No se indicaron conversaciones válidas")
                ORG.mover(casos, d.get("carpeta") or None)
                return self._json({"ok": True, "movidas": len(casos)})
        except (ErrorImportacion, ErrorOrganizacion, ErrorTrabajo) as e:
            return self._error(str(e))
        except Exception as e:  # noqa: BLE001 — se muestra en la interfaz
            return self._error(f"Error interno: {type(e).__name__}: {e}", 500)
        self._error("No existe", 404)

    def _subir(self, qs):
        """Recibe un archivo crudo (body = bytes) y lo guarda en la carpeta del lote."""
        lote = (qs.get("lote") or [""])[0]
        nombre = Path((qs.get("archivo") or [""])[0]).name
        if not LOTE_VALIDO.match(lote) or not nombre or nombre.startswith("."):
            return self._error("Lote o nombre de archivo inválido")
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_SUBIDA:
            return self._error("Archivo demasiado grande", 413)
        carpeta = SUBIDAS / lote
        carpeta.mkdir(parents=True, exist_ok=True)
        with open(carpeta / nombre, "wb") as f:
            restante = n
            while restante > 0 and (bloque := self.rfile.read(min(1024 * 1024, restante))):
                f.write(bloque)
                restante -= len(bloque)
        self._json({"ok": True, "archivo": nombre, "bytes": n})

    def _ejecutar_importacion(self, entrada: Path, nombre_entrada: str, d: dict):
        caso = importar(entrada, DATASET, fuente=d.get("fuente") or "manual",
                        declaro=bool(d.get("declaro")), nombre=(d.get("nombre") or "").strip() or None,
                        prioridad=int(d.get("prioridad") or 5), sobrescribir=bool(d.get("sobrescribir")),
                        nombre_entrada=nombre_entrada)
        if d.get("carpeta"):            # sin carpeta elegida, un caso reemplazado conserva la suya
            ORG.mover([caso.name], d["carpeta"])
        self._json({"caso": caso.name, "resumen": resumen_caso(caso)})

    def _importar(self, d: dict):
        self._json(importar_lote(d))

    def _importar_telefono(self, d: dict):
        """Exporta un chat del teléfono por USB y lo importa (en segundo plano)."""
        if not d.get("declaro"):
            raise ErrorImportacion("Falta la declaración de consentimiento: el chat debe ser del equipo y con contenido ficticio")

        def importar_zip(ruta: Path) -> str:
            caso = importar(ruta, DATASET, fuente="telefono", declaro=True, nombre=(d.get("nombre") or "").strip() or None,
                            prioridad=int(d.get("prioridad") or 5), sobrescribir=bool(d.get("sobrescribir")),
                            nombre_entrada=ruta.name)
            if d.get("carpeta"):
                ORG.mover([caso.name], d["carpeta"])
            return caso.name
        try:
            id_ = telefono.iniciar(str(d.get("serial") or ""), str(d.get("chat") or ""),
                                   bool(d.get("con_archivos", True)), importar_zip)
        except ValueError as e:
            raise ErrorImportacion(str(e)) from e
        self._json({"id": id_})

    def _pegar(self, d: dict):
        texto = d.get("texto") or ""
        if not texto.strip():
            raise ErrorImportacion("El texto está vacío")
        titulo = re.sub(r"[^\w\- ]+", "", (d.get("nombre") or "").strip()) or "texto pegado"
        carpeta = SUBIDAS / uuid.uuid4().hex
        carpeta.mkdir(parents=True)
        try:
            txt = carpeta / f"Chat de WhatsApp con {titulo}.txt"
            txt.write_text(texto, encoding="utf-8")
            self._ejecutar_importacion(txt, txt.name, d)
        finally:
            shutil.rmtree(carpeta, ignore_errors=True)

    # ---------------------------------------------------------------- DELETE
    def do_DELETE(self):
        partes, _ = self._ruta()
        if len(partes) == 3 and partes[:2] == ["api", "casos"]:
            carpeta = self._caso(partes[2])
            if not carpeta:
                return self._error("Caso no encontrado", 404)
            if PLATAFORMA:                       # cancela su procesamiento y borra sus resultados
                PLATAFORMA.olvidar_caso(carpeta.name)
            shutil.rmtree(carpeta)
            Almacen(DATASET).recolectar()        # M3: borra del almacén lo que ya no usa ningún caso
            ORG.olvidar(carpeta.name)
            with _cache_lock:
                _cache.pop(carpeta.name, None)
            return self._json({"ok": True})
        if len(partes) == 3 and partes[:2] == ["api", "carpetas"]:
            try:
                return self._json({"ok": True, "liberadas": ORG.borrar(partes[2])})
            except ErrorOrganizacion as e:
                return self._error(str(e), 404)
        self._error("No existe", 404)


def importar_lote(d: dict) -> dict:
    """Importa los archivos subidos a SUBIDAS/<lote> (por /api/subir o por el receptor asíncrono de M3).
    Bloqueante: el receptor la corre en un hilo auxiliar, de a WA_IMPORTADORES a la vez."""
    lote = d.get("lote", "")
    if not LOTE_VALIDO.match(lote) or not (SUBIDAS / lote).is_dir():
        raise ErrorImportacion("No hay archivos subidos para importar")
    carpeta = SUBIDAS / lote
    try:
        archivos = [p for p in carpeta.iterdir() if p.is_file()]
        zips = [p for p in archivos if p.suffix.lower() == ".zip"]
        txts = [p for p in archivos if p.suffix.lower() == ".txt"]
        if len(zips) == 1 and len(archivos) == 1:
            entrada, nombre_entrada = zips[0], zips[0].name
        elif len(txts) == 1 and len(archivos) == 1:
            entrada, nombre_entrada = txts[0], txts[0].name
        elif zips and len(archivos) > 1:
            raise ErrorImportacion("Suba el .zip solo, o bien el .txt con sus archivos (no ambos)")
        else:
            entrada, nombre_entrada = carpeta, (txts[0].name if len(txts) == 1 else carpeta.name)
        caso = importar(entrada, DATASET, fuente=d.get("fuente") or "manual",
                        declaro=bool(d.get("declaro")), nombre=(d.get("nombre") or "").strip() or None,
                        prioridad=int(d.get("prioridad") or 5), sobrescribir=bool(d.get("sobrescribir")),
                        nombre_entrada=nombre_entrada)
        if d.get("carpeta"):            # sin carpeta elegida, un caso reemplazado conserva la suya
            ORG.mover([caso.name], d["carpeta"])
        return {"caso": caso.name, "resumen": resumen_caso(caso)}
    finally:
        shutil.rmtree(carpeta, ignore_errors=True)


class ManejadorWorkers(Manejador):
    """Puerto aparte para los workers de otras computadoras. Solo entrega los adjuntos
    registrados en el metadata.json de cada caso, y solo con el token compartido.
    La interfaz (con los chats completos) sigue escuchando únicamente en 127.0.0.1.

    M3 · Transferencia por bloques:
      GET /manifiesto/<caso>/<archivo>?bloque=<bytes>  → tamaño, SHA-256 total y SHA-256 de cada bloque
      GET /archivo/<caso>/<archivo>  con "Range: bytes=a-b"  → un bloque
    HTTP/1.1 con keep-alive: el worker reutiliza una conexión TCP para todos los bloques."""
    TOKEN: str = ""
    protocol_version = "HTTP/1.1"
    _manifiestos: dict = {}                     # (sha256, tamaño de bloque) → hashes de los bloques
    _candado_man = threading.Lock()

    def log_message(self, fmt, *args):
        if len(args) > 1 and str(args[1]) == "206":   # cada bloque de un archivo: no se imprime (serían cientos)
            return
        super().log_message(fmt, *args)

    def _manifiesto(self, archivo: Path, sha: str, tam: int) -> dict:
        clave = (sha, tam)
        with self._candado_man:
            hashes = self._manifiestos.get(clave)
        if hashes is None:
            hashes = []
            with open(archivo, "rb") as f:
                while bloque := f.read(tam):
                    hashes.append(hashlib.sha256(bloque).hexdigest())
            with self._candado_man:
                self._manifiestos[clave] = hashes
        return {"bytes": archivo.stat().st_size, "sha256": sha, "bloque": tam, "bloques": hashes}

    def do_GET(self):
        partes, qs = self._ruta()
        if not hmac.compare_digest(self.headers.get("X-Token", ""), self.TOKEN):
            return self._error("Token inválido", 401)
        if len(partes) != 3 or partes[0] not in ("archivo", "manifiesto"):
            return self._error("No existe", 404)
        carpeta = self._caso(partes[1])
        if not carpeta:
            return self._error("Archivo no encontrado", 404)
        meta = json.loads((carpeta / "metadata.json").read_text(encoding="utf-8"))
        archivo = carpeta / partes[2]
        if partes[2] in ("chat.txt", "metadata.json") or partes[2] not in meta.get("files", {}) \
                or not dentro(carpeta, archivo) or not archivo.is_file():
            return self._error("Archivo no encontrado", 404)
        if partes[0] == "manifiesto":
            try:
                tam = min(64 * 2**20, max(16 * 2**10, int((qs.get("bloque") or [2**20])[0])))
            except ValueError:
                return self._error("Tamaño de bloque inválido")
            return self._json(self._manifiesto(archivo, meta["files"][partes[2]].get("sha256") or "", tam))
        self._enviar_archivo(archivo)

    def do_POST(self):
        self._error("No existe", 404)

    do_DELETE = do_POST


def main():
    global DATASET, ORG, PLATAFORMA
    ap = argparse.ArgumentParser(description="Interfaz web local del importador de WhatsApp")
    ap.add_argument("--puerto", type=int, default=8100)
    ap.add_argument("--salida", type=Path, default=AQUI.parent / "dataset_whatsapp",
                    help="carpeta donde quedan los casos importados")
    ap.add_argument("--puerto-workers", type=int, default=8101,
                    help="puerto donde los workers descargan adjuntos (requiere WORKER_TOKEN)")
    ap.add_argument("--host-workers", default="0.0.0.0",
                    help="interfaz del puerto de workers (0.0.0.0 = accesible desde la red)")
    ap.add_argument("--puerto-receptor", type=int, default=8102,
                    help="receptor asíncrono de archivos con contrapresión (M3), solo en 127.0.0.1")
    a = ap.parse_args()
    DATASET = a.salida.resolve()
    DATASET.mkdir(parents=True, exist_ok=True)
    ORG = Organizacion(DATASET)
    servidor = ThreadingHTTPServer(("127.0.0.1", a.puerto), Manejador)
    print(f"Importador de WhatsApp en http://localhost:{a.puerto}")
    print(f"Casos en {DATASET}   (Ctrl+C para detener)")
    origenes = (f"http://localhost:{a.puerto}", f"http://127.0.0.1:{a.puerto}")
    receptor = Receptor(SUBIDAS, importar_lote, origenes=origenes)
    receptor.iniciar_en_hilo("127.0.0.1", a.puerto_receptor)
    print(f"[M3] receptor asíncrono en 127.0.0.1:{a.puerto_receptor} · contrapresión «{receptor.politica}», "
          f"cola de {receptor.cola_max}, {receptor.n_importadores} importadores")

    if FALTA_DISTRIBUIDO:
        print(f"[plataforma] desactivada: {FALTA_DISTRIBUIDO}")
    else:
        PLATAFORMA = Plataforma(DATASET, cargar_caso)
        PLATAFORMA.iniciar()
        token = config_distribuido("WORKER_TOKEN")
        if token:
            ManejadorWorkers.TOKEN = token
            srv_workers = ThreadingHTTPServer((a.host_workers, a.puerto_workers), ManejadorWorkers)
            threading.Thread(target=srv_workers.serve_forever, daemon=True).start()
            print(f"[plataforma] adjuntos para workers en {a.host_workers}:{a.puerto_workers} (con token)")
        else:
            print("[plataforma] sin WORKER_TOKEN: los workers no podrán verificar adjuntos")
    try:
        servidor.serve_forever()
    except KeyboardInterrupt:
        print("\nDetenido.")


if __name__ == "__main__":
    main()
