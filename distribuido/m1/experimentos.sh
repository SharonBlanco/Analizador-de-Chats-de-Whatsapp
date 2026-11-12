#!/usr/bin/env bash
# ============================================================
# M1 · Comandos exactos de las mediciones (docs/modulo-M1-memoria.md).
# Correr desde la raíz del proyecto, con RabbitMQ arriba y distribuido/config.env completo.
#   bash distribuido/m1/experimentos.sh huella|estrategias|rafaga|oom|admision|todo
#
# Si VS Code está instalado como Flatpak, docker se llama a través de: DOCKER="flatpak-spawn --host docker"
# ============================================================
set -euo pipefail
DOCKER=${DOCKER:-docker}
RAIZ=$(pwd)
OUT=docs/mediciones/m1
HF=${HF_CACHE:-$HOME/.cache/huggingface}           # caché de modelos de Hugging Face (para no descargarlos otra vez)
set -a; source distribuido/config.env; set +a       # RABBIT_USER, RABBIT_PASS, WORKER_TOKEN
mkdir -p "$OUT"

# Monta los modelos ya descargados y las muestras del dataset (solo lectura)
MONTAJES=(-v "$HF":/tmp/modelos/hf -v "$RAIZ/yolov8l-oiv7.pt":/tmp/modelos/yolov8l-oiv7.pt:ro
          -v "$RAIZ/fuentes_dataset/imagenes_openimages/jpg":/medios/imagenes:ro
          -v "$RAIZ/fuentes_dataset/audio_fleurs/wav":/medios/audios:ro
          -v "$RAIZ/distribuido/m1":/m1:ro -v "$RAIZ/$OUT":/salida)

worker() {   # worker NOMBRE LIMITE [VARIABLES…] : un worker en Docker con límite de memoria (cgroups v2)
  local nombre=$1 limite=$2; shift 2
  $DOCKER rm -f "$nombre" >/dev/null 2>&1 || true
  $DOCKER run -d --name "$nombre" --network host --read-only --tmpfs /tmp:size=2g --user "$(id -u):$(id -g)" \
    --memory="$limite" --memory-swap="$limite" \
    -v "$HF":/tmp/modelos/hf -v "$RAIZ/yolov8l-oiv7.pt":/tmp/modelos/yolov8l-oiv7.pt:ro \
    -e RABBIT_HOST=localhost -e RABBIT_USER="$RABBIT_USER" -e RABBIT_PASS="$RABBIT_PASS" -e WORKER_TOKEN="$WORKER_TOKEN" \
    -e COORDINADOR_URL=http://localhost:8201 -e WA_PREFIJO_COLAS=wa.m1. -e WA_INTERVALO_LATIDO=1 \
    -e WORKER_ID="$nombre" -e HOST_NAME="$(hostname)" -e WA_WHISPER_MODELO=small -e HF_HUB_OFFLINE=1 -e YOLO_OFFLINE=1 "$@" wa-worker >/dev/null
  echo "worker $nombre: --memory=$limite $*"
}
parar() { $DOCKER rm -f "$@" >/dev/null 2>&1 || true; }

case ${1:-todo} in
  huella|todo)
    # 1) Huella de cada modelo: cada uno en un proceso nuevo, dentro de la imagen del worker
    $DOCKER run --rm --network host --user "$(id -u):$(id -g)" "${MONTAJES[@]}" -e PYTHONPATH=/app:/app/mensajeria \
      -e WA_YOLO_MODELO=/tmp/modelos/yolov8l-oiv7.pt wa-worker python /m1/medir_modelos.py --medios /medios --salida /salida
    cp "$OUT/huella_modelos.json" distribuido/worker/huella_modelos.json
    ;;&
  estrategias|todo)
    # 2) Un modelo por proceso vs fork + copy-on-write vs hilos (contenedor con límite holgado)
    $DOCKER run --rm --network host --user "$(id -u):$(id -g)" --memory=3g --memory-swap=3g "${MONTAJES[@]}" \
      -e PYTHONPATH=/app:/app/mensajeria wa-worker python /m1/estrategias_carga.py --n 1,2,3 --salida /salida
    ;;&
  rafaga|todo)
    # 3) Ráfaga / saturación (B.6): un worker de 2200 MB que PIDE 4 sub-tareas a la vez. Cada sub-tarea de texto
    #    necesita ~818 MB temporales: 4 a la vez no caben. El control de admisión lo limita a lo que cabe.
    worker wa-m1-rafaga 2200m -e WORKER_POOLS=texto -e WORKER_CONCURRENCIA=4
    python distribuido/m1/rafaga.py --casos 8 --bloques 4 --sin-adjuntos --workers 1 --salida "$OUT/rafaga"
    $DOCKER logs wa-m1-rafaga > "$OUT/rafaga/worker.log" 2>&1
    python distribuido/m1/graficar_memoria.py "$OUT/rafaga" --titulo "Ráfaga de 8 casos (32 sub-tareas) · --memory=2200m, concurrencia pedida 4"
    parar wa-m1-rafaga
    ;;&
  oom|todo)
    # 4) OOM provocado: límite chico y SIN control de admisión → el kernel mata al worker
    worker wa-m1-oom 700m -e WA_ADMISION=0 -e WORKER_POOLS=texto
    python distribuido/m1/rafaga.py --casos 1 --bloques 2 --sin-adjuntos --workers 1 --max-min 6 --salida "$OUT/oom"
    $DOCKER logs wa-m1-oom > "$OUT/oom/worker.log" 2>&1
    $DOCKER inspect wa-m1-oom --format '{{json .State}}' > "$OUT/oom/estado_contenedor.json"
    python distribuido/m1/graficar_memoria.py "$OUT/oom" --titulo "OOM provocado · --memory=700m sin control de admisión"
    parar wa-m1-oom
    ;;&
  admision|todo)
    # 5) Mismo límite CON control de admisión → el nodo se degrada y no colapsa
    worker wa-m1-adm 700m -e WORKER_POOLS=texto
    python distribuido/m1/rafaga.py --casos 1 --bloques 2 --sin-adjuntos --workers 1 --max-min 6 --salida "$OUT/admision"
    $DOCKER logs wa-m1-adm > "$OUT/admision/worker.log" 2>&1
    python distribuido/m1/graficar_memoria.py "$OUT/admision" --titulo "Con control de admisión · --memory=700m"
    parar wa-m1-adm
    ;;
esac
