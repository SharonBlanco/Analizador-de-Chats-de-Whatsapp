"""
M3 · Throughput (MB/s) de la transferencia por bloques según el tamaño de bloque.

Levanta el servidor de archivos de los workers (el mismo de mensajeria/web.py) en un puerto
local y descarga un archivo grande con descargar_por_bloques() del worker, variando:
  · tamaño de bloque: 64 KB, 256 KB, 1 MB, 4 MB, 16 MB
  · conexiones en paralelo: 1 y 4
Cada combinación se repite --repeticiones veces; se reporta la mediana. También se mide el costo
de un bloque dañado (cuántos bytes hay que volver a pedir) y la memoria que ocupa un bloque.

  python distribuido/m3/medir_bloques.py --caso 040_fernanda_mora --salida docs/mediciones/m3
  python distribuido/m3/medir_bloques.py --url https://….trycloudflare.com …   (medir a través del túnel)

Usa solo casos del dataset generado.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

R = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(R / "mensajeria"), str(R / "distribuido"), str(R / "distribuido" / "worker")]

ap = argparse.ArgumentParser()
ap.add_argument("--caso", default="040_fernanda_mora")
ap.add_argument("--dataset", type=Path, default=R / "dataset_whatsapp")
ap.add_argument("--url", help="coordinador remoto (p. ej. el túnel); por defecto, un servidor local")
ap.add_argument("--tamanos-kb", default="64,256,1024,4096,16384")
ap.add_argument("--paralelas", default="1,4")
ap.add_argument("--repeticiones", type=int, default=3)
ap.add_argument("--salida", type=Path, default=R / "docs" / "mediciones" / "m3")
a = ap.parse_args()

PUERTO = 8291
os.environ.setdefault("WORKER_TOKEN", "token-de-medicion")
os.environ["COORDINADOR_URL"] = a.url or f"http://127.0.0.1:{PUERTO}"
os.environ.update(WA_DETECCION="0", RABBIT_USER=os.environ.get("RABBIT_USER", "x"), RABBIT_PASS=os.environ.get("RABBIT_PASS", "x"))

import web  # noqa: E402
import worker  # noqa: E402

if not a.url:
    web.DATASET = a.dataset
    web.ManejadorWorkers.TOKEN = os.environ["WORKER_TOKEN"]
    web.ManejadorWorkers.log_message = lambda *x: None
    srv = ThreadingHTTPServer(("127.0.0.1", PUERTO), web.ManejadorWorkers)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()

meta = json.loads((a.dataset / a.caso / "metadata.json").read_text(encoding="utf-8"))
nombre, f = max(((k, v) for k, v in meta["files"].items() if k != "chat.txt"), key=lambda x: x[1]["bytes"])
sub = {"caso": a.caso, "archivo": nombre, "trabajo": "medicion", "subtarea": "medicion", "sha256": f["sha256"]}
destino = Path(tempfile.mkdtemp()) / "descarga"
print(f"Archivo: {a.caso}/{nombre} · {f['bytes'] / 2**20:.0f} MB · servidor: {os.environ['COORDINADOR_URL']}")

filas = []
for par in [int(x) for x in a.paralelas.split(",")]:
    for kb in [int(x) for x in a.tamanos_kb.split(",")]:
        worker.BLOQUE, worker.PARALELAS = kb * 1024, par
        worker.descargar_por_bloques(sub, destino)               # calentamiento (manifiesto en caché)
        tiempos = []
        for _ in range(a.repeticiones):
            t0 = time.perf_counter()
            r = worker.descargar_por_bloques(sub, destino)
            tiempos.append(time.perf_counter() - t0)
            assert r["sha256_calculado"] == f["sha256"], "el archivo reensamblado no coincide"
        med = statistics.median(tiempos)
        fila = {"bloque_kb": kb, "paralelas": par, "bloques": r["transferencia"]["bloques"],
                "segundos_mediana": round(med, 3), "mb_s": round(f["bytes"] / 2**20 / med, 1),
                "mb_s_min": round(f["bytes"] / 2**20 / max(tiempos), 1), "mb_s_max": round(f["bytes"] / 2**20 / min(tiempos), 1),
                "repetir_si_se_dana_kb": kb, "memoria_por_bloque_kb": kb * par}
        filas.append(fila)
        print(f"  bloque {kb:>6} KB · {par} conexión(es): {fila['mb_s']:>7} MB/s "
              f"({fila['bloques']} pedidos, mediana de {a.repeticiones})", flush=True)

a.salida.mkdir(parents=True, exist_ok=True)
nombre_salida = "bloques_tunel.json" if a.url else "bloques_local.json"
(a.salida / nombre_salida).write_text(json.dumps({"archivo": nombre, "bytes": f["bytes"], "servidor": "túnel" if a.url else "local",
                                                   "filas": filas}, indent=2), encoding="utf-8")
print(f"→ {a.salida / nombre_salida}")
os._exit(0)
