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

import json
import math
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Callable

import pika

from comun import (COLA_RESULTADOS, ESPERA_REINTENTO, EXCHANGE_CONTROL, EXCHANGE_LATIDOS, MAX_REINTENTOS,
                   PERSISTENTE, POOLS, SIN_SENAL, ConfiguracionIncompleta, config, declarar, parametros_rabbit)
from estadisticas import calcular, combinar, previos

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


class ErrorTrabajo(Exception):
    pass


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
        self.workers: dict[str, dict] = {}
        self.conectado = False
        self.configurado = True
        self.error: str | None = None
        self.destino = f'{config("RABBIT_HOST", "localhost")}:{config("RABBIT_PORT", "5672")}'

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
            for clave, cuerpo, prioridad in mensajes:
                canal.basic_publish(exchange=exchange, routing_key=clave,
                                    body=json.dumps(cuerpo, ensure_ascii=False).encode(),
                                    properties=pika.BasicProperties(delivery_mode=PERSISTENTE,
                                                                    priority=max(1, min(10, prioridad))))
        finally:
            conexion.close()

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
        except (ValueError, KeyError):
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
                  "reasignaciones": 0, "inicio": None, "fin": None, "duracion": None, "error": None,
                  "resultado": None, **datos}
            subtareas[st["id"]] = st
        for i, (ini, fin) in enumerate(rangos):
            nueva(tipo="texto", indice=i, ini=ini, fin=fin, mensajes=fin - ini,
                  desde=mensajes[ini].id, hasta=mensajes[fin - 1].id)
        if con_adjuntos:
            for i, (nombre, f) in enumerate(x for x in meta.get("files", {}).items() if x[0] != "chat.txt"):
                nueva(tipo="adjunto", indice=i, archivo=nombre, sha256=f.get("sha256"),
                      bytes=f.get("bytes"), tipo_archivo=f.get("tipo"))

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
        return POOLS["adjuntos"], {**base, "caso": tr["caso"], "archivo": st["archivo"],
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

    # ============================================================ flujo de tareas
    def _procesar(self, msg: dict) -> str | None:
        """Aplica un mensaje de un worker. Devuelve el id del trabajo si hay que repetir
        su cancelación a los workers."""
        tr = self.trabajos.get(msg.get("trabajo"))
        st = tr["subtareas"].get(msg.get("subtarea")) if tr else None
        if st and st["estado"] == "cancelled" and msg.get("evento") == "inicio":
            return tr["id"]
        if not st or st["estado"] in FINALES_ST:
            return None                              # trabajo borrado, o duplicado de una re-entrega
        evento, worker = msg.get("evento"), msg.get("worker")

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
        print(f"[plataforma] {tr['id']} terminado → {tr['estado']}")

    def _consolidar(self, tr: dict) -> None:
        textos = sorted((s for s in tr["subtareas"].values() if s["tipo"] == "texto"), key=lambda s: s["indice"])
        hechos = [s for s in textos if s["estado"] == "completed"]
        completo = len(hechos) == len(textos)
        resultado: dict = {"texto_completo": completo, "estadisticas": None, "verificacion": None}
        if hechos:
            combinado = _json(combinar([s["resultado"] for s in hechos]))
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
        tr["resultado"] = resultado

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
                "segundos": round(fin - tr["creado"], 2)}

    def estado(self) -> dict:
        with self.lock:
            trabajos = sorted(self.trabajos.values(), key=lambda t: -t["creado"])[:50]
            return _json({"configurado": self.configurado, "conectado": self.conectado, "error": self.error,
                          "rabbit": self.destino, "workers": self._workers(),
                          "trabajos": [self._resumen(t) for t in trabajos]})

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
