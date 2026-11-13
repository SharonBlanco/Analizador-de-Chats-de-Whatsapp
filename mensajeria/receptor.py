"""
M3 · Receptor de archivos con E/S asíncrona (asyncio) y contrapresión (backpressure).

Un solo hilo con un bucle de eventos atiende MUCHAS subidas a la vez: mientras espera que lleguen
los bytes de un cliente (await lector.read), atiende a otro. Los bytes se escriben al disco en
bloques de 1 MB desde un hilo auxiliar (asyncio.to_thread), así el bucle nunca se bloquea.

Contrapresión: las importaciones (descomprimir, verificar, calcular SHA-256, guardar en el almacén)
pasan por una COLA ACOTADA de WA_COLA_MAX lugares que vacían WA_IMPORTADORES tareas. Si la cola
está llena, según WA_CONTRAPRESION:
  rechazar  → 503 Service Unavailable + Retry-After: el cliente reintenta más tarde (por defecto)
  bloquear  → el pedido espera (conexión abierta) hasta que se libere un lugar
  ninguna   → cola sin límite (comportamiento anterior; solo para comparar)
Las subidas simultáneas también están acotadas (WA_SUBIDAS_MAX): las que sobran esperan sin que se lea
su cuerpo, y el control de flujo de TCP frena al cliente.

Rutas (solo 127.0.0.1; la interfaz las llama desde el navegador):
  POST /subir?lote=<id>&archivo=<nombre>   cuerpo = bytes del archivo
  POST /importar                           JSON {lote, fuente, declaro, …} → resultado de la importación
  GET  /estado                             profundidad de la cola, en curso, rechazos
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Callable
from urllib.parse import parse_qs, unquote, urlparse

BLOQUE = 1024 * 1024
LOTE_VALIDO = re.compile(r"^[a-f0-9]{8,32}$")
MAX_SUBIDA = 4 * 2 ** 30


def _config(clave: str, defecto: str) -> str:
    return os.environ.get(clave, defecto)


class Receptor:
    def __init__(self, carpeta_subidas: Path, importar: Callable[[dict], dict], *, politica: str | None = None,
                 cola_max: int | None = None, importadores: int | None = None, subidas_max: int | None = None,
                 origenes: tuple[str, ...] = ("http://localhost:8100", "http://127.0.0.1:8100"),
                 csv_metricas: Path | None = None, autorizacion=None):
        self.subidas = Path(carpeta_subidas)
        self.importar = importar                       # función bloqueante: corre en un hilo auxiliar
        self.politica = politica or _config("WA_CONTRAPRESION", "rechazar")
        self.cola_max = int(cola_max if cola_max is not None else _config("WA_COLA_MAX", "4"))
        self.n_importadores = int(importadores or _config("WA_IMPORTADORES", "2"))
        self.subidas_max = int(subidas_max or _config("WA_SUBIDAS_MAX", "8"))
        self.origenes = origenes
        self.autorizacion = autorizacion
        self.csv = csv_metricas
        self.metricas = {"aceptadas": 0, "rechazadas": 0, "subidas_frenadas": 0, "importadas": 0, "errores": 0,
                         "en_curso": 0, "subidas_en_curso": 0, "profundidad_max": 0}
        self.registros: list[dict] = []                # una fila por pedido de importación (para medir)
        self.t_importacion = 0.5                       # promedio móvil de lo que tarda una importación (s)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.cola: asyncio.Queue | None = None

    # ------------------------------------------------------------ arranque
    def iniciar_en_hilo(self, host: str = "127.0.0.1", puerto: int = 8102) -> None:
        listo = threading.Event()

        def correr():
            asyncio.run(self._principal(host, puerto, listo))
        threading.Thread(target=correr, daemon=True, name="receptor-asyncio").start()
        listo.wait(10)

    async def _principal(self, host, puerto, listo: threading.Event | None = None):
        self.loop = asyncio.get_running_loop()
        self.cola = asyncio.Queue(maxsize=0 if self.politica == "ninguna" else self.cola_max)
        self.sem_subidas = asyncio.Semaphore(self.subidas_max)
        for _ in range(self.n_importadores):
            asyncio.create_task(self._importador())
        if self.csv:
            asyncio.create_task(self._muestrear())
        servidor = await asyncio.start_server(self._atender, host, puerto, limit=2 ** 16)
        if listo:
            listo.set()
        async with servidor:
            await servidor.serve_forever()

    # ------------------------------------------------------------ consumidores de la cola
    async def _importador(self):
        while True:
            pedido, futuro = await self.cola.get()
            self.metricas["en_curso"] += 1
            pedido["_inicio"] = time.time()
            try:
                t0 = time.perf_counter()
                resultado = await asyncio.to_thread(self.importar, pedido["datos"])
                self.t_importacion = 0.8 * self.t_importacion + 0.2 * (time.perf_counter() - t0)
                if not futuro.done():
                    futuro.set_result((200, resultado))
                self.metricas["importadas"] += 1
            except Exception as e:  # noqa: BLE001 — se informa al cliente
                if not futuro.done():
                    futuro.set_result((400, {"error": str(e)}))
                self.metricas["errores"] += 1
            finally:
                self.metricas["en_curso"] -= 1
                self.cola.task_done()

    async def _muestrear(self):
        """Profundidad de la cola cada 0,2 s (para el gráfico de la ráfaga)."""
        self.csv.parent.mkdir(parents=True, exist_ok=True)
        with open(self.csv, "w", encoding="utf-8") as f:
            f.write("t,profundidad,en_curso,subidas_en_curso,aceptadas,rechazadas\n")
            while True:
                m = self.metricas
                f.write(f"{time.time():.3f},{self.cola.qsize()},{m['en_curso']},{m['subidas_en_curso']},"
                        f"{m['aceptadas']},{m['rechazadas']}\n")
                f.flush()
                await asyncio.sleep(0.2)

    def estado(self) -> dict:
        return {**self.metricas, "profundidad": self.cola.qsize() if self.cola else 0, "politica": self.politica,
                "cola_max": None if self.politica == "ninguna" else self.cola_max, "importadores": self.n_importadores}

    # ------------------------------------------------------------ HTTP mínimo sobre asyncio
    async def _atender(self, lector: asyncio.StreamReader, escritor: asyncio.StreamWriter):
        try:
            cabecera = await lector.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            escritor.close()
            return
        lineas = cabecera.decode("latin-1").split("\r\n")
        try:
            metodo, ruta, _ = lineas[0].split(" ", 2)
        except ValueError:
            escritor.close()
            return
        cab = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in lineas[1:] if l)}
        origen = cab.get("origin", "")
        u = urlparse(ruta)
        try:
            if self.autorizacion is not None:
                # CORS y autorizacion tambien en el puerto M3 (8102).
                if origen and origen not in self.origenes:
                    return await self._responder(escritor, 403, {'error':'Origen prohibido'}, origen)
                estado_auth = self.autorizacion(cab, metodo, u.path)
                if estado_auth not in (200,204):
                    return await self._responder(escritor, estado_auth,
                                                  {'error':'No autenticado' if estado_auth==401 else '403 prohibido'}, origen)
            if metodo == "OPTIONS":
                await self._responder(escritor, 204, None, origen)
            elif metodo == "GET" and u.path == "/estado":
                await self._responder(escritor, 200, self.estado(), origen)
            elif metodo == "POST" and u.path == "/subir":
                await self._subir(lector, escritor, cab, parse_qs(u.query), origen)
            elif metodo == "POST" and u.path == "/importar":
                cuerpo = await lector.readexactly(int(cab.get("content-length") or 0))
                await self._encolar(escritor, json.loads(cuerpo or b"{}"), origen)
            else:
                await self._responder(escritor, 404, {"error": "No existe"}, origen)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception as e:  # noqa: BLE001
            try:
                await self._responder(escritor, 500, {"error": f"{type(e).__name__}: {e}"}, origen)
            except ConnectionError:
                pass
        finally:
            escritor.close()

    async def _responder(self, escritor, estado: int, datos, origen: str, extra: dict | None = None):
        cuerpo = b"" if datos is None else json.dumps(datos, ensure_ascii=False).encode()
        razones = {200: "OK", 204: "No Content", 400: "Bad Request", 404: "Not Found", 413: "Payload Too Large",
                   401: "Unauthorized", 403:"Forbidden", 500: "Internal Server Error", 503: "Service Unavailable"}
        cab = [f"HTTP/1.1 {estado} {razones.get(estado, '')}", "Connection: close", f"Content-Length: {len(cuerpo)}",
               "Content-Type: application/json; charset=utf-8"]
        if origen in self.origenes:              # la interfaz (puerto 8100) llama a este puerto (8102)
            cab += [f"Access-Control-Allow-Origin: {origen}", "Access-Control-Allow-Methods: GET, POST, OPTIONS",
                    "Access-Control-Allow-Headers: Content-Type, X-CSRF-Token", "Access-Control-Allow-Credentials: true",
                    "Access-Control-Expose-Headers: Retry-After"]
        cab += [f"{k}: {v}" for k, v in (extra or {}).items()]
        escritor.write(("\r\n".join(cab) + "\r\n\r\n").encode() + cuerpo)
        await escritor.drain()                   # contrapresión de TCP: espera si el cliente lee lento

    async def _subir(self, lector, escritor, cab, qs, origen):
        lote = (qs.get("lote") or [""])[0]
        nombre = Path(unquote((qs.get("archivo") or [""])[0])).name
        n = int(cab.get("content-length") or 0)
        if not LOTE_VALIDO.match(lote) or not nombre or nombre.startswith("."):
            return await self._responder(escritor, 400, {"error": "Lote o nombre de archivo inválido"}, origen)
        if n > MAX_SUBIDA:
            return await self._responder(escritor, 413, {"error": "Archivo demasiado grande"}, origen)
        # Más de WA_SUBIDAS_MAX subidas a la vez: esta espera SIN LEER el cuerpo. El búfer de TCP se llena
        # y el cliente deja de enviar solo (control de flujo de TCP = contrapresión hasta el navegador).
        if self.sem_subidas.locked():
            self.metricas["subidas_frenadas"] += 1
        async with self.sem_subidas:
            self.metricas["subidas_en_curso"] += 1
            try:
                carpeta = self.subidas / lote
                carpeta.mkdir(parents=True, exist_ok=True)
                f = await asyncio.to_thread(open, carpeta / nombre, "wb")
                try:
                    restante = n
                    while restante > 0:
                        bloque = await lector.read(min(BLOQUE, restante))   # no bloquea: cede el turno a otros clientes
                        if not bloque:
                            raise ConnectionError("el cliente cortó la subida")
                        await asyncio.to_thread(f.write, bloque)            # el disco, en un hilo auxiliar
                        restante -= len(bloque)
                finally:
                    await asyncio.to_thread(f.close)
            finally:
                self.metricas["subidas_en_curso"] -= 1
        await self._responder(escritor, 200, {"ok": True, "archivo": nombre, "bytes": n}, origen)

    async def _encolar(self, escritor, datos: dict, origen: str):
        registro = {"llegada": time.time(), "estado": None}
        self.registros.append(registro)
        futuro = self.loop.create_future()
        pedido = {"datos": datos}
        if self.politica == "rechazar":
            try:
                self.cola.put_nowait((pedido, futuro))
            except asyncio.QueueFull:
                self.metricas["rechazadas"] += 1
                registro.update(estado="rechazada", fin=time.time())
                # Retry-After: lo que tardarán en atenderse los que ya están delante, según el tiempo medido
                delante = self.cola.qsize() + self.metricas["en_curso"]
                espera = min(30, max(1, math.ceil(self.t_importacion * delante / max(1, self.n_importadores))))
                return await self._responder(escritor, 503, {"error": "Cola de importación llena; reintente",
                                                             "profundidad": self.cola.qsize()},
                                             origen, {"Retry-After": str(espera)})
        else:
            await self.cola.put((pedido, futuro))    # "bloquear": espera aquí si está llena; "ninguna": nunca espera
        self.metricas["aceptadas"] += 1
        self.metricas["profundidad_max"] = max(self.metricas["profundidad_max"], self.cola.qsize())
        estado, resultado = await futuro
        registro.update(estado="ok" if estado == 200 else "error", fin=time.time(),
                        espera_cola=round(pedido["_inicio"] - registro["llegada"], 3))
        await self._responder(escritor, estado, resultado, origen)
