"""
B.6 · Pruebas de carga y tolerancia a fallos sobre la plataforma real (usa la API de mensajeria/web.py).

  python distribuido/b6/escenarios.py rafaga      --casos 20      ráfaga concurrente: latencia, throughput, CPU/RAM por nodo
  python distribuido/b6/escenarios.py caida                       caída de un worker a mitad de un caso (se detiene a mano)
  python distribuido/b6/escenarios.py reinicio                    reinicio del coordinador con un caso en curso (a mano)
  python distribuido/b6/escenarios.py cancelacion                 cancelación de un caso pesado: liberación de recursos

Cada escenario guarda en docs/mediciones/b6/<escenario>/:
  linea_de_tiempo.csv (cambios de estado de cada sub-tarea, muestreados cada 0,5 s), resumen.json y gráficos.
Solo usa casos del dataset generado. Requiere la app corriendo (python mensajeria/web.py) y los workers conectados.
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

R = Path(__file__).resolve().parents[2]
SALIDA = R / "docs" / "mediciones" / "b6"
METRICAS = R / "dataset_whatsapp" / ".metricas" / "recursos_workers.csv"
FINALES = {"completed", "partially_completed", "failed", "cancelled"}

ap = argparse.ArgumentParser()
ap.add_argument("escenario", choices=["rafaga", "caida", "reinicio", "cancelacion"])
ap.add_argument("--app", default="http://localhost:8100")
ap.add_argument("--casos", type=int, default=20)
ap.add_argument("--bloques", type=int, default=4)
ap.add_argument("--sin-adjuntos", action="store_true")
ap.add_argument("--caso", help="caso para caida/reinicio/cancelacion")
a = ap.parse_args()


def api(ruta: str, datos: dict | None = None, intentos: int = 1):
    for i in range(intentos):
        try:
            pedido = urllib.request.Request(a.app + ruta, method="POST" if datos is not None else "GET",
                                            data=json.dumps(datos).encode() if datos is not None else None,
                                            headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(pedido, timeout=30) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise SystemExit(f"[✗] {ruta}: {json.loads(e.read() or b'{}').get('error', e)}") from e
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if i == intentos - 1:
                raise
            time.sleep(1)


def casos_sinteticos(pesados: bool | None = None) -> list[str]:
    cat = {c["caso"]: c for c in csv.DictReader(open(R / "dataset_generado/verdad/casos.csv", encoding="utf-8"))}
    disponibles = {c["caso"] for c in api("/api/casos")["casos"] if c.get("fuente") == "generador"}
    lista = [c for c in cat if c in disponibles]
    if pesados is not None:
        lista = [c for c in lista if (cat[c]["tipo"] == "pesado") == pesados]
    return lista


def preparar(nombre: str) -> Path:
    d = SALIDA / nombre
    d.mkdir(parents=True, exist_ok=True)
    p = api("/api/plataforma")
    activos = [w for w in p["workers"] if w["activo"]]
    if not p.get("conectado"):
        raise SystemExit(f"[✗] La app no está conectada a RabbitMQ: {p.get('error')}")
    print(f"[B.6] {len(activos)} worker(s) activos: " + ", ".join(
        f"{w['worker']} ({(w.get('dispositivo') or 'cpu').upper()}, {', '.join(w.get('pools') or [])})" for w in activos))
    if len(activos) < 2:
        print("[!] Conviene tener al menos 2–3 workers conectados para esta prueba.")
    return d


class Seguimiento:
    """Muestrea el detalle de los trabajos cada 0,5 s y registra cada cambio de estado o de worker."""

    def __init__(self, ids: list[str]):
        self.ids, self.previo, self.filas, self.t0 = ids, {}, [], time.time()

    def muestrear(self) -> list[dict]:
        detalles = []
        for i in self.ids:
            try:
                d = api(f"/api/trabajos/{i}", intentos=1)
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                return []                                       # la app se está reiniciando
            detalles.append(d)
            for s in d["subtareas"]:
                clave = (i, s["id"])
                ahora = (s["estado"], s.get("worker"))
                if self.previo.get(clave) != ahora:
                    self.previo[clave] = ahora
                    self.filas.append({"t": round(time.time() - self.t0, 2), "trabajo": i, "subtarea": s["id"],
                                       "tipo": s["tipo"] if s["tipo"] == "texto" else s.get("tipo_archivo"),
                                       "estado": s["estado"], "worker": s.get("worker") or "",
                                       "reasignaciones": s.get("reasignaciones", 0), "intentos": s.get("intentos", 0)})
        return detalles

    def guardar(self, d: Path):
        with open(d / "linea_de_tiempo.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, ["t", "trabajo", "subtarea", "tipo", "estado", "worker", "reasignaciones", "intentos"])
            w.writeheader()
            w.writerows(self.filas)


def esperar(seg: Seguimiento, limite_min: float = 60, al_muestrear=None) -> list[dict]:
    ultimo, detalles = "", []
    while time.time() - seg.t0 < limite_min * 60:
        nuevos = seg.muestrear()
        if nuevos:
            detalles = nuevos
            hechos = sum(x["estado"] in FINALES for x in detalles)
            sts = [s for x in detalles for s in x["subtareas"]]
            linea = f"{hechos}/{len(detalles)} casos · {sum(s['estado'] == 'completed' for s in sts)}/{len(sts)} sub-tareas"
            if linea != ultimo:
                print(f"  [{time.time() - seg.t0:6.0f} s] {linea}", flush=True)
                ultimo = linea
            if al_muestrear:
                al_muestrear(detalles)
            if hechos == len(detalles):
                return detalles
        time.sleep(0.5)
    return detalles


def recursos(t0: float, t1: float) -> list[dict]:
    if not METRICAS.exists():
        return []
    return [f for f in csv.DictReader(open(METRICAS, encoding="utf-8")) if t0 - 10 <= float(f["t"]) <= t1 + 10]


def resumen_subtareas(detalles: list[dict]) -> dict:
    sts = [s for x in detalles for s in x["subtareas"]]
    return {"subtareas": len(sts), "por_estado": {e: sum(s["estado"] == e for s in sts) for e in {s["estado"] for s in sts}},
            "reasignaciones": sum(s.get("reasignaciones", 0) for s in sts), "reintentos": sum(s.get("intentos", 0) for s in sts),
            "por_worker": {w: sum(s.get("worker") == w and s["estado"] == "completed" for s in sts)
                           for w in sorted({s.get("worker") for s in sts if s.get("worker")})}}


def guardar(d: Path, res: dict, seg: Seguimiento):
    seg.guardar(d)
    (d / "resumen.json").write_text(json.dumps(res, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in res.items() if k not in ("casos_detalle", "nodos")}, indent=2, ensure_ascii=False))
    print(f"→ {d}")


def lanzar(caso: str, bloques: int, adjuntos: bool) -> str:
    return api(f"/api/casos/{caso}/paralelo", {"bloques": bloques, "adjuntos": adjuntos})["id"]


# ============================================================ escenarios
def rafaga():
    d = preparar("rafaga")
    casos = casos_sinteticos(pesados=False)[: a.casos]
    if len(casos) < a.casos:
        casos += casos_sinteticos(pesados=True)[: a.casos - len(casos)]
    t0 = time.time()
    ids = [lanzar(c, a.bloques, not a.sin_adjuntos) for c in casos]
    print(f"[B.6] {len(ids)} casos enviados a la vez ({a.bloques} bloques c/u, adjuntos: {not a.sin_adjuntos})")
    seg = Seguimiento(ids)
    seg.t0 = t0
    detalles = esperar(seg)
    t1 = time.time()
    time.sleep(12)                                              # unos latidos más después de terminar
    lat = [x["segundos"] for x in detalles if x["estado"] in FINALES]
    filas = recursos(t0, time.time())
    nodos = {}
    for f in filas:
        n = nodos.setdefault(f["worker"], {"cpu": [], "ram_mb": [], "dispositivo": f["dispositivo"], "maquina": f["maquina"]})
        if f["cpu_pct"]:
            n["cpu"].append(float(f["cpu_pct"]))
        if f["rss_mb"]:
            n["ram_mb"].append(float(f["rss_mb"]))
    res = {"t0": t0, "casos": len(ids), "segundos_total": round(t1 - t0, 1),
           "throughput_casos_min": round(len(lat) / (t1 - t0) * 60, 2),
           "latencia_s": {"p50": round(statistics.median(lat), 1), "p95": round(sorted(lat)[int(.95 * (len(lat) - 1))], 1),
                          "max": round(max(lat), 1)} if lat else None,
           "estados": {e: sum(x["estado"] == e for x in detalles) for e in {x["estado"] for x in detalles}},
           **resumen_subtareas(detalles),
           "nodos": {w: {"maquina": n["maquina"], "dispositivo": n["dispositivo"],
                         "cpu_media": round(statistics.mean(n["cpu"]), 1) if n["cpu"] else None,
                         "cpu_max": max(n["cpu"]) if n["cpu"] else None,
                         "ram_max_mb": max(n["ram_mb"]) if n["ram_mb"] else None} for w, n in nodos.items()},
           "casos_detalle": [{"caso": x["caso"], "estado": x["estado"], "segundos": x["segundos"]} for x in detalles]}
    guardar(d, res, seg)


def un_caso(nombre: str, pesado: bool, aviso: str | None, accion=None):
    d = preparar(nombre)
    caso = a.caso or casos_sinteticos(pesados=pesado)[0]
    bloques = max(a.bloques, 6)
    t0 = time.time()
    id_ = lanzar(caso, bloques, True)
    print(f"[B.6] {caso} → {id_} ({bloques} bloques + adjuntos)")
    seg = Seguimiento([id_])
    seg.t0 = t0
    marcas = {}

    def vigilar(detalles):
        sts = detalles[0]["subtareas"]
        en_curso = sum(s["estado"] == "procesando" for s in sts)
        if "accion" not in marcas and en_curso >= 2:
            marcas["accion"] = round(time.time() - t0, 2)
            if aviso:
                print("\n" + "=" * 70 + f"\n  >>> AHORA: {aviso}\n" + "=" * 70 + "\n", flush=True)
            if accion:
                accion(id_)
    detalles = esperar(seg, al_muestrear=vigilar)
    res = {"caso": caso, "trabajo": id_, "estado_final": detalles[0]["estado"] if detalles else None,
           "segundos": round(time.time() - t0, 1), "accion_en_s": marcas.get("accion"), **resumen_subtareas(detalles)}
    return d, res, seg


def caida():
    d, res, seg = un_caso("caida", False, "detené UN worker (en Colab: botón ■ de la celda; en la laptop: Ctrl+C). "
                                          "Dejá los otros funcionando.")
    guardar(d, res, seg)


def reinicio():
    d, res, seg = un_caso("reinicio", False, "reiniciá el coordinador: en la terminal de web.py, Ctrl+C y volvé a "
                                             "correr  python mensajeria/web.py  (este script espera a que vuelva).")
    guardar(d, res, seg)


def cancelacion():
    def cancelar(id_):
        time.sleep(3)                                           # que haya descargas y modelos trabajando
        api(f"/api/trabajos/{id_}/cancelar", {})
        print("[B.6] cancelación enviada", flush=True)
    d, res, seg = un_caso("cancelacion", True, None, cancelar)
    time.sleep(12)
    p = api("/api/plataforma")
    res["workers_despues"] = {w["worker"]: {"en_curso": w.get("en_curso"), "rss_mb": w.get("rss_mb")}
                              for w in p["workers"] if w["activo"]}
    filas = recursos(seg.t0, time.time())
    res["recursos"] = [{k: f[k] for k in ("t", "worker", "rss_mb", "cpu_pct", "en_curso")} for f in filas]
    guardar(d, res, seg)


{"rafaga": rafaga, "caida": caida, "reinicio": reinicio, "cancelacion": cancelacion}[a.escenario]()
