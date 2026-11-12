"""
M1 · Gráfico de memoria por worker en el tiempo (a partir de memoria_workers.csv).

Panel superior: memoria del cgroup de cada worker (lo que el kernel le cobra al contenedor)
con su límite (línea punteada) y los OOM marcados con ✕.
Panel inferior: sub-tareas en curso de cada worker (mismo eje de tiempo, otro gráfico:
nunca dos escalas en un mismo eje).

  python distribuido/m1/graficar_memoria.py docs/mediciones/m1/rafaga --titulo "Ráfaga de 20 casos"
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

SERIES = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4"]     # orden fijo por worker
TINTA, TINTA_2, REJILLA = "#263B38", "#5b6f6b", "#E1ECE8"


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("carpeta", type=Path)
    ap.add_argument("--titulo", default="Memoria por worker durante la ráfaga")
    ap.add_argument("--salida", type=Path)
    a = ap.parse_args()
    filas = list(csv.DictReader(open(a.carpeta / ".metricas" / "memoria_workers.csv", encoding="utf-8")))
    resumen = json.loads((a.carpeta / "resumen.json").read_text()) if (a.carpeta / "resumen.json").exists() else {}
    t0 = resumen.get("t0") or min(float(f["t"]) for f in filas)
    workers = sorted({f["worker"] for f in filas})

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.edgecolor": REJILLA,
                         "axes.labelcolor": TINTA_2, "xtick.color": TINTA_2, "ytick.color": TINTA_2})
    fig, (arriba, abajo) = plt.subplots(2, 1, figsize=(10, 6.2), sharex=True, gridspec_kw={"height_ratios": [3, 1.2]})
    for i, w in enumerate(workers):
        color = SERIES[i % len(SERIES)]
        fs = [f for f in filas if f["worker"] == w]
        t = [float(f["t"]) - t0 for f in fs]
        uso = [num(f["cgroup_mb"]) or num(f["rss_mb"]) for f in fs]
        arriba.plot(t, uso, color=color, lw=2, label=w, solid_capstyle="round")
        lim = next((num(f["limite_mb"]) for f in fs if num(f["limite_mb"])), None)
        if lim:
            arriba.axhline(lim, color=color, lw=1.2, ls=(0, (4, 3)))
            arriba.annotate(f"límite {w}: {lim:.0f} MB", (t[-1], lim), xytext=(-4, 4), textcoords="offset points",
                            ha="right", va="bottom", color=TINTA_2, fontsize=9)
        # OOM: el contador oom_kills sube entre dos latidos
        previo = None
        for f, tt, u in zip(fs, t, uso):
            k = num(f["oom_kills"])
            if previo is not None and k is not None and k > previo:
                arriba.plot([tt], [lim or u], marker="X", ms=10, color=color, mec="white", mew=1.5, ls="none")
                arriba.annotate("OOM", (tt, lim or u), xytext=(6, 6), textcoords="offset points", color=TINTA, fontsize=9,
                                fontweight="bold")
            previo = k if k is not None else previo
        abajo.step(t, [num(f["en_curso"]) or 0 for f in fs], where="post", color=color, lw=2, label=w)

    arriba.set_ylabel("memoria del cgroup (MB)")
    arriba.set_ylim(bottom=0)
    abajo.set_ylabel("sub-tareas\nen curso")
    abajo.set_xlabel("segundos desde el envío de la ráfaga")
    if resumen.get("segundos_total"):
        for ax in (arriba, abajo):
            ax.axvline(resumen["segundos_total"], color=TINTA_2, lw=1, ls=":")
        arriba.annotate("terminan todos los casos", (resumen["segundos_total"], 0), xytext=(4, 6),
                        textcoords="offset points", color=TINTA_2, fontsize=9)
    for ax in (arriba, abajo):
        ax.grid(axis="y", color=REJILLA, lw=0.8)
        ax.spines[["top", "right"]].set_visible(False)
        ax.axvline(0, color=TINTA_2, lw=1, ls=":")
    if len(workers) > 1:
        arriba.legend(frameon=False, loc="upper left", ncol=len(workers))
    arriba.set_title(a.titulo, loc="left", color=TINTA, fontsize=12, fontweight="bold")
    fig.tight_layout()
    salida = a.salida or a.carpeta / "memoria.png"
    fig.savefig(salida, dpi=150)
    print(f"gráfico: {salida}")


if __name__ == "__main__":
    main()
