# ============================================================
# Worker del analizador de chats.
# Se construye DESDE LA RAÍZ del proyecto (necesita mensajeria/ y distribuido/):
#
#   docker build -f distribuido/worker/Dockerfile -t wa-worker .
#   docker run --rm --read-only --tmpfs /tmp \
#     -e RABBIT_HOST=192.168.1.10 -e RABBIT_USER=... -e RABBIT_PASS=... \
#     -e COORDINADOR_URL=http://192.168.1.10:8101 -e WORKER_TOKEN=... \
#     -e WORKER_ID=worker-2 -e HOST_NAME=$(hostname) wa-worker
#
# Corre sin privilegios (usuario "worker", no root) y admite --read-only: lo único
# que escribe son los adjuntos temporales en /tmp, que se borran al terminar.
# ============================================================
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 worker

WORKDIR /app
COPY distribuido/worker/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY mensajeria/whatsapp_formato.py mensajeria/estadisticas.py ./mensajeria/
COPY distribuido/comun.py distribuido/worker/worker.py distribuido/worker/detectores.py ./

# Los modelos se descargan la primera vez a /tmp/modelos (compatible con --read-only --tmpfs /tmp).
# Para no bajarlos en cada arranque, monte un volumen ahí: -v wa_modelos:/tmp/modelos
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HF_HOME=/tmp/modelos/hf YOLO_CONFIG_DIR=/tmp/modelos/yolo \
    WA_YOLO_MODELO=/tmp/modelos/yolov8l-oiv7.pt
USER worker
CMD ["python", "worker.py"]
