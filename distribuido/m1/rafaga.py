"""
M1 / B.6 · Ráfaga de casos concurrentes con un coordinador propio (para medir).

Levanta un coordinador aparte (colas con prefijo "wa.m1." → no se mezcla con la app ni con
otros workers), envía N casos A LA VEZ y espera a que todos terminen. Mientras tanto, los
latidos de los workers quedan en <salida>/.metricas/memoria_workers.csv (memoria del cgroup
de cada worker en el tiempo), que luego grafica graficar_memoria.py.

Solo usa el DATASET GENERADO (dataset_generado/importados/): ningún chat real.

  python distribuido/m1/rafaga.py --casos 20 --salida docs/mediciones/m1/rafaga
Los workers se lanzan aparte con el mismo prefijo (WA_PREFIJO_COLAS=wa.m1.) y
COORDINADOR_URL=http://<esta PC>:8201. Ver docs/modulo-M1-memoria.md.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import threading
import time
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[2]
ap = argparse.ArgumentParser()
ap.add_argument("--casos", type=int, default=20, help="casos enviados a la vez")
ap.add_argument("--bloques", type=int, default=4)
ap.add_argument("--sin-adjuntos", action="store_true", help="solo texto (sin fotos ni audios)")
ap.add_argument("--solo", help="lista de casos separados por coma (por defecto, en orden del catálogo)")
ap.add_argument("--salida", type=Path, required=True)
ap.add_argument("--prefijo", default="wa.m1.")
ap.add_argument("--puerto-archivos", type=int, default=8201)
ap.add_argument("--workers", type=int, default=1, help="workers que hay que esperar antes de empezar")
ap.add_argument("--max-min", type=float, default=40, help="tiempo máximo de la prueba")
a = ap.parse_args()
os.environ["WA_PREFIJO_COLAS"] = a.prefijo                     # antes de importar comun
for p in (RAIZ / "mensajeria", RAIZ / "distribuido"):
    sys.path.insert(0, str(p))

import web  # noqa: E402
from comun import POOLS, config, parametros_rabbit  # noqa: E402
from coordinador import FINALES_TR, Plataforma  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

GENERADOS = RAIZ / "dataset_generado" / "casos"
IMPORTADOS = RAIZ / "dataset_generado" / "importados"


def preparar_dataset() -> list[str]:
    """Importa (una vez) los casos generados a dataset_generado/importados/."""
    import csv
    from importar import importar
    catalogo = list(csv.DictReader(open(RAIZ / "dataset_generado" / "verdad" / "casos.csv", encoding="utf-8")))
    IMPORTADOS.mkdir(parents=True, exist_ok=True)
    for c in catalogo:
        if (IMPORTADOS / c["caso"] / "metadata.json").exists():
            continue
        entrada = GENERADOS / c["caso"]
        nombre = (f"WhatsApp Chat - {c['chat']}.zip" if c["formato"] == "ios"
                  else next(entrada.glob("*.txt")).name)
        importar(entrada, IMPORTADOS, fuente="generador", declaro=False, nombre=c["caso"], prioridad=int(c["prioridad"]),
                 sobrescribir=True, nombre_entrada=nombre)
        print(f"  importado {c['caso']}")
    return [c["caso"] for c in catalogo]


def vaciar_colas() -> None:
    import pika
    con = pika.BlockingConnection(parametros_rabbit())
    canal = con.channel()
    for cola in POOLS.values():
        try:
            canal.queue_purge(cola)
        except Exception:  # noqa: BLE001 — la cola todavía no existe
            canal = con.channel()
    con.close()


def main() -> None:
    casos = preparar_dataset()
    if a.solo:
        casos = a.solo.split(",")
    elegidos = [casos[i % len(casos)] for i in range(a.casos)]
    a.salida.mkdir(parents=True, exist_ok=True)
    for viejo in (a.salida / ".trabajos").glob("*.json"):
        viejo.unlink()
    csv_mem = a.salida / ".metricas" / "memoria_workers.csv"
    if csv_mem.exists():
        csv_mem.unlink()

    web.DATASET = IMPORTADOS
    plataforma = Plataforma(a.salida, web.cargar_caso)
    plataforma.iniciar()
    web.ManejadorWorkers.TOKEN = config("WORKER_TOKEN") or ""
    srv = ThreadingHTTPServer(("0.0.0.0", a.puerto_archivos), web.ManejadorWorkers)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    vaciar_colas()

    print(f"[ráfaga] esperando {a.workers} worker(s) con prefijo {a.prefijo} …")
    while True:
        activos = [w for w in plataforma.estado()["workers"] if w["activo"]]
        if plataforma.conectado and len(activos) >= a.workers:
            break
        time.sleep(1)
    print(f"[ráfaga] workers: {', '.join(w['worker'] for w in activos)}. Midiendo 10 s de reposo…")
    time.sleep(10)

    t0 = time.time()
    ids = [plataforma.crear(c, a.bloques, not a.sin_adjuntos)["id"] for c in elegidos]
    print(f"[ráfaga] {len(ids)} casos enviados a la vez ({a.bloques} bloques c/u, adjuntos: {not a.sin_adjuntos})")
    ultimo = ""
    while time.time() - t0 < a.max_min * 60:
        with plataforma.lock:
            trs = [plataforma.trabajos[i] for i in ids]
            hechos = sum(t["estado"] in FINALES_TR for t in trs)
            sts = [s for t in trs for s in t["subtareas"].values()]
        linea = f"{hechos}/{len(ids)} casos · {sum(s['estado'] == 'completed' for s in sts)}/{len(sts)} sub-tareas"
        if linea != ultimo:
            print(f"  [{time.time() - t0:6.0f} s] {linea}")
            ultimo = linea
        if hechos == len(ids):
            break
        time.sleep(1)
    print("[ráfaga] 15 s más de medición después de terminar…")
    time.sleep(15)

    with plataforma.lock:
        trs = [json.loads(json.dumps(plataforma.trabajos[i])) for i in ids]
    lat = [t["terminado"] - t["creado"] for t in trs if t.get("terminado")]
    sts = [s for t in trs for s in t["subtareas"].values()]
    total = max((t["terminado"] or time.time()) for t in trs) - t0
    resumen = {
        "t0": t0, "casos": len(ids), "bloques": a.bloques, "adjuntos": not a.sin_adjuntos,
        "por_estado": {e: sum(t["estado"] == e for t in trs) for e in {t["estado"] for t in trs}},
        "subtareas": len(sts),
        "subtareas_por_estado": {e: sum(s["estado"] == e for s in sts) for e in {s["estado"] for s in sts}},
        "segundos_total": round(total, 1),
        "throughput_casos_min": round(len(lat) / total * 60, 2) if total else None,
        "throughput_subtareas_s": round(sum(s["estado"] == "completed" for s in sts) / total, 2) if total else None,
        "latencia_s": {"p50": round(statistics.median(lat), 1), "p95": round(sorted(lat)[int(0.95 * (len(lat) - 1))], 1),
                       "max": round(max(lat), 1), "min": round(min(lat), 1)} if lat else None,
        "oom": [{"subtarea": s["id"], **o} for s in sts for o in s.get("oom", [])],
        "reintentos": sum(s.get("intentos", 0) for s in sts),
        "reasignaciones": sum(s.get("reasignaciones", 0) for s in sts),
        "errores": sorted({s["error"] for s in sts if s.get("error")})[:10],
        "workers": {w["worker"]: {"pools": w.get("pools"), "concurrencia": w.get("concurrencia"),
                                  "completadas": w.get("completadas"), "memoria": w.get("memoria")}
                    for w in plataforma.estado()["workers"]},
    }
    (a.salida / "resumen.json").write_text(json.dumps(resumen, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({k: v for k, v in resumen.items() if k != "workers"}, indent=2, ensure_ascii=False))
    srv.shutdown()
    os._exit(0)


if __name__ == "__main__":
    main()
