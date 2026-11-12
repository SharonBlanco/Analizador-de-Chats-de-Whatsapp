#!/usr/bin/env bash
# M3 · Comandos exactos de las mediciones (docs/modulo-M3-almacenamiento.md). Desde la raíz del proyecto, con el venv.
set -euo pipefail
OUT=docs/mediciones/m3
# 1) Almacén por contenido: migración de los casos importados, verificación y tasa de deduplicación
python mensajeria/almacen.py --dataset dataset_whatsapp --migrar --verificar | tee "$OUT/deduplicacion.txt"
# 2) Throughput según el tamaño de bloque (5 tamaños × 1 y 4 conexiones × 3 repeticiones)
python distribuido/m3/medir_bloques.py --caso 040_fernanda_mora --salida "$OUT"
# 3) Ráfaga de 120 subidas simultáneas con las tres políticas de contrapresión
python distribuido/m3/rafaga_subidas.py --clientes 120 --salida "$OUT"
# 4) Gráficos
python distribuido/m3/graficar.py "$OUT"
