"""
M1 · Comparación de estrategias de carga de modelos (memoria total para N workers).

  A) independientes: N procesos; cada uno importa las librerías y carga SU copia del modelo.
  B) fork:           un proceso carga el modelo y luego hace fork() N veces. Padre e hijos
                     comparten las páginas físicas (copy-on-write): una página solo se copia
                     cuando alguno la escribe.
  C) hilos:          un proceso, un modelo, N hilos (lo que hace hoy worker.py).

Para cada estrategia y cada N se mide, con todos los procesos vivos:
  · suma de PSS (Proportional Set Size): cada página compartida se reparte entre quienes la
    comparten → la suma de PSS ES la memoria física que usan juntos.
  · suma de RSS: cuenta cada página compartida una vez POR PROCESO (exagera; se muestra para
    ver la diferencia con PSS).
  · suma de USS: solo páginas privadas.
  · memory.current del cgroup (lo que el kernel le cobra al contenedor), si existe.
Dos momentos: recién cargado/creado ("reposo") y después de que cada uno procesó un bloque
("tras inferencia"), porque el copy-on-write se va perdiendo a medida que los hijos escriben.

Modelo: el de texto (pysentimiento hate_speech), el que usan todas las sub-tareas de texto.
  python estrategias_carga.py --n 1,2,3 --salida /salida
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
TEXTOS = ["hola, ya llegaste?", "sos un idiota, no te soporto", "te voy a matar si no venís", "mañana nos vemos"] * 16
CG = Path("/sys/fs/cgroup/memory.current")


def mem(pid) -> dict:
    d = {}
    for linea in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
        p = linea.split()
        if len(p) >= 2 and p[0].endswith(":") and p[1].isdigit():
            d[p[0][:-1]] = int(p[1]) / 1024
    return {"rss": d["Rss"], "pss": d["Pss"], "uss": d.get("Private_Clean", 0) + d.get("Private_Dirty", 0)}


def cgroup_mb() -> float | None:
    try:
        return int(CG.read_text()) / 2 ** 20
    except OSError:
        return None


def suma(pids) -> dict:
    ms = [mem(p) for p in pids]
    t = {k: round(sum(m[k] for m in ms), 1) for k in ("rss", "pss", "uss")}
    t["procesos"] = len(pids)
    cg = cgroup_mb()
    t["cgroup"] = round(cg, 1) if cg is not None else None
    return t


def cargar():
    import torch
    torch.set_num_threads(1)          # sin hilos de OpenMP antes del fork (no son seguros tras fork)
    from pysentimiento import create_analyzer
    return create_analyzer(task="hate_speech", lang="es")


def esperar_orden():
    return sys.stdin.readline().strip()


# ------------------------------------------------------------ procesos hijos
def hijo_independiente():
    an = cargar()
    print("LISTO", flush=True)
    if esperar_orden() == "inferir":
        an.predict(TEXTOS)
        gc.collect()
        print("LISTO", flush=True)
    esperar_orden()


def maestro_fork(n: int):
    """Carga el modelo y hace fork n veces. Cada hijo espera una señal (por un pipe) para inferir."""
    an = cargar()
    gc.collect()
    gc.freeze()        # saca los objetos actuales del recolector: menos escrituras (y menos copias COW) en los hijos
    hijos, pipes = [], []
    for _ in range(n):
        r_orden, w_orden = os.pipe()
        r_listo, w_listo = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(w_orden)
            os.close(r_listo)
            os.read(r_orden, 1)                   # esperar la orden de inferir
            an.predict(TEXTOS)
            gc.collect()
            os.write(w_listo, b"1")
            os.read(r_orden, 1)                   # esperar la orden de terminar
            os._exit(0)
        os.close(r_orden)
        os.close(w_listo)
        hijos.append(pid)
        pipes.append((w_orden, r_listo))
    print("PIDS " + json.dumps(hijos), flush=True)
    if esperar_orden() == "inferir":
        for w, _ in pipes:
            os.write(w, b"1")
        for _, r in pipes:
            os.read(r, 1)
        print("LISTO", flush=True)
    esperar_orden()
    for w, _ in pipes:
        os.write(w, b"1")
    for p in hijos:
        os.waitpid(p, 0)


def proceso_hilos(n: int):
    an = cargar()
    print("LISTO", flush=True)
    if esperar_orden() == "inferir":
        hilos = [threading.Thread(target=an.predict, args=(TEXTOS,)) for _ in range(n)]
        for h in hilos:
            h.start()
        for h in hilos:
            h.join()
        gc.collect()
        print("LISTO", flush=True)
    esperar_orden()


# ------------------------------------------------------------ orquestador
def lanzar(*args):
    p = subprocess.Popen([sys.executable, __file__, *args], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    VIVOS.append(p)
    return p


def leer(p, prefijo="LISTO"):
    while True:
        linea = p.stdout.readline()
        if not linea:
            raise RuntimeError("el proceso terminó antes de tiempo")
        if linea.startswith(prefijo):
            return linea


def ordenar(ps, orden):
    for p in ps:
        p.stdin.write(orden + "\n")
        p.stdin.flush()


VIVOS: list = []


def medir(estrategia: str, n: int) -> dict:
    try:
        return _medir(estrategia, n)
    except RuntimeError:
        for p in VIVOS:                       # los que sobrevivieron al OOM se terminan antes de la siguiente
            p.kill()
            p.wait()
        VIVOS.clear()
        raise


def _medir(estrategia: str, n: int) -> dict:
    base = cgroup_mb()
    t0 = time.perf_counter()
    if estrategia == "independientes":
        ps = [lanzar("--hijo", "independiente") for _ in range(n)]
        for p in ps:
            leer(p)
        pids = [p.pid for p in ps]
    elif estrategia == "fork":
        ps = [lanzar("--hijo", "fork", "--cuantos", str(n))]
        pids = [ps[0].pid] + json.loads(leer(ps[0], "PIDS")[5:])
    else:
        ps = [lanzar("--hijo", "hilos", "--cuantos", str(n))]
        leer(ps[0])
        pids = [ps[0].pid]
    listo = round(time.perf_counter() - t0, 1)
    time.sleep(1)
    reposo = suma(pids)
    ordenar(ps, "inferir")
    for p in ps:
        leer(p)
    time.sleep(1)
    tras = suma(pids)
    ordenar(ps, "salir")
    for p in ps:
        p.wait(timeout=120)
    VIVOS.clear()
    r = {"estrategia": estrategia, "n": n, "segundos_hasta_listo": listo, "reposo": reposo, "tras_inferencia": tras,
         "cgroup_base": round(base, 1) if base is not None else None}
    print(f"  {estrategia:>14} N={n}: PSS {reposo['pss']:.0f} → {tras['pss']:.0f} MB · RSS {tras['rss']:.0f} MB · "
          f"USS {tras['uss']:.0f} MB · cgroup {tras['cgroup']} MB · listo en {listo} s", flush=True)
    return r


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--hijo")
    ap.add_argument("--cuantos", type=int, default=1)
    ap.add_argument("--n", default="1,2,3")
    ap.add_argument("--estrategias", default="independientes,fork,hilos")
    ap.add_argument("--salida", default="/salida")
    a = ap.parse_args()
    if a.hijo == "independiente":
        hijo_independiente()
    elif a.hijo == "fork":
        maestro_fork(a.cuantos)
    elif a.hijo == "hilos":
        proceso_hilos(a.cuantos)
    else:
        resultados = []
        for n in [int(x) for x in a.n.split(",")]:
            for e in a.estrategias.split(","):
                try:
                    resultados.append(medir(e, n))
                except RuntimeError:     # un proceso murió: con el límite del contenedor, el kernel lo mató (OOM)
                    resultados.append({"estrategia": e, "n": n, "error": "un proceso murió (OOM dentro del límite del contenedor)",
                                       "cgroup_oom_kills": (Path("/sys/fs/cgroup/memory.events").read_text().split("oom_kill ")[1].split()[0]
                                                            if Path("/sys/fs/cgroup/memory.events").exists() else None)})
                    print(f"  {e:>14} N={n}: ✗ OOM", flush=True)
                    time.sleep(3)
        Path(a.salida).mkdir(parents=True, exist_ok=True)
        (Path(a.salida) / "estrategias_carga.json").write_text(json.dumps(resultados, indent=2), encoding="utf-8")
