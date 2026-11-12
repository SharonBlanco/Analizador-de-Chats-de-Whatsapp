"""
M3 · Ráfaga de subidas: profundidad de la cola y tiempos de respuesta, con y sin contrapresión.

N clientes suben un chat (.zip del dataset generado) AL MISMO TIEMPO al receptor asíncrono y
piden importarlo. Se repite con cada política de contrapresión:
  ninguna   cola sin límite
  rechazar  cola acotada; si está llena → 503 + Retry-After y el cliente reintenta
  bloquear  cola acotada; si está llena, el pedido espera conectado
Se mide la profundidad de la cola cada 0,2 s (lado servidor) y, por cliente, el tiempo total,
cuántos 503 recibió y cuánto esperó en la cola cada importación aceptada.

  python distribuido/m3/rafaga_subidas.py --clientes 30 --salida docs/mediciones/m3
Solo usa casos del dataset generado; importa a una carpeta temporal (no toca la app).
"""

from __future__ import annotations

import argparse
import csv
import http.client
import json
import shutil
import statistics
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import quote

R = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(R / "mensajeria"), str(R / "distribuido")]

import web  # noqa: E402
from organizacion import Organizacion  # noqa: E402
from receptor import Receptor  # noqa: E402

ap = argparse.ArgumentParser()
ap.add_argument("--clientes", type=int, default=30)
ap.add_argument("--politicas", default="ninguna,rechazar,bloquear")
ap.add_argument("--cola-max", type=int, default=4)
ap.add_argument("--importadores", type=int, default=2)
ap.add_argument("--salida", type=Path, default=R / "docs" / "mediciones" / "m3")
a = ap.parse_args()

# zips de casos chicos y medianos del dataset generado (sin los pesados con video)
catalogo = list(csv.DictReader(open(R / "dataset_generado" / "verdad" / "casos.csv", encoding="utf-8")))
# chats con fotos y audios (importarlos cuesta: verificar, calcular SHA-256 y guardar en el almacén); sin los pesados
casos = [c for c in catalogo if c["tipo"] in ("texto_imagen", "texto_audio", "heterogeneo")]
ZIPS = Path(tempfile.mkdtemp(prefix="m3_zips_"))
zips = []
for c in casos:
    z = ZIPS / (f"WhatsApp Chat - {c['chat']}.zip" if c["formato"] == "ios" else f"Chat de WhatsApp con {c['chat']}.zip")
    z = z.with_name(f"{c['caso']}__{z.name}")
    with zipfile.ZipFile(z, "w", zipfile.ZIP_STORED) as zf:
        for f in (R / "dataset_generado" / "casos" / c["caso"]).iterdir():
            zf.write(f, f.name)
    zips.append(z)
print(f"{len(zips)} zips preparados ({sum(z.stat().st_size for z in zips) / 2**20:.0f} MB)")


def cliente(puerto: int, z: Path, salida: dict, inicio: threading.Event):
    inicio.wait()
    t0 = time.time()
    lote = uuid.uuid4().hex
    nombre = z.name.split("__", 1)[1]
    c = http.client.HTTPConnection("127.0.0.1", puerto, timeout=600)
    c.request("POST", f"/subir?lote={lote}&archivo={quote(nombre)}", body=open(z, "rb"),
              headers={"Content-Length": str(z.stat().st_size)})
    r = c.getresponse()
    r.read()
    t_subida = time.time() - t0
    rechazos = 0
    while True:
        c = http.client.HTTPConnection("127.0.0.1", puerto, timeout=600)
        c.request("POST", "/importar", body=json.dumps({"lote": lote, "fuente": "generador", "nombre": z.stem.split("__")[0]
                                                         + "_" + lote[:6]}), headers={"Content-Type": "application/json"})
        r = c.getresponse()
        r.read()
        if r.status == 503:
            rechazos += 1
            time.sleep(int(r.getheader("Retry-After") or 2))
            continue
        break
    salida.update(segundos=round(time.time() - t0, 3), subida=round(t_subida, 3), rechazos=rechazos, estado=r.status)


def correr(politica: str, puerto: int) -> dict:
    destino = Path(tempfile.mkdtemp(prefix=f"m3_{politica}_"))
    web.DATASET, web.ORG = destino, Organizacion(destino)
    csv_cola = a.salida / f"cola_{politica}.csv"
    rec = Receptor(web.SUBIDAS, web.importar_lote, politica=politica, cola_max=a.cola_max,
                   importadores=a.importadores, csv_metricas=csv_cola)
    rec.iniciar_en_hilo("127.0.0.1", puerto)
    time.sleep(0.5)
    resultados = [{} for _ in range(a.clientes)]
    inicio = threading.Event()
    hilos = [threading.Thread(target=cliente, args=(puerto, zips[i % len(zips)], resultados[i], inicio)) for i in range(a.clientes)]
    for h in hilos:
        h.start()
    t0 = time.time()
    inicio.set()                                   # todos a la vez
    for h in hilos:
        h.join()
    total = time.time() - t0
    time.sleep(0.5)
    tiempos = [r["segundos"] for r in resultados]
    esperas = [x["espera_cola"] for x in rec.registros if x.get("espera_cola") is not None]
    res = {"politica": politica, "clientes": a.clientes, "cola_max": None if politica == "ninguna" else a.cola_max,
           "importadores": a.importadores, "t0": t0, "segundos_total": round(total, 2),
           "throughput_casos_s": round(a.clientes / total, 2),
           "ok": sum(r["estado"] == 200 for r in resultados), "errores": sum(r["estado"] != 200 for r in resultados),
           "respuestas_503": sum(r["rechazos"] for r in resultados),
           "clientes_con_503": sum(r["rechazos"] > 0 for r in resultados),
           "profundidad_max": rec.metricas["profundidad_max"],
           "tiempo_cliente_s": {"p50": round(statistics.median(tiempos), 2), "p95": round(sorted(tiempos)[int(.95 * (len(tiempos) - 1))], 2),
                                "max": round(max(tiempos), 2)},
           "espera_en_cola_s": {"p50": round(statistics.median(esperas), 2), "max": round(max(esperas), 2)} if esperas else None,
           "clientes_detalle": resultados}
    shutil.rmtree(destino, ignore_errors=True)
    print(f"  {politica:>9}: cola máx {res['profundidad_max']:>2} · espera en cola p50 {res['espera_en_cola_s']['p50']} s, "
          f"máx {res['espera_en_cola_s']['max']} s · 503: {res['respuestas_503']} · total {res['segundos_total']} s · "
          f"{res['ok']}/{a.clientes} importados", flush=True)
    return res


a.salida.mkdir(parents=True, exist_ok=True)
todos = [correr(p, 8300 + i) for i, p in enumerate(a.politicas.split(","))]
(a.salida / "rafaga_subidas.json").write_text(json.dumps(todos, indent=2, ensure_ascii=False), encoding="utf-8")
shutil.rmtree(ZIPS, ignore_errors=True)
print(f"→ {a.salida / 'rafaga_subidas.json'}")
