"""M2: comparación reproducible de FIFO, prioridad y aging.

Simulación de eventos discretos usando el Planificador real del proyecto.
No requiere RabbitMQ. Produce un CSV para la ficha de M2.

Ejecutar desde la raíz:
    python distribuido/experimentos/m2.py
"""
from __future__ import annotations

import csv
import sys
from collections import defaultdict
from pathlib import Path

DISTRIBUIDO = Path(__file__).resolve().parents[1]
if str(DISTRIBUIDO) not in sys.path:
    sys.path.insert(0, str(DISTRIBUIDO))

from planificador import Planificador, Tarea

POLITICAS = ("fifo", "prioridad", "aging")
N_CRITICAS = 60
INTERVALO_LLEGADA = 1.0
DURACION_TAREA = 2.0
INTERVALO_AGING = 10.0
SALIDA = Path(__file__).resolve().parent / "resultados_m2.csv"


def ejecutar(politica: str) -> list[dict]:
    plan = Planificador(politica=politica, intervalo_aging=INTERVALO_AGING)
    # La tarea baja llega antes que las críticas. A partir de t=0
    # llegan críticas continuamente a intervalos de 1 segundo.
    llegadas = [("baja-001", 1, 0.0)]
    llegadas += [
        (f"critica-{i + 1:03d}", 4, i * INTERVALO_LLEGADA)
        for i in range(N_CRITICAS)
    ]
    llegadas.sort(key=lambda x: x[2])  # sort estable: baja primero en t=0

    filas = []
    indice = 0
    reloj = 0.0

    while indice < len(llegadas) or plan.pendientes():
        if plan.pendientes() == 0 and indice < len(llegadas):
            reloj = max(reloj, llegadas[indice][2])

        while indice < len(llegadas) and llegadas[indice][2] <= reloj:
            nombre, nivel, instante = llegadas[indice]
            plan.agregar(Tarea(nombre, nivel, llegada=instante))
            indice += 1

        tarea = plan.siguiente(ahora=reloj)
        if tarea is None:
            continue

        inicio = reloj
        fin = inicio + DURACION_TAREA
        filas.append({
            "politica": politica,
            "tarea": tarea.id,
            "prioridad": "baja" if tarea.prioridad == 1 else "critica",
            "llegada_s": round(tarea.llegada, 3),
            "inicio_s": round(inicio, 3),
            "espera_s": round(inicio - tarea.llegada, 3),
            "fin_s": round(fin, 3),
        })
        reloj = fin

    return filas


def main() -> None:
    registros = [fila for politica in POLITICAS for fila in ejecutar(politica)]
    SALIDA.parent.mkdir(parents=True, exist_ok=True)
    campos = ["politica", "tarea", "prioridad", "llegada_s", "inicio_s", "espera_s", "fin_s"]
    with SALIDA.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=campos)
        writer.writeheader()
        writer.writerows(registros)

    por_politica = defaultdict(list)
    for fila in registros:
        por_politica[fila["politica"]].append(fila)

    print("POLÍTICA  | ESPERA BAJA (s) | ESPERA PROMEDIO (s) | ESPERA MÁXIMA (s) | TOTAL")
    print("-" * 79)
    for politica in POLITICAS:
        filas = por_politica[politica]
        baja = next(f for f in filas if f["tarea"] == "baja-001")
        media = sum(f["espera_s"] for f in filas) / len(filas)
        maxima = max(f["espera_s"] for f in filas)
        print(f"{politica:<9} | {baja['espera_s']:>14.1f} | {media:>18.2f} | "
              f"{maxima:>17.1f} | {len(filas):>5}")
    print(f"\nCSV: {SALIDA}")
    print("Nota: tiempos simulados, no mediciones de RabbitMQ ni de workers reales.")


if __name__ == "__main__":
    main()
