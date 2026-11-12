# ============================================================
# Worker del analizador de chats.
# Se construye DESDE LA RAÍZ del proyecto (necesita mensajeria/ y distribuido/):
#
#   docker build -f distribuido/worker/Dockerfile -t wa-worker .
#   docker run -d --name wa-worker-2 --read-only --tmpfs /tmp \
#     --memory=3g --memory-swap=3g \                 ← M1: límite de memoria (cgroups v2)
#     -e RABBIT_HOST=192.168.1.10 -e RABBIT_USER=... -e RABBIT_PASS=... \
#     -e COORDINADOR_URL=http://192.168.1.10:8101 -e WORKER_TOKEN=... \
#     -e WORKER_ID=worker-2 -e HOST_NAME=$(hostname) \
#     -v wa_modelos:/tmp/modelos wa-worker
#
# --memory-swap igual a --memory: sin swap, al pasarse del límite el kernel mata el proceso
# (OOM) en lugar de ralentizar todo el nodo. El supervisor lo detecta y reinicia el worker.
#
# Corre sin privilegios (usuario "worker", no root) y admite --read-only: lo único
# que escribe son los adjuntos temporales en /tmp, que se borran al terminar.
# ============================================================
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --create-home --uid 10001 worker \
    && mkdir -p /tmp/modelos && chown worker /tmp/modelos   # un volumen nuevo montado aquí hereda este dueño

WORKDIR /app
COPY distribuido/worker/requirements.txt .
# PyTorch y torchvision solo CPU (~200 MB en lugar de ~2,5 GB con CUDA; las máquinas en la nube no tienen GPU).
# Los dos del mismo índice: un torchvision de PyPI (hecho para CUDA) rompe YOLO con "torchvision::nms does not exist".
RUN pip install --no-cache-dir torch==2.15.0 torchvision==0.29.2 --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir -r requirements.txt

COPY mensajeria/whatsapp_formato.py mensajeria/estadisticas.py ./mensajeria/
COPY distribuido/comun.py distribuido/worker/*.py distribuido/worker/huella_modelos.json ./

# Los modelos se descargan la primera vez a /tmp/modelos (compatible con --read-only --tmpfs /tmp).
# Para no bajarlos en cada arranque, monte un volumen ahí: -v wa_modelos:/tmp/modelos
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 HF_HOME=/tmp/modelos/hf YOLO_CONFIG_DIR=/tmp/modelos/yolo \
    WA_YOLO_MODELO=/tmp/modelos/yolov8l-oiv7.pt WORKER_DIR=/tmp
USER worker
# El supervisor arranca worker.py como proceso hijo, detecta si el kernel lo mató por memoria (OOM)
# y lo vuelve a arrancar (M1).
CMD ["python", "supervisor.py"]
