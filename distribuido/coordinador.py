"""
Coordinador del procesamiento en paralelo. Corre dentro de mensajeria/web.py.

Un "trabajo" divide una conversación en sub-tareas y las reparte entre los workers:
  texto     → un bloque de mensajes; el worker devuelve estadisticas.parcial() del bloque
  adjunto   → un archivo; el worker recalcula su SHA-256 (cadena de custodia) y sus metadatos
Cuando todas terminan (barrier/join) se combinan los parciales con estadisticas.combinar()
y el resultado se compara con el análisis local: si son idénticos, la división es correcta.

Estados del trabajo:   queued → processing ⇄ retrying → completed | partially_completed | failed
                       (cancelled en cualquier momento)
Estados de sub-tarea:  pendiente → procesando → completed | failed | cancelled  (retrying entre medio)

El estado se guarda en <dataset>/.trabajos/<id>.json en cada cambio: si el coordinador
se reinicia, recupera los trabajos en curso. Los resultados que lleguen mientras tanto
esperan en la cola durable de RabbitMQ, y los duplicados (re-entregas) se ignoran.
"""

from __future__ import annotations

import csv
import json
import math
import os
import re
import threading
import unicodedata
from collections import Counter, deque
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

import pika

from comun import (COLA_RESULTADOS, ESPERA_REINTENTO, EXCHANGE_CONTROL, EXCHANGE_LATIDOS, MAX_REINTENTOS,
                   PERSISTENTE, POOLS, SIN_SENAL, TIPOS_POOL, ConfiguracionIncompleta, config, declarar,
                   destino_rabbit, parametros_rabbit)
from estadisticas import calcular, combinar, previos
from planificador import Planificador, Tarea, convertir_prioridad


FINALES_ST = {"completed", "failed", "cancelled"}
FINALES_TR = {"completed", "partially_completed", "failed", "cancelled"}
MAX_BLOQUES = 64
SECCIONES = {   # qué partes del resultado se comparan con el análisis local
    "Mensajes, participantes y respuestas": ["resumen", "participantes"],
    "Actividad por día": ["por_dia"],
    "Mapa día × hora": ["calor", "por_hora", "por_dia_semana"],
    "Tipos de mensaje": ["por_tipo"],
    "Palabras y enlaces": ["palabras", "dominios"],
}


ORDEN_SEVERIDAD = {"alta": 2, "media": 1}
# Ground truth del dataset generado (generador_dataset/generar.py): si el caso viene de ahí,
# los hallazgos se comparan con lo que el generador insertó.
VERDAD = Path(config("WA_VERDAD", str(Path(__file__).resolve().parent.parent / "dataset_generado" / "verdad")))


class ErrorTrabajo(Exception):
    pass


def _normalizar(t: str) -> list[str]:
    t = "".join(c for c in unicodedata.normalize("NFD", t.lower()) if unicodedata.category(c) != "Mn")
    return re.sub(r"[^\w\s]", " ", t).split()


def wer(referencia: str, hipotesis: str) -> float | None:
    """Tasa de error por palabra (distancia de edición entre palabras / palabras de la referencia)."""
    r, h = _normalizar(referencia), _normalizar(hipotesis)
    if not r:
        return None
    previa = list(range(len(h) + 1))
    for i, pr in enumerate(r, 1):
        actual = [i] + [0] * len(h)
        for j, ph in enumerate(h, 1):
            actual[j] = min(previa[j] + 1, actual[j - 1] + 1, previa[j - 1] + (pr != ph))
        previa = actual
    return round(previa[-1] / len(r), 3)


def metricas(predichos: set, reales: set, universo: int) -> dict:
    vp, fp, fn = len(predichos & reales), len(predichos - reales), len(reales - predichos)
    precision = vp / (vp + fp) if vp + fp else None
    exhaustividad = vp / (vp + fn) if vp + fn else None
    f1 = 2 * precision * exhaustividad / (precision + exhaustividad) if precision and exhaustividad else None
    r = lambda x: round(x, 3) if x is not None else None  # noqa: E731
    return {"vp": vp, "fp": fp, "fn": fn, "vn": universo - vp - fp - fn, "total": universo, "reales": len(reales),
            "precision": r(precision), "exhaustividad": r(exhaustividad), "f1": r(f1)}


def _json(x):
    """Copia normalizada como JSON (tuplas → listas), para comparar y para responder."""
    return json.loads(json.dumps(x, ensure_ascii=False))


def _iso(t: float | None) -> str | None:
    return datetime.fromtimestamp(t).isoformat(timespec="seconds") if t else None


class Plataforma:
    def __init__(self, dataset: Path, cargar: Callable[[str], tuple[list, dict]]):
        """cargar(caso) → (mensajes parseados, metadata.json)"""
        self.cargar = cargar
        self.dir = dataset / ".trabajos"
        self.lock = threading.RLock()
        self.trabajos: dict[str, dict] = {}
        # M2: planificador de subtareas
        self.planificador = Planificador(
            politica=config("WA_POLITICA", "aging"),
            intervalo_aging=float(config("WA_INTERVALO_AGING", "10"))
        )
        self.workers: dict[str, dict] = {}
        # M2: subtareas publicadas que aún no han finalizado.
        self.despachadas: set[str] = set()
        # M2: reservas de capacidad asociadas a subtareas.
        self.reservas: dict[str, str] = {}
        self.alertas: deque = deque(maxlen=200)       # notificaciones asíncronas (B.4)
        self._n_alerta = 0
        self.conectado = False
        self.configurado = True
        self.error: str | None = None
        self.destino = destino_rabbit()
        # M1: serie de memoria de cada worker (un renglón por latido) para graficar ráfagas
        self.csv_memoria = dataset / ".metricas" / "memoria_workers.csv"
        self._candado_csv = threading.Lock()

    # ============================================================ arranque
    def iniciar(self) -> None:
        try:
            parametros_rabbit()
        except ConfiguracionIncompleta as e:
            self.configurado, self.error = False, str(e)
            print(f"[plataforma] desactivada: {e}")
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        for f in sorted(self.dir.glob("*.json")):
            try:
                tr = json.loads(f.read_text(encoding="utf-8"))
                self.trabajos[tr["id"]] = tr
            except (OSError, json.JSONDecodeError, KeyError):
                print(f"[plataforma] no se pudo leer {f.name}")
        threading.Thread(target=self._escuchar, daemon=True, name="resultados").start()

    def _guardar(self, tr: dict) -> None:
        tmp = self.dir / f"{tr['id']}.tmp"
        tmp.write_text(json.dumps(tr, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.dir / f"{tr['id']}.json")

    # ============================================================ RabbitMQ
    def _publicar(self, mensajes: list[tuple[str, dict, int]], exchange: str = "") -> None:
        """Publica [(routing_key, cuerpo, prioridad)] con una conexión propia (pika no es
        thread-safe, y esto se llama desde los hilos HTTP y desde los temporizadores)."""
        conexion = pika.BlockingConnection(parametros_rabbit())
        try:
            canal = conexion.channel()
            declarar(canal)
            canal.confirm_delivery()

            for clave, cuerpo, prioridad in mensajes:
                canal.basic_publish(
                    exchange=exchange,
                    routing_key=clave,
                    body=json.dumps(cuerpo, ensure_ascii=False).encode(),
                    properties=pika.BasicProperties(
                        delivery_mode=PERSISTENTE,
                        priority=max(1, min(10, prioridad))
                    ),
                    mandatory=True
                )
        finally:
            conexion.close()

    def _capacidad_disponible(self) -> dict[str, int]:
        """Calcula capacidad libre por tipo de tarea.

        Debe llamarse con self.lock adquirido.
        """
        ahora = time.time()
        capacidad = {pool: 0 for pool in POOLS}
        for worker in self.workers.values():
            if ahora - worker.get("visto", 0) >= SIN_SENAL:
                continue
            concurrencia = max(0, int(worker.get("concurrencia", 1)))
            ocupados = max(0, int(worker.get("en_curso", 0)))
            libres = max(0, concurrencia - ocupados)

            for pool in worker.get("pools", []):
                if pool in capacidad:
                    capacidad[pool] += libres
        return capacidad
    def _workers_disponibles(self) -> list[dict]:
        """Devuelve la capacidad estimada de cada worker activo.

        Debe llamarse con self.lock adquirido.
        """
        ahora = time.time()
        disponibles = []
        for worker_id, worker in self.workers.items():
            if ahora - worker.get("visto", 0) >= SIN_SENAL:
                continue
            concurrencia = max(0, int(worker.get("concurrencia", 1)))
            ocupados = max(0, int(worker.get("en_curso", 0)))
            libres = max(0, concurrencia - ocupados)

            if libres > 0:
                disponibles.append({
                    "worker": worker_id,
                    "pools": list(worker.get("pools", [])),
                    "libres": libres,
                })
        return disponibles

    def _workers_con_reservas(self) -> list[dict]:
        """Calcula capacidad conservadora considerando latidos y reservas.

        Debe llamarse con self.lock adquirido.
        """
        ahora = time.time()
        disponibles = []

        for worker_id, worker in self.workers.items():
            if ahora - worker.get("visto", 0) >= SIN_SENAL:
                continue

            concurrencia = max(
                0, int(worker.get("concurrencia", 1))
            )
            en_curso = max(
                0, int(worker.get("en_curso", 0))
            )

            reservadas = sum(
                1
                for reservado_a in self.reservas.values()
                if reservado_a == worker_id
            )

            ocupadas = max(en_curso, reservadas)
            libres = max(0, concurrencia - ocupadas)

            if libres > 0:
                disponibles.append({
                    "worker": worker_id,
                    "pools": list(worker.get("pools", [])),
                    "libres": libres,
                })

        return disponibles

    def _registrar_pendientes(
        self,
        tr: dict,
        mensajes: list,
        anteriores: dict
    ) -> int:
        """Registra en M2 las subtareas que todavía no se publicaron.

        Debe llamarse con self.lock adquirido.
        """
        existentes = {
            tarea.id
            for cola in self.planificador.colas.values()
            for tarea in cola
        }

        agregadas = 0

        for st in tr["subtareas"].values():
            if st["estado"] != "pendiente":
                continue

            if st.get("despachada", False):
                continue

            if st["id"] in existentes:
                continue

            datos = self._mensaje(tr, st, mensajes, anteriores)

            tarea = Tarea(
                id=st["id"],
                prioridad=convertir_prioridad(tr["prioridad"]),
                datos=datos
            )

            self.planificador.agregar(tarea)
            existentes.add(st["id"])
            agregadas += 1

        return agregadas

    def _despachar(self) -> bool:
        """Selecciona y publica una subtarea pendiente de M2.

        Debe llamarse con self.lock adquirido.
        """
        tarea = self.planificador.siguiente()
        if tarea is None:
            return False
        try:
            self._publicar([tarea.datos])
        except Exception:
            self.planificador.devolver(tarea)
            raise
        return True

    def _escuchar(self) -> None:
        """Hilo que consume la cola de resultados y los latidos. Se reconecta solo."""
        primera = True
        while True:
            try:
                conexion = pika.BlockingConnection(parametros_rabbit())
                canal = conexion.channel()
                declarar(canal)
                cola_latidos = canal.queue_declare(queue="", exclusive=True).method.queue
                canal.queue_bind(queue=cola_latidos, exchange=EXCHANGE_LATIDOS)
                canal.basic_consume(queue=COLA_RESULTADOS, on_message_callback=self._al_resultado)
                canal.basic_consume(queue=cola_latidos, on_message_callback=self._al_latido, auto_ack=True)
                self.conectado, self.error = True, None
                print(f"[plataforma] conectado a RabbitMQ en {self.destino}")
                if primera:
                    primera = False
                    self._reanudar()
                canal.start_consuming()
            except ConfiguracionIncompleta as e:
                self.conectado, self.configurado, self.error = False, False, str(e)
                return
            except Exception as e:  # noqa: BLE001 — cualquier caída: reintentar
                if self.conectado or not self.error:
                    print(f"[plataforma] sin conexión con RabbitMQ ({self.destino}): {e}")
                self.conectado = False
                self.error = f"No hay conexión con RabbitMQ en {self.destino} ({type(e).__name__})"
                time.sleep(5)

    def _al_latido(self, _canal, _metodo, _props, cuerpo) -> None:
        try:
            latido = json.loads(cuerpo)
            with self.lock:
                self.workers[latido["worker"]] = {**latido, "visto": time.time()}
            self._anotar_memoria(latido)
        except (ValueError, KeyError):
            pass

    def _anotar_memoria(self, latido: dict) -> None:
        m = latido.get("memoria")
        if not m:
            return
        fila = [round(time.time(), 2), latido["worker"], m.get("uso_mb"), m.get("limite_mb"), m.get("rss_mb"), m.get("pss_mb"),
                m.get("reservado_mb"), latido.get("en_curso"), m.get("oom_kills"), m.get("reinicios")]
        try:
            with self._candado_csv:
                nuevo = not self.csv_memoria.exists()
                self.csv_memoria.parent.mkdir(parents=True, exist_ok=True)
                with open(self.csv_memoria, "a", newline="", encoding="utf-8") as f:
                    w = csv.writer(f)
                    if nuevo:
                        w.writerow(["t", "worker", "cgroup_mb", "limite_mb", "rss_mb", "pss_mb", "reservado_mb",
                                    "en_curso", "oom_kills", "reinicios"])
                    w.writerow(fila)
        except OSError:
            pass

    def _al_resultado(self, canal, metodo, _props, cuerpo) -> None:
        try:
            msg = json.loads(cuerpo)
            with self.lock:
                recancelar = self._procesar(msg)
            if recancelar:
                # Un worker que se conectó después de la cancelación no recibió el aviso y
                # empezó una sub-tarea de ese trabajo: se lo repetimos para que la aborte.
                canal.basic_publish(exchange=EXCHANGE_CONTROL, routing_key="",
                                    body=json.dumps({"cancelar": recancelar}).encode())
        except Exception as e:  # noqa: BLE001 — un mensaje dañado no debe tumbar el hilo
            print(f"[plataforma] error con un resultado: {type(e).__name__}: {e}")
        canal.basic_ack(delivery_tag=metodo.delivery_tag)

    # ============================================================ crear / cancelar
    def crear(self, caso: str, bloques: int, con_adjuntos: bool) -> dict:
        if not self.configurado:
            raise ErrorTrabajo(self.error or "La plataforma distribuida no está configurada")
        if not self.conectado:
            raise ErrorTrabajo(self.error or "No hay conexión con RabbitMQ")
        mensajes, meta = self.cargar(caso)
        if not mensajes:
            raise ErrorTrabajo("La conversación no tiene mensajes")
        n = len(mensajes)
        bloques = max(1, min(int(bloques), MAX_BLOQUES, n))
        tam = math.ceil(n / bloques)
        rangos = [(i, min(i + tam, n)) for i in range(0, n, tam)]
        subtareas: dict[str, dict] = {}
        def nueva(**datos):
            st = {"id": f"st-{uuid.uuid4().hex[:8]}", "estado": "pendiente", "worker": None, "intentos": 0,
                  "despachada": False,
                  "reasignaciones": 0, "inicio": None, "fin": None, "duracion": None, "error": None,
                  "resultado": None, **datos}
            subtareas[st["id"]] = st

        for i, (ini, fin) in enumerate(rangos):
            nueva(tipo="texto", indice=i, ini=ini, fin=fin, mensajes=fin - ini,
                  desde=mensajes[ini].id, hasta=mensajes[fin - 1].id)
        if con_adjuntos:
            for i, (nombre, f) in enumerate(x for x in meta.get("files", {}).items() if x[0] != "chat.txt"):
                nueva(tipo="adjunto", indice=i, archivo=nombre, sha256=f.get("sha256"),
                      bytes=f.get("bytes"), tipo_archivo=f.get("tipo"),
                      mensaje={k: f.get(k) for k in ("mensaje_id", "linea", "autor", "fecha_hora")})

        ahora = time.time()
        tr = {"id": f"tr-{uuid.uuid4().hex[:8]}", "caso": caso, "chat": meta.get("chat"),
              "prioridad": int(meta.get("prioridad") or 5), "estado": "queued",
              "creado": ahora, "iniciado": None, "terminado": None,
              "bloques": len(rangos), "adjuntos": len(subtareas) - len(rangos),
              "subtareas": subtareas, "resultado": None}
        anteriores = previos(mensajes)
        publicar = [self._mensaje(tr, st, mensajes, anteriores) for st in subtareas.values()]
        # Registrar ANTES de publicar: un worker rápido puede responder antes de que
        # _publicar() regrese, y su resultado se descartaría por "trabajo desconocido".
        with self.lock:
            self.trabajos[tr["id"]] = tr
            self._guardar(tr)
        try:
            self._publicar(publicar)
        except Exception as e:  # noqa: BLE001
            with self.lock:
                self.trabajos.pop(tr["id"], None)
                (self.dir / f"{tr['id']}.json").unlink(missing_ok=True)
            raise ErrorTrabajo(f"No se pudieron encolar las sub-tareas: {type(e).__name__}: {e}") from e
        extra = f" + {tr['adjuntos']} adjuntos" if tr["adjuntos"] else ""
        print(f"[plataforma] {tr['id']}: {caso} dividido en {len(rangos)} bloques{extra}")
        return self.detalle(tr["id"])

    def _mensaje(self, tr: dict, st: dict, mensajes: list, anteriores: dict) -> tuple[str, dict, int]:
        base = {"trabajo": tr["id"], "subtarea": st["id"], "tipo": st["tipo"]}
        if st["tipo"] == "texto":
            bloque = []
            for m in mensajes[st["ini"]:st["fin"]]:
                d = m.a_dict()
                d.pop("linea_cruda", None)
                d["previo"] = anteriores.get(m.id)        # para medir respuestas entre bloques
                bloque.append(d)
            return POOLS["texto"], {**base, "mensajes": bloque}, tr["prioridad"]
        pool = TIPOS_POOL.get(st.get("tipo_archivo"), "adjuntos")
        return POOLS[pool], {**base, "caso": tr["caso"], "archivo": st["archivo"], "tipo_archivo": st.get("tipo_archivo"),
                             "sha256": st["sha256"], "bytes": st["bytes"]}, tr["prioridad"]

    def cancelar(self, id_: str) -> dict:
        with self.lock:
            tr = self.trabajos.get(id_)
            if not tr:
                raise ErrorTrabajo("Trabajo no encontrado")
            if tr["estado"] in FINALES_TR:
                raise ErrorTrabajo("El trabajo ya terminó")
            tr["estado"], tr["terminado"] = "cancelled", time.time()
            for st in tr["subtareas"].values():
                if st["estado"] not in FINALES_ST:
                    st["estado"], st["fin"] = "cancelled", time.time()
            self._guardar(tr)
        try:   # los workers descartan lo que tomen de este trabajo y abortan lo que estén haciendo
            self._publicar([("", {"cancelar": id_}, 10)], exchange=EXCHANGE_CONTROL)
        except Exception as e:  # noqa: BLE001
            print(f"[plataforma] no se pudo avisar la cancelación a los workers: {e}")
        return self.detalle(id_)

    def olvidar_caso(self, caso: str) -> int:
        """Al eliminar una conversación: cancela lo que tenga en curso y borra sus procesamientos
        (así sus hallazgos dejan de aparecer en Riesgos). Devuelve cuántos trabajos se borraron."""
        with self.lock:
            ids = [i for i, t in self.trabajos.items() if t["caso"] == caso]
            en_curso = [i for i in ids if self.trabajos[i]["estado"] not in FINALES_TR]
        for i in en_curso:
            try:
                self.cancelar(i)
            except ErrorTrabajo:
                pass
        with self.lock:
            for i in ids:
                self.trabajos.pop(i, None)
                (self.dir / f"{i}.json").unlink(missing_ok=True)
        return len(ids)

    # ============================================================ flujo de tareas
    def _procesar(self, msg: dict) -> str | None:
        """Aplica un mensaje de un worker. Devuelve el id del trabajo si hay que repetir
        su cancelación a los workers."""
        tr = self.trabajos.get(msg.get("trabajo"))
        st = tr["subtareas"].get(msg.get("subtarea")) if tr else None
        if st and st["estado"] == "cancelled" and msg.get("evento") == "inicio":
            return tr["id"]
        evento, worker = msg.get("evento"), msg.get("worker")
        if evento == "oom":
            return self._oom(tr, st, msg)
        if not st or st["estado"] in FINALES_ST:
            return None                              # trabajo borrado, o duplicado de una re-entrega

        if evento == "inicio":
            # Si la tenía otro worker, este cayó y RabbitMQ la re-entregó
            if st["estado"] == "procesando" and st["worker"] and st["worker"] != worker:
                st["reasignaciones"] += 1
                print(f"[plataforma] {st['id']} reasignada: {st['worker']} → {worker}")
            st.update(estado="procesando", worker=worker, inicio=time.time())
            if not tr["iniciado"]:
                tr["iniciado"] = time.time()
        elif evento == "cancelada":
            st.update(estado="cancelled", worker=worker, fin=time.time())
        elif evento == "fin" and msg.get("estado") == "completed":
            st.update(estado="completed", worker=worker, fin=time.time(), duracion=msg.get("duracion"),
                      resultado=msg.get("resultado"), error=None)
            altos = [h for h in self._hallazgos_de(st) if h["severidad"] == "alta"]
            if altos and tr["estado"] != "cancelled":
                h = altos[0]
                donde = f"mensaje #{h['id']}" if h["tipo"] == "texto" else f"{h['tipo']} {h['archivo']}"
                extra = f" y {len(altos) - 1} más" if len(altos) > 1 else ""
                self._alerta("alta", tr, f"{', '.join(sorted({d['categoria'] for d in h['detecciones']}))} en {donde}{extra}")
        elif evento == "fin":
            st["error"] = msg.get("error") or "Error desconocido"
            if msg.get("reintentable") and st["intentos"] < MAX_REINTENTOS and tr["estado"] != "cancelled":
                st["intentos"] += 1
                st.update(estado="retrying", worker=None)
                espera = ESPERA_REINTENTO * st["intentos"]
                print(f"[plataforma] reintento {st['intentos']}/{MAX_REINTENTOS} de {st['id']} en {espera:g} s: {st['error']}")
                threading.Timer(espera, self._reencolar, args=(tr["id"], st["id"])).start()
            else:
                st.update(estado="failed", worker=worker, fin=time.time(), duracion=msg.get("duracion"))
        self._barrera(tr)
        self._guardar(tr)
        return None

    def _oom(self, tr: dict | None, st: dict | None, msg: dict) -> None:
        """M1: el supervisor de un worker avisa que el kernel lo mató por memoria. La sub-tarea no se
        pierde (no tenía ACK: RabbitMQ la re-entrega); aquí solo se registra y se avisa."""
        worker = msg.get("worker")
        with self.lock:
            if worker in self.workers:
                self.workers[worker]["oom"] = self.workers[worker].get("oom", 0) + 1
        if not tr or not st:
            return None
        st.setdefault("oom", []).append({"worker": worker, "veces": msg.get("veces"), "limite_mb": msg.get("limite_mb"),
                                         "pico_mb": msg.get("pico_mb"), "hora": _iso(msg.get("hora") or time.time())})
        if st["estado"] == "procesando" and st.get("worker") == worker:
            st.update(estado="pendiente", worker=None)          # esperando la re-entrega de RabbitMQ
        politica = ("se reintenta sola en ese nodo" if (msg.get("veces") or 1) < 2
                    else "ese nodo la rechazará: pasa a otro nodo o falla tras los reintentos")
        self._alerta("media", tr, f"OOM: {worker} se quedó sin memoria ({msg.get('limite_mb')} MB) con {st['id']}; {politica}")
        self._guardar(tr)
        return None

    def _reencolar(self, id_tr: str, id_st: str) -> None:
        with self.lock:
            tr = self.trabajos.get(id_tr)
            st = tr["subtareas"].get(id_st) if tr else None
            if not st or st["estado"] != "retrying" or tr["estado"] == "cancelled":
                return
        try:
            mensajes, _ = self.cargar(tr["caso"])
            self._publicar([self._mensaje(tr, st, mensajes, previos(mensajes))])
            with self.lock:
                if st["estado"] == "retrying":
                    st["estado"] = "pendiente"
                    self._barrera(tr)
                    self._guardar(tr)
        except Exception as e:  # noqa: BLE001
            with self.lock:
                st.update(estado="failed", error=f"No se pudo re-encolar: {e}", fin=time.time())
                self._barrera(tr)
                self._guardar(tr)

    def _reanudar(self) -> None:
        """Tras arrancar: los reintentos que esperaban en un temporizador se perdieron con
        el proceso anterior; se vuelven a encolar. Lo pendiente sigue en RabbitMQ."""
        with self.lock:
            pendientes = [(tr["id"], st["id"]) for tr in self.trabajos.values() if tr["estado"] not in FINALES_TR
                          for st in tr["subtareas"].values() if st["estado"] == "retrying"]
        for id_tr, id_st in pendientes:
            self._reencolar(id_tr, id_st)

    def _barrera(self, tr: dict) -> None:
        """Barrier/join: solo cuando TODAS las sub-tareas terminaron se cierra el trabajo."""
        if tr["estado"] in FINALES_TR:
            return
        sts = list(tr["subtareas"].values())
        if not all(s["estado"] in FINALES_ST for s in sts):
            tr["estado"] = ("retrying" if any(s["estado"] == "retrying" for s in sts) else
                            "processing" if tr["iniciado"] else "queued")
            return
        ok = sum(s["estado"] == "completed" for s in sts)
        tr["estado"] = "completed" if ok == len(sts) else "failed" if ok == 0 else "partially_completed"
        tr["terminado"] = time.time()
        self._consolidar(tr)
        conteo = (tr["resultado"] or {}).get("resumen_hallazgos", {}).get("por_severidad", {})
        self._alerta("info", tr, f"procesamiento {tr['estado']}: {sum(conteo.values())} hallazgos "
                                 f"({conteo.get('alta', 0)} de severidad alta)")
        print(f"[plataforma] {tr['id']} terminado → {tr['estado']}")

    def _consolidar(self, tr: dict) -> None:
        textos = sorted((s for s in tr["subtareas"].values() if s["tipo"] == "texto"), key=lambda s: s["indice"])
        hechos = [s for s in textos if s["estado"] == "completed"]
        completo = len(hechos) == len(textos)
        resultado: dict = {"texto_completo": completo, "estadisticas": None, "verificacion": None}
        if hechos:
            # los trabajos anteriores guardaban el parcial directo; los nuevos, en "parcial"
            combinado = _json(combinar([s["resultado"].get("parcial", s["resultado"]) for s in hechos]))
            resultado["estadisticas"] = combinado
            if completo:
                t0 = time.perf_counter()
                mensajes, _ = self.cargar(tr["caso"])
                local = _json(calcular(mensajes))
                resultado["verificacion"] = {
                    "identico": combinado == local,
                    "secciones": [{"seccion": nombre, "coincide": all(combinado[k] == local[k] for k in claves)}
                                  for nombre, claves in SECCIONES.items()],
                    "segundos_local": round(time.perf_counter() - t0, 3),
                }
        adj = [s for s in tr["subtareas"].values() if s["tipo"] == "adjunto" and s["estado"] == "completed"]
        resultado["adjuntos_integros"] = sum(1 for s in adj if (s["resultado"] or {}).get("coincide"))
        resultado["adjuntos_alterados"] = sum(1 for s in adj if not (s["resultado"] or {}).get("coincide"))

        hallazgos = sorted((h for s in tr["subtareas"].values() if s["estado"] == "completed"
                            for h in self._hallazgos_de(s)), key=lambda h: (h.get("fecha_hora") or "", h.get("id") or 0))
        resultado["hallazgos"] = hallazgos
        resultado["resumen_hallazgos"] = {
            "por_severidad": dict(Counter(h["severidad"] for h in hallazgos)),
            "por_categoria": dict(Counter(d["categoria"] for h in hallazgos for d in h["detecciones"])),
            "por_tipo": dict(Counter(h["tipo"] for h in hallazgos)),
        }
        resultado["deteccion"] = dict(Counter((s["resultado"] or {}).get("deteccion", {}).get("estado", "sin datos")
                                              for s in tr["subtareas"].values()
                                              if s["estado"] == "completed" and (s["tipo"] == "texto" or
                                              s.get("tipo_archivo") in TIPOS_POOL)))
        try:
            resultado["evaluacion"] = self._evaluar(tr, hallazgos)
        except Exception as e:  # noqa: BLE001 — la evaluación es un extra: no debe impedir cerrar el trabajo
            resultado["evaluacion"] = {"error": f"{type(e).__name__}: {e}"}
        tr["resultado"] = resultado

    # ============================================================ hallazgos, alertas y evaluación
    @staticmethod
    def _hallazgos_de(st: dict) -> list[dict]:
        """Hallazgos de una sub-tarea terminada, en un formato común para texto, imagen y audio."""
        r = st.get("resultado") or {}
        if st["tipo"] == "texto":
            lista = [{"tipo": "texto", **h} for h in r.get("hallazgos") or []]
        elif r.get("detecciones"):
            m = st.get("mensaje") or {}
            tipo = "imagen" if st.get("tipo_archivo") == "imagen" else "audio"
            lista = [{"tipo": tipo, "archivo": st["archivo"], "id": m.get("mensaje_id"), "linea": m.get("linea"),
                      "autor": m.get("autor"), "fecha_hora": m.get("fecha_hora"), "detecciones": r["detecciones"],
                      "transcripcion": (r.get("analisis") or {}).get("transcripcion")}]
        else:
            return []
        for h in lista:
            h["severidad"] = max((d["severidad"] for d in h["detecciones"]), key=lambda x: ORDEN_SEVERIDAD.get(x, 0))
            h["worker"] = st.get("worker")
        return lista

    def _alerta(self, nivel: str, tr: dict, texto: str) -> None:
        self._n_alerta += 1
        self.alertas.appendleft({"n": self._n_alerta, "nivel": nivel, "hora": _iso(time.time()), "caso": tr["caso"],
                                 "chat": tr["chat"], "trabajo": tr["id"], "texto": texto})
        print(f"[alerta {nivel}] {tr['chat']}: {texto}")

    def _verdad(self, archivo: str, caso: str) -> list[dict] | None:
        ruta = VERDAD / archivo
        if not ruta.exists():
            return None
        with open(ruta, encoding="utf-8") as f:
            return [fila for fila in csv.DictReader(f) if fila.get("caso") == caso]

    def _evaluar(self, tr: dict, hallazgos: list[dict]) -> dict | None:
        """Compara con el ground truth del dataset generado (si el caso viene de ahí)."""
        if not self._verdad("casos.csv", tr["caso"]):
            return None
        evaluacion: dict = {"fuente": str(VERDAD)}
        if all(s["estado"] == "completed" for s in tr["subtareas"].values() if s["tipo"] == "texto"):
            mensajes, _ = self.cargar(tr["caso"])
            textos = {m.id for m in mensajes if m.tipo == "texto"}
            reales = {int(f["id"]) for f in self._verdad("mensajes_etiquetados.csv", tr["caso"]) or []
                      if f["etiqueta"] == "positivo"}
            predichos = {h["id"] for h in hallazgos if h["tipo"] == "texto"}
            evaluacion["texto"] = metricas(predichos & textos, reales & textos, len(textos))
            evaluacion["texto"]["omitidos"] = sorted(reales - predichos)[:20]
            evaluacion["texto"]["falsos"] = sorted(predichos - reales)[:20]
        for tipo, archivo in (("imagen", "imagenes.csv"), ("audio", "audios.csv")):
            hechos = {s["archivo"]: s for s in tr["subtareas"].values() if s["tipo"] == "adjunto"
                      and TIPOS_POOL.get(s.get("tipo_archivo")) == tipo and s["estado"] == "completed"
                      and (s["resultado"] or {}).get("deteccion", {}).get("estado") == "ok"}
            filas = [f for f in self._verdad(archivo, tr["caso"]) or [] if f["archivo"] in hechos]
            if not filas:
                continue
            reales = {f["archivo"] for f in filas if f["positivo"] == "True"}
            predichos = {f["archivo"] for f in filas if hechos[f["archivo"]]["resultado"].get("detecciones")}
            evaluacion[tipo] = metricas(predichos, reales, len(filas))
            evaluacion[tipo]["omitidos"] = sorted(reales - predichos)[:20]
            evaluacion[tipo]["falsos"] = sorted(predichos - reales)[:20]
            if tipo == "audio":
                errores = [w for f in filas if f.get("transcripcion")
                           if (w := wer(f["transcripcion"], (hechos[f["archivo"]]["resultado"].get("analisis") or {})
                                        .get("transcripcion", ""))) is not None]
                evaluacion[tipo]["wer_promedio"] = round(sum(errores) / len(errores), 3) if errores else None
        return evaluacion

    # ============================================================ consultas
    def _workers(self) -> list[dict]:
        ahora = time.time()
        for w in [w for w, x in self.workers.items() if ahora - x["visto"] > 600]:
            del self.workers[w]                               # sin señal hace más de 10 min
        lista = [{**x, "activo": ahora - x["visto"] < SIN_SENAL, "hace": round(ahora - x["visto"], 1)}
                 for x in self.workers.values()]
        return sorted(lista, key=lambda x: (not x["activo"], x["worker"]))

    def _resumen(self, tr: dict) -> dict:
        sts = list(tr["subtareas"].values())
        cuenta = {}
        for s in sts:
            cuenta[s["estado"]] = cuenta.get(s["estado"], 0) + 1
        fin = tr["terminado"] or time.time()
        return {"id": tr["id"], "caso": tr["caso"], "chat": tr["chat"], "estado": tr["estado"],
                "prioridad": tr["prioridad"], "creado": _iso(tr["creado"]), "terminado": _iso(tr["terminado"]),
                "bloques": tr["bloques"], "adjuntos": tr["adjuntos"], "total": len(sts),
                "terminadas": sum(s["estado"] in FINALES_ST for s in sts), "por_estado": cuenta,
                "segundos": round(fin - tr["creado"], 2),
                "hallazgos": ((tr.get("resultado") or {}).get("resumen_hallazgos") or {}).get("por_severidad")}

    def estado(self) -> dict:
        with self.lock:
            trabajos = sorted(self.trabajos.values(), key=lambda t: -t["creado"])[:50]
            return _json({"configurado": self.configurado, "conectado": self.conectado, "error": self.error,
                          "rabbit": self.destino, "workers": self._workers(),
                          "trabajos": [self._resumen(t) for t in trabajos], "alertas": list(self.alertas)[:50]})

    def _ultimos_con_hallazgos(self) -> dict[str, dict]:
        """Por caso, el procesamiento terminado más reciente que tenga hallazgos calculados."""
        ultimos: dict[str, dict] = {}
        for tr in self.trabajos.values():
            r = tr.get("resultado") or {}
            if tr["estado"] in ("completed", "partially_completed") and "hallazgos" in r:
                if tr["caso"] not in ultimos or tr["creado"] > ultimos[tr["caso"]]["creado"]:
                    ultimos[tr["caso"]] = tr
        return ultimos

    def riesgo_por_caso(self) -> dict[str, dict]:
        """Resumen para la lista de conversaciones: {caso: {"alta": n, "media": n, "trabajo": id}}."""
        with self.lock:
            return {caso: {**(tr["resultado"].get("resumen_hallazgos") or {}).get("por_severidad", {}),
                           "trabajo": tr["id"], "terminado": _iso(tr["terminado"])}
                    for caso, tr in self._ultimos_con_hallazgos().items()}

    def en_curso_por_caso(self) -> dict[str, dict]:
        """Procesamientos que todavía no terminan: {caso: {trabajo, terminadas, total}}."""
        with self.lock:
            salida = {}
            for tr in self.trabajos.values():
                if tr["estado"] not in FINALES_TR:
                    sts = tr["subtareas"].values()
                    salida[tr["caso"]] = {"trabajo": tr["id"], "estado": tr["estado"], "total": len(sts),
                                          "terminadas": sum(st["estado"] in FINALES_ST for st in sts)}
            return salida

    def hallazgos_globales(self) -> dict:
        """Todos los hallazgos de todas las conversaciones (último procesamiento de cada una)."""
        with self.lock:
            hallazgos, casos = [], {}
            for caso, tr in self._ultimos_con_hallazgos().items():
                casos[caso] = {"chat": tr["chat"], "trabajo": tr["id"], "terminado": _iso(tr["terminado"]),
                               "por_severidad": (tr["resultado"].get("resumen_hallazgos") or {}).get("por_severidad", {})}
                hallazgos += [{**h, "caso": caso, "chat": tr["chat"], "trabajo": tr["id"]} for h in tr["resultado"]["hallazgos"]]
            hallazgos.sort(key=lambda h: (h.get("fecha_hora") or "", h["caso"]), reverse=True)
            return _json({"casos": casos, "hallazgos": hallazgos})

    def trabajos_de(self, caso: str) -> list[dict]:
        with self.lock:
            return _json([self._resumen(t) for t in sorted(self.trabajos.values(), key=lambda t: -t["creado"])
                          if t["caso"] == caso])

    def detalle(self, id_: str) -> dict:
        with self.lock:
            tr = self.trabajos.get(id_)
            if not tr:
                raise ErrorTrabajo("Trabajo no encontrado")
            subtareas = []
            por_worker: dict[str, dict] = {}
            for s in sorted(tr["subtareas"].values(), key=lambda s: (s["tipo"] != "texto", s["indice"])):
                d = {k: v for k, v in s.items() if k != "resultado"}
                d["inicio"], d["fin"] = _iso(s["inicio"]), _iso(s["fin"])
                if s["tipo"] == "adjunto":
                    d["resultado"] = s["resultado"]               # pequeño: hash y metadatos
                subtareas.append(d)
                if s["worker"] and s["estado"] == "completed":
                    w = por_worker.setdefault(s["worker"], {"worker": s["worker"], "subtareas": 0, "segundos": 0.0})
                    w["subtareas"] += 1
                    w["segundos"] += s["duracion"] or 0
            fin = tr["terminado"] or time.time()
            computo = sum(w["segundos"] for w in por_worker.values())
            total = fin - tr["creado"]
            metricas = {
                "segundos_total": round(total, 3),
                "segundos_computo": round(computo, 3),
                "workers_usados": len(por_worker),
                "reintentos": sum(s["intentos"] for s in tr["subtareas"].values()),
                "reasignaciones": sum(s["reasignaciones"] for s in tr["subtareas"].values()),
                "por_worker": sorted((dict(w, segundos=round(w["segundos"], 3)) for w in por_worker.values()),
                                     key=lambda w: -w["subtareas"]),
            }
            return _json({**self._resumen(tr), "subtareas": subtareas, "metricas": metricas,
                          "resultado": tr["resultado"]})
