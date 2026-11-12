# Declaración de reutilización del I Proyecto Programado

Según la sección **B.2** del enunciado, este documento indica qué se reutiliza del I Proyecto, qué se modificó y qué es nuevo.

- **I Proyecto:** plataforma distribuida de procesamiento multimedia por casos, repositorio [`SharonBlanco/Proyecto`](https://github.com/SharonBlanco/Proyecto). Archivos de referencia: `coordinador/app.py`, `worker/worker.py`, `docker-compose.yml` e `init.sql`.
- **II Proyecto:** analizador de chats de WhatsApp, repositorio [`SharonBlanco/Analizador-de-Chats-de-Whatsapp`](https://github.com/SharonBlanco/Analizador-de-Chats-de-Whatsapp).

**Resumen:** del I Proyecto solo se reutiliza la **infraestructura de distribución de tareas**: la conexión de los workers con RabbitMQ y el flujo de una sub-tarea, desde que se encola hasta que se cierra el caso. No se reutiliza nada del procesamiento multimedia, del dashboard, de la base de datos ni del cliente de carga. El código reutilizado se reescribió para el nuevo dominio y vive en la carpeta `distribuido/`.

---

## 1. Qué se reutiliza

Son mecanismos que se conservan con la misma lógica. Se adaptaron los nombres, el dominio y el formato de los mensajes.

| Mecanismo | Origen en el I Proyecto | Dónde está ahora |
|---|---|---|
| Colas durables con prioridad (`x-max-priority: 10`) y mensajes persistentes (`delivery_mode=2`) | `coordinador/app.py`: `declare_queues`, `publish_subtask`; `worker/worker.py`: `QUEUE_ARGS` | `distribuido/comun.py`: `ARGS_COLA`, `declarar`; `distribuido/coordinador.py`: `_publicar` |
| Una cola por tipo de carga (*pools*) y workers que eligen qué pools atienden (`WORKER_POOLS`) | `worker/worker.py`: `ALL_POOLS`, `POOL_QUEUES` | `distribuido/comun.py`: `POOLS`; `distribuido/worker/worker.py`: `MIS_POOLS` |
| Consumo con un hilo por sub-tarea: el hilo principal solo atiende la conexión y los hilos publican con `add_callback_threadsafe`, porque pika no es *thread-safe* | `worker/worker.py`: `handle_message`, `on_message`, `start_consuming` | `distribuido/worker/worker.py`: `manejar`, `al_mensaje`, `consumir` |
| **ACK después de publicar el resultado**: si el worker muere a mitad, RabbitMQ re-entrega la sub-tarea a otro | `worker/worker.py`: `finish()` dentro de `handle_message` | `distribuido/worker/worker.py`: `publicar(..., ack=True)` |
| Límite de sub-tareas simultáneas por worker con `basic_qos(prefetch_count, global_qos=True)` | `worker/worker.py`: `start_consuming` | `distribuido/worker/worker.py`: `consumir` (`WORKER_CONCURRENCIA`) |
| Bucle `process_data_events(time_limit=1)` en lugar de `start_consuming()`, y reconexión automática cada 5 s | `worker/worker.py`: `start_consuming` | `distribuido/worker/worker.py`: `consumir` |
| Cola de resultados que escucha el coordinador en un hilo propio, con reconexión | `coordinador/app.py`: `listen_results` | `distribuido/coordinador.py`: `_escuchar`, `_al_resultado` |
| Mensaje de inicio de una sub-tarea, y detección de **reasignación** cuando la reporta un worker distinto al que la tenía | `coordinador/app.py`: `handle_worker_message` (estado `assigned`, `reassignments`) | `distribuido/coordinador.py`: `_procesar` (evento `inicio`, `reasignaciones`) |
| Resultados duplicados por re-entrega: se ignoran si la sub-tarea ya estaba en un estado final | `coordinador/app.py`: `handle_worker_message` (`FINAL_STATES`) | `distribuido/coordinador.py`: `_procesar` (`FINALES_ST`) |
| Reintentos de errores transitorios: el worker marca `retryable`, el coordinador reencola con `threading.Timer` y una espera que crece con cada intento, hasta un máximo | `coordinador/app.py`: `MAX_RETRIES`, `RETRY_DELAY`, `republish_subtask` | `distribuido/coordinador.py`: `_procesar`, `_reencolar`; `MAX_REINTENTOS` y `ESPERA_REINTENTO` en `comun.py` |
| **Barrier/join**: el caso se cierra solo cuando todas sus sub-tareas terminaron, como `completed`, `partially_completed` o `failed` | `coordinador/app.py`: `refresh_case_status` | `distribuido/coordinador.py`: `_barrera` |
| Datos de la máquina de cada worker (CPU, núcleos, RAM, SO, Docker) y latido periódico con uso de CPU y RAM | `worker/worker.py`: `machine_info`, `heartbeat_loop` | `distribuido/worker/worker.py`: `maquina`, `latidos` |
| Imagen Docker del worker basada en `python:3.11-slim` con FFmpeg instalado | `worker/Dockerfile` | `distribuido/worker/Dockerfile` |
| Lectura de duración con `ffprobe` | `worker/worker.py`: `probe_duration` | `distribuido/worker/worker.py`: `metadatos` (ampliado) |

## 2. Qué se modificó

| Aspecto | I Proyecto | II Proyecto | Motivo |
|---|---|---|---|
| Qué procesa un worker | Conversión de video y audio, miniaturas y metadatos musicales | **Bloques de mensajes** de un chat (estadísticas parciales) y **adjuntos** (recálculo de SHA-256 y metadatos) | Nuevo dominio: análisis de conversaciones |
| Pools | `video`, `audio`, `ligera` | `texto`, `adjuntos`, con el prefijo `wa.` para no mezclarse con las colas del I Proyecto | Nuevo tipo de carga |
| Persistencia del estado | PostgreSQL (`init.sql`: tablas `cases`, `subtasks`, `workers`, `resource_logs`) | Un archivo JSON por trabajo en `<dataset>/.trabajos/`, con escritura atómica (`os.replace`) | Menos infraestructura; el estado de un trabajo es pequeño y alcanza para que un reinicio del coordinador no pierda ni duplique sub-tareas |
| Transporte de los latidos | `POST /api/workers/heartbeat` (HTTP) | *Exchange* `fanout` de RabbitMQ (`wa.latidos`) | El worker ya no necesita alcanzar el servidor HTTP del coordinador para reportarse |
| Cancelación | El worker se enteraba solo **al descargar** el archivo (HTTP 410); lo que ya estaba en proceso continuaba | *Exchange* `fanout` de control (`wa.control`). El worker revisa la cancelación **durante** el procesamiento y la descarga, aborta y borra sus temporales. Si un worker se conecta después del aviso, el coordinador se lo repite | Cancelación real con liberación de recursos (B.4 y B.6) |
| Descarga de archivos | `GET /api/files/...` **sin autenticación**, en el mismo puerto que el dashboard | Puerto separado (8101) con **token compartido** (`hmac.compare_digest`). Solo entrega los adjuntos registrados del caso, nunca el chat. La interfaz escucha únicamente en `127.0.0.1` | Protección de datos sensibles (M4) |
| Credenciales | Escritas en el código y en `docker-compose.yml` (`admin` / `admin123`) | Variables de entorno o `distribuido/config.env`, excluido de Git, con plantilla en `config.env.ejemplo`. El compose las toma con `--env-file` | "Nunca llaves en el código" (M4) |
| Usuario del contenedor del worker | root | Usuario sin privilegios (`worker`, uid 10001). Funciona con `--read-only --tmpfs /tmp` | Workers sin privilegios (M4) |
| Máximo de reintentos y espera | 2 reintentos, 5 s × intento, fijos en el código | 3 reintentos, 2 s × intento, configurables | Ajuste para cargas cortas |
| Coordinador | Aplicación FastAPI independiente | Módulo dentro del servidor de la interfaz (`mensajeria/web.py`), sin dependencias web externas | Una sola aplicación para importar, analizar y distribuir |

## 3. Qué no se reutiliza

Se descartó del I Proyecto:

- Todo el procesamiento multimedia: conversión con FFmpeg, extracción de audio, miniaturas y elección de portada.
- Las consultas a MusicBrainz, iTunes y letras de canciones.
- El dashboard FastAPI, los reportes HTML y la API de casos.
- PostgreSQL y `init.sql`.
- La pausa y reanudación de casos, y la pausa automática por saturación de CPU (`CPU_HIGH`/`CPU_LOW`).
- El cliente de carga `cliente/cliente.py` y los scripts de generación del dataset multimedia (`scripts/`).
- La subida de resultados por HTTP (`upload_result`) y el reporte de progreso (`report_progress`).

## 4. Qué es nuevo

| Componente | Archivos |
|---|---|
| Parser del formato de exportación de WhatsApp (Android e iOS, 12 h y 24 h, adjuntos, mensajes del sistema, id por mensaje) | `mensajeria/whatsapp_formato.py` |
| Importador de exportaciones (`.zip`, `.txt` o carpeta), con SHA-256 de cada archivo y declaración de consentimiento (B.3) | `mensajeria/importar.py` |
| Exportación automática desde un teléfono Android por ADB | `exportar_whatsapp.py` |
| Análisis descriptivo con pandas y gráficos | `AnalyzeWhatsApp.py` |
| Estadísticas divididas en `parcial()` (un bloque) y `combinar()` (*join*). El análisis local y el distribuido usan el mismo código | `mensajeria/estadisticas.py` |
| Verificación de la división: al cerrar un trabajo se compara el resultado combinado con el análisis del chat completo, sección por sección | `distribuido/coordinador.py`: `_consolidar` |
| Verificación de integridad de adjuntos: el worker recalcula el SHA-256 y lo compara con el registrado al importar | `distribuido/worker/worker.py`: `procesar_adjunto` |
| Interfaz web: importación, conversaciones, carpetas, búsqueda por texto y fecha, panorama general, estadísticas, workers y procesamiento paralelo | `mensajeria/web.py`, `mensajeria/web/index.html`, `mensajeria/organizacion.py` |

## 5. Deficiencias del I Proyecto corregidas

Quedan corregidas estas debilidades del código anterior:

1. **Credenciales en el código:** se movieron a configuración externa (ver sección 2).
2. **Archivos accesibles sin autenticación:** ahora requieren token y se limitan a los adjuntos registrados.
3. **Cancelación incompleta:** antes solo se detectaba al descargar; ahora alcanza a las sub-tareas en curso y libera sus recursos.
4. **Worker ejecutándose como root:** ahora corre sin privilegios.
