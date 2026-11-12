"""
B.6 · Gráficos de los escenarios (lee docs/mediciones/b6/<escenario>/).
  python distribuido/b6/graficar.py rafaga | caida | reinicio | cancelacion | todos
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

R = Path(__file__).resolve().parents[2]
B6 = R / "docs" / "mediciones" / "b6"
METRICAS = R / "dataset_whatsapp" / ".metricas" / "recursos_workers.csv"
SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]
GRIS = "#b9c4c1"
TINTA, TINTA_2, REJILLA = "#263B38", "#5b6f6b", "#E1ECE8"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.edgecolor": REJILLA,
                     "axes.labelcolor": TINTA_2, "xtick.color": TINTA_2, "ytick.color": TINTA_2})


def estilo(ax):
    ax.grid(axis="y", color=REJILLA, lw=0.8)
    ax.spines[["top", "right"]].set_visible(False)


def rafaga():
    d = B6 / "rafaga"
    res = json.loads((d / "resumen.json").read_text(encoding="utf-8"))
    t0, t1 = res["t0"], res["t0"] + res["segundos_total"]
    filas = [f for f in csv.DictReader(open(METRICAS, encoding="utf-8")) if t0 - 15 <= float(f["t"]) <= t1 + 15]
    workers = sorted({f["worker"] for f in filas})
    fig, ejes = plt.subplots(3, 1, figsize=(10, 8.5), sharex=True, gridspec_kw={"height_ratios": [2, 2, 1.3]})
    for i, w in enumerate(workers):
        fs = [f for f in filas if f["worker"] == w]
        t = [float(f["t"]) - t0 for f in fs]
        etiqueta = f"{w} ({(fs[0]['dispositivo'] or 'cpu').upper()})"
        for ax, col in zip(ejes, ("cpu_pct", "rss_mb", "en_curso")):
            ax.plot(t, [float(f[col] or 0) for f in fs], color=SERIES[i % len(SERIES)], lw=2,
                    label=etiqueta, drawstyle="steps-post" if col == "en_curso" else "default")
    ejes[0].set_ylabel("CPU del nodo (%)")
    ejes[1].set_ylabel("memoria del worker (MB)")
    ejes[2].set_ylabel("sub-tareas\nen curso")
    ejes[2].set_xlabel("segundos desde el envío de la ráfaga")
    for ax in ejes:
        ax.axvline(0, color=TINTA_2, lw=1, ls=":")
        ax.axvline(res["segundos_total"], color=TINTA_2, lw=1, ls=":")
        ax.set_ylim(bottom=0)
        estilo(ax)
    ejes[0].legend(frameon=False, loc="upper right", ncol=min(3, len(workers)))
    ejes[0].set_title(f"Ráfaga de {res['casos']} casos · {res['subtareas']} sub-tareas · latencia p50 "
                      f"{res['latencia_s']['p50']} s, p95 {res['latencia_s']['p95']} s · {res['throughput_casos_min']} casos/min",
                      loc="left", color=TINTA, fontsize=11, fontweight="bold")
    fig.tight_layout()
    fig.savefig(d / "recursos_por_nodo.png", dpi=150)
    # reparto de sub-tareas por worker
    fig, ax = plt.subplots(figsize=(8, 0.6 + 0.55 * len(res["por_worker"])))
    ws = list(res["por_worker"])
    ax.barh(ws, [res["por_worker"][w] for w in ws], color=[SERIES[workers.index(w) % len(SERIES)] if w in workers else GRIS for w in ws],
            height=0.6)
    for y, w in enumerate(ws):
        ax.annotate(str(res["por_worker"][w]), (res["por_worker"][w], y), xytext=(4, 0), textcoords="offset points",
                    va="center", color=TINTA, fontsize=9)
    ax.set_xlabel("sub-tareas completadas")
    ax.invert_yaxis()
    ax.spines[["top", "right"]].set_visible(False)
    ax.set_title("Reparto de la ráfaga entre los workers", loc="left", color=TINTA, fontsize=11, fontweight="bold")
    fig.tight_layout()
    fig.savefig(d / "reparto.png", dpi=150)
    print("→", d / "recursos_por_nodo.png", d / "reparto.png")


def linea_de_tiempo(nombre: str, titulo: str, marca: str):
    d = B6 / nombre
    res = json.loads((d / "resumen.json").read_text(encoding="utf-8"))
    filas = list(csv.DictReader(open(d / "linea_de_tiempo.csv", encoding="utf-8")))
    subs = list(dict.fromkeys(f["subtarea"] for f in filas))
    workers = sorted({f["worker"] for f in filas if f["worker"]})
    color = {w: SERIES[i % len(SERIES)] for i, w in enumerate(workers)}
    fin_total = max(float(f["t"]) for f in filas) + 1
    fig, ax = plt.subplots(figsize=(10, 0.8 + 0.32 * len(subs)))
    for y, s in enumerate(subs):
        ev = [f for f in filas if f["subtarea"] == s]
        for k, e in enumerate(ev):
            t = float(e["t"])
            t_sig = float(ev[k + 1]["t"]) if k + 1 < len(ev) else (t + 0.6 if e["estado"] in ("completed", "cancelled", "failed") else fin_total)
            if e["estado"] == "procesando":
                ax.barh(y, t_sig - t, left=t, height=0.6, color=color.get(e["worker"], GRIS))
            elif e["estado"] in ("pendiente", "retrying"):
                ax.barh(y, t_sig - t, left=t, height=0.25, color=GRIS)
            if e["estado"] in ("completed", "cancelled", "failed"):
                ax.plot([t], [y], marker={"completed": "o", "cancelled": "X", "failed": "s"}[e["estado"]], ms=6,
                        color=TINTA, mec="white", mew=1)
    if res.get("accion_en_s") is not None:
        ax.axvline(res["accion_en_s"], color="#c0392b", lw=1.5, ls="--")
        ax.annotate(marca, (res["accion_en_s"], -0.9), xytext=(4, 0), textcoords="offset points", color="#c0392b", fontsize=9)
    ax.set_yticks(range(len(subs)), [next(f["tipo"] for f in filas if f["subtarea"] == s) for s in subs], fontsize=8)
    ax.invert_yaxis()
    ax.set_xlabel("segundos desde que se envió el caso")
    leyenda = [Patch(color=color[w], label=w) for w in workers] + [Patch(color=GRIS, label="en cola / esperando")]
    ax.legend(handles=leyenda, frameon=False, loc="lower right", fontsize=9)
    ax.set_title(f"{titulo} · estado final: {res['estado_final']} · reasignaciones: {res['reasignaciones']}",
                 loc="left", color=TINTA, fontsize=11, fontweight="bold")
    ax.spines[["top", "right"]].set_visible(False)
    fig.tight_layout()
    fig.savefig(d / "linea_de_tiempo.png", dpi=150)
    print("→", d / "linea_de_tiempo.png")


if __name__ == "__main__":
    que = sys.argv[1] if len(sys.argv) > 1 else "todos"
    tareas = {"rafaga": rafaga,
              "caida": lambda: linea_de_tiempo("caida", "Caída de un worker", "se detiene un worker"),
              "reinicio": lambda: linea_de_tiempo("reinicio", "Reinicio del coordinador", "se reinicia el coordinador"),
              "cancelacion": lambda: linea_de_tiempo("cancelacion", "Cancelación de un caso pesado", "cancelación")}
    for k, f in tareas.items():
        if que in (k, "todos") and (B6 / k / "resumen.json").exists():
            f()
