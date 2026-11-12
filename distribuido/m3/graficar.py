"""
M3 · Gráficos de la ficha:
  rafaga_subidas.png  profundidad de la cola en el tiempo (una línea por política) y tiempo por cliente
  bloques.png         throughput según el tamaño de bloque (1 y 4 conexiones)

  python distribuido/m3/graficar.py docs/mediciones/m3
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

COLORES = {"ninguna": "#eb6834", "rechazar": "#2a78d6", "bloquear": "#1baf7a"}
NOMBRES = {"ninguna": "sin contrapresión (cola ilimitada)", "rechazar": "rechazar (503 + Retry-After)",
           "bloquear": "bloquear (el cliente espera)"}
TINTA, TINTA_2, REJILLA = "#263B38", "#5b6f6b", "#E1ECE8"
plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10, "axes.edgecolor": REJILLA,
                     "axes.labelcolor": TINTA_2, "xtick.color": TINTA_2, "ytick.color": TINTA_2})


def estilo(ax):
    ax.grid(axis="y", color=REJILLA, lw=0.8)
    ax.spines[["top", "right"]].set_visible(False)


def rafaga(d: Path):
    datos = json.loads((d / "rafaga_subidas.json").read_text(encoding="utf-8"))
    fig, (arriba, abajo) = plt.subplots(2, 1, figsize=(10, 7), gridspec_kw={"height_ratios": [3, 2]})
    for r in datos:
        p = r["politica"]
        filas = list(csv.DictReader(open(d / f"cola_{p}.csv", encoding="utf-8")))
        t = [float(f["t"]) - r["t0"] for f in filas]
        y = [int(f["profundidad"]) for f in filas]
        corte = [i for i, x in enumerate(t) if -0.5 <= x <= r["segundos_total"] + 1]
        arriba.step([t[i] for i in corte], [y[i] for i in corte], where="post", lw=2, color=COLORES[p], label=NOMBRES[p])
    lim = next((r["cola_max"] for r in datos if r["cola_max"]), None)
    if lim:
        arriba.axhline(lim, color=TINTA_2, lw=1, ls=(0, (4, 3)))
        arriba.annotate(f"límite de la cola acotada: {lim}", (0, lim), xytext=(4, 4), textcoords="offset points",
                        color=TINTA_2, fontsize=9)
    arriba.set_ylabel("pedidos en la cola de importación")
    arriba.set_xlabel("segundos desde que llegan los clientes")
    arriba.legend(frameon=False, loc="upper right")
    arriba.set_title(f"Ráfaga de {datos[0]['clientes']} subidas simultáneas · {datos[0]['importadores']} importadores",
                     loc="left", color=TINTA, fontsize=12, fontweight="bold")
    estilo(arriba)
    # tiempo de respuesta por cliente (ordenado): un punto por cliente, una franja por política
    for i, r in enumerate(datos):
        ts = sorted(c["segundos"] for c in r["clientes_detalle"])
        abajo.scatter(ts, [i] * len(ts), s=14, color=COLORES[r["politica"]], alpha=0.75, edgecolors="none")
        abajo.annotate(f"p50 {r['tiempo_cliente_s']['p50']} s · máx {r['tiempo_cliente_s']['max']} s · 503: {r['respuestas_503']}",
                       (max(ts), i), xytext=(8, -3), textcoords="offset points", color=TINTA_2, fontsize=9)
    abajo.set_yticks(range(len(datos)), [NOMBRES[r["politica"]].split(" (")[0] for r in datos])
    abajo.set_xlabel("tiempo de respuesta de cada cliente: subir + importar (s)")
    abajo.set_ylim(-0.6, len(datos) - 0.4)
    abajo.margins(x=0.25)
    estilo(abajo)
    fig.tight_layout()
    fig.savefig(d / "rafaga_subidas.png", dpi=150)
    print("→", d / "rafaga_subidas.png")


def bloques(d: Path):
    f = d / "bloques_local.json"
    if not f.exists():
        return
    datos = json.loads(f.read_text(encoding="utf-8"))
    fig, ax = plt.subplots(figsize=(9, 4.6))
    for par, color in ((1, "#eb6834"), (4, "#2a78d6")):
        fs = [x for x in datos["filas"] if x["paralelas"] == par]
        ax.plot([x["bloque_kb"] for x in fs], [x["mb_s"] for x in fs], marker="o", ms=7, lw=2, color=color,
                label="1 conexión" if par == 1 else f"{par} conexiones")
        ult = fs[-1]
        ax.annotate("1 conexión" if par == 1 else f"{par} conexiones", (ult["bloque_kb"], ult["mb_s"]), xytext=(8, 0),
                    textcoords="offset points", va="center", color=TINTA_2, fontsize=9)
    ax.set_xscale("log", base=2)
    ax.set_xticks([x["bloque_kb"] for x in datos["filas"] if x["paralelas"] == 1],
                  [f"{k // 1024} MB" if k >= 1024 else f"{k} KB" for k in (x["bloque_kb"] for x in datos["filas"] if x["paralelas"] == 1)])
    ax.axvline(1024, color=TINTA_2, lw=1, ls=":")
    ax.annotate("elegido: 1 MB", (1024, 0), xytext=(4, 6), textcoords="offset points", color=TINTA_2, fontsize=9)
    ax.set_ylim(bottom=0)
    ax.set_xlabel("tamaño de bloque")
    ax.set_ylabel("throughput (MB/s)")
    ax.legend(frameon=False, loc="upper left")
    ax.set_title(f"Transferencia por bloques · archivo de {datos['bytes'] / 2**20:.0f} MB · servidor {datos['servidor']}",
                 loc="left", color=TINTA, fontsize=12, fontweight="bold")
    estilo(ax)
    fig.tight_layout()
    fig.savefig(d / "bloques.png", dpi=150)
    print("→", d / "bloques.png")


if __name__ == "__main__":
    d = Path(sys.argv[1] if len(sys.argv) > 1 else "docs/mediciones/m3")
    rafaga(d)
    bloques(d)
