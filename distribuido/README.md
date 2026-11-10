# Procesamiento en paralelo

Divide una conversación entre varios workers (computadoras) usando RabbitMQ.
Reutiliza el flujo de tareas del I Proyecto: colas durables con prioridad, ACK
después del resultado (si un worker cae, su sub-tarea pasa a otro), reintentos
con espera creciente, cancelación real y cierre por barrier/join.

| Archivo | Qué hace |
|---|---|
| `comun.py` | Nombres de colas y configuración compartida |
| `coordinador.py` | Crea los trabajos, escucha resultados y latidos, combina y verifica. Corre dentro de `mensajeria/web.py` |
| `worker/worker.py` | Worker: bloques de texto (estadísticas parciales) y adjuntos (SHA-256 + ffprobe) |
| `worker/Dockerfile` | Imagen del worker, sin root y compatible con `--read-only` |
| `docker-compose.yml` | Solo RabbitMQ |

## 1. Configuración (una vez por computadora)

```bash
cp distribuido/config.env.ejemplo distribuido/config.env   # completar valores
pip install pika psutil
```
`config.env` no se sube a Git. Todos usan el mismo `RABBIT_USER`, `RABBIT_PASS` y `WORKER_TOKEN`.

## 2. Coordinador (PC principal)

```bash
cd distribuido && docker compose --env-file config.env up -d && cd ..   # si aún no hay RabbitMQ
python3 mensajeria/web.py          # interfaz en http://localhost:8100, adjuntos para workers en :8101
```

## 3. Workers (cada computadora)

En su `config.env`: `RABBIT_HOST=<IP del coordinador>` y `COORDINADOR_URL=http://<IP del coordinador>:8101`.
```bash
WORKER_ID=worker-2 python3 distribuido/worker/worker.py
# o con Docker, desde la raíz del proyecto:
docker build -f distribuido/worker/Dockerfile -t wa-worker .
docker run --rm --read-only --tmpfs /tmp --env-file distribuido/config.env -e WORKER_ID=worker-2 -e HOST_NAME=$(hostname) wa-worker
```

## 4. Uso

En la interfaz: conversación → **Procesamiento paralelo** → **Dividir entre workers**.
La sección **Workers** muestra las computadoras conectadas. Para demostraciones,
`WA_RETARDO_DEMO=2` hace que cada sub-tarea tarde 2 s más (no usar al medir).
