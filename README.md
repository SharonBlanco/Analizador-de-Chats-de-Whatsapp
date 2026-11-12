# Analizador de chats de WhatsApp
### Plataforma distribuida de análisis de datos de mensajería
**IC-6600 Principios de Sistemas Operativos · TEC · II Semestre 2026 · II Proyecto Programado, Alternativa B**

La plataforma importa conversaciones exportadas de WhatsApp (texto, fotos y notas de voz), las divide en sub-tareas y las reparte entre **workers en varias computadoras** por medio de RabbitMQ. Cada worker calcula estadísticas y **detecta agresiones**: amenazas, actividades ilícitas, armas, odio o machismo e insultos. Cuando terminan todas las sub-tareas de un caso, el coordinador arma un **reporte consolidado** con alertas, y un **dashboard web** muestra los hallazgos, los riesgos por conversación y el estado de cada worker.

> **Datos:** el dataset de evaluación es **sintético o público** (OffendES, Open Images, FLEURS, películas Blender). Los chats reales no se suben al repositorio (`dataset_whatsapp/` está en `.gitignore`) y los experimentos usan solo el dataset generado.

---

## Qué hace

| Función | Detalle |
|---|---|
| **Importar chats** | Exportaciones de Android (12 h y 24 h) e iOS, en `.txt` o `.zip`, con o sin archivos adjuntos. Se pueden subir desde la web, pegar el texto o **importar directo desde un teléfono Android por USB** (ADB). Cada adjunto queda registrado con su SHA-256. |
| **Dashboard** | Panorama general de todos los chats, búsqueda, filtros por fecha, carpetas, estadísticas por chat (participantes, horarios, palabras, tiempos de respuesta) y una pestaña global de **Riesgos** con un enlace a cada mensaje marcado. |
| **Procesamiento distribuido** | Divide cada chat en bloques más una sub-tarea por adjunto. Usa colas con prioridad, ACK después del resultado, reintentos con espera creciente, cancelación y cierre por barrera (*barrier/join*). Los workers mandan latidos con su CPU y memoria. El estado de cada trabajo se guarda en disco y sobrevive a un reinicio del coordinador. |
| **Detección (gratuita y local)** | **Texto:** reglas de amenaza y de actividad ilícita, lista de insultos y el modelo pysentimiento/RoBERTuito (odio y agresividad). **Imagen:** YOLOv8 entrenado con Open Images V7 (armas). **Audio:** faster-whisper transcribe y el texto pasa por el mismo detector. Cada hallazgo indica por qué se marcó y su severidad (alta: amenazas, armas y actividades ilícitas; media: agresión, odio e insultos). |
| **Evaluación** | Precisión, exhaustividad, F1 y WER contra el *ground truth* del dataset generado. |

## Arquitectura

```
 Navegador ──► mensajeria/web.py (127.0.0.1:8100) ── interfaz, importación, dashboard
                     │
                     ├─ distribuido/coordinador.py   divide en sub-tareas, reintentos, barrera, reporte
                     │      └─ distribuido/planificador.py   prioridades + aging (M2)
                     ├─ puerto 8101 (con token)       los workers descargan los adjuntos
                     ▼
               RabbitMQ (Docker) ── colas por pool: texto · imagen · audio · adjuntos
                     │                 resultados ◄── latidos (fanout) · control/cancelación (fanout)
        ┌────────────┼────────────┐
        ▼            ▼            ▼
    worker 1      worker 2     worker 3     distribuido/worker/  (Python o Docker con --memory)
    supervisor.py → worker.py → detectores.py · memoria.py (admisión M1)
```

## Módulos de Sistemas Operativos

| Módulo | Estado | Dónde |
|---|---|---|
| **M1 · Gestión de memoria** | Implementado y medido: límite con cgroups v2, huella por modelo, control de admisión, detección de OOM con reencolado y comparación entre procesos, fork con copy-on-write e hilos. | [docs/modulo-M1-memoria.md](docs/modulo-M1-memoria.md), `distribuido/worker/memoria.py`, `distribuido/worker/supervisor.py`, `distribuido/m1/` |
| **M2 · Planificación** | Planificador con 4 niveles de prioridad y políticas FIFO, prioridad y aging (`WA_POLITICA`), con pruebas unitarias. | `distribuido/planificador.py`, `distribuido/tests/` |
| **M3 · E/S y almacenamiento** | Pendiente. | — |
| **M4 · Seguridad** | Pendiente. Ya hay: credenciales fuera del código, token para los adjuntos, workers sin root y contenedor de solo lectura. | — |
| **M5 · Cómputo heterogéneo** | Mediciones de CPU frente a GPU hechas; falta la política de *routing* y la ficha. | [docs/mediciones/](docs/mediciones/) |

## Puesta en marcha

La guía completa, con Windows, el dataset y problemas frecuentes, está en **[docs/guia_equipo.md](docs/guia_equipo.md)**. Resumen para Linux:

```bash
# 1. Entorno (Python 3.12 o superior, ffmpeg)
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# 2. Credenciales (nunca en el código)
cp distribuido/config.env.ejemplo distribuido/config.env   # completar RABBIT_USER, RABBIT_PASS y WORKER_TOKEN

# 3. RabbitMQ
cd distribuido && docker compose --env-file config.env up -d && cd ..

# 4. Aplicación web + coordinador → http://localhost:8100
python mensajeria/web.py

# 5. Uno o más workers (en esta u otras computadoras)
python distribuido/worker/worker.py
```

### Worker en Docker (con límite de memoria, M1)
```bash
docker build -f distribuido/worker/Dockerfile -t wa-worker .
docker run -d --name wa-worker-2 --read-only --tmpfs /tmp --memory=3g --memory-swap=3g \
  -e RABBIT_HOST=<IP del coordinador> -e RABBIT_USER=... -e RABBIT_PASS=... -e WORKER_TOKEN=... \
  -e COORDINADOR_URL=http://<IP del coordinador>:8101 -e WORKER_ID=worker-2 \
  -v wa_modelos:/tmp/modelos wa-worker
```
Para saber cuántos workers caben en un nodo: `python distribuido/worker/memoria.py --nodo 12g`.

### Variables útiles
| Variable | Para qué |
|---|---|
| `WORKER_POOLS` | Qué colas atiende el worker (`texto,imagen,audio,adjuntos`) |
| `WORKER_CONCURRENCIA` | Sub-tareas simultáneas que se piden (la admisión de memoria puede bajarlas) |
| `WA_DETECCION=0` | Worker sin modelos: solo estadísticas y verificación |
| `WA_DISPOSITIVO` | `auto`, `cpu` o `cuda` |
| `WA_WHISPER_MODELO` | `auto` (medium en GPU, small en CPU) o un tamaño fijo |
| `WA_ADMISION=0` | Desactiva el control de admisión (solo para provocar OOM en pruebas) |
| `WA_POLITICA` | Política del planificador: `fifo`, `prioridad` o `aging` |

## Dataset
```bash
python generador_dataset/descargar_fuentes.py      # imágenes, audios y videos con licencia libre
python generador_dataset/descargar_offendes.py     # comentarios ofensivos (requiere cuenta de Hugging Face)
python generador_dataset/generar.py --importar     # 40 casos sintéticos + ground truth
```
Las fuentes, las licencias y la composición del dataset están en [docs/dataset.md](docs/dataset.md).

## Pruebas
```bash
cd distribuido && python -m unittest discover -s tests          # planificador (M2)
bash distribuido/m1/experimentos.sh todo                         # mediciones de M1 (necesita Docker)
```

## Estructura
```
mensajeria/            importador, parser de WhatsApp, estadísticas, interfaz web, importación por USB
distribuido/           coordinador, planificador (M2), configuración y colas compartidas
  worker/              worker, supervisor, detectores, memoria (M1), Dockerfile
  m1/                  scripts de medición de memoria
  tests/               pruebas unitarias
generador_dataset/     descarga de fuentes y generador del dataset sintético
docs/                  fichas de evidencia, mediciones, dataset, reutilización, guía del equipo
AnalyzeWhatsApp.py     análisis individual con pandas (gráficos)
exportar_whatsapp.py   exportación automática desde un teléfono Android (uiautomator2)
```

## Documentación
| Documento | Contenido |
|---|---|
| [docs/guia_equipo.md](docs/guia_equipo.md) | Instalación paso a paso y problemas frecuentes |
| [docs/modulo-M1-memoria.md](docs/modulo-M1-memoria.md) | Ficha de evidencia de M1 |
| [docs/mediciones/](docs/mediciones/) | Mediciones de los detectores (texto, YOLO, Whisper) y de M1 |
| [docs/dataset.md](docs/dataset.md) | Dataset: fuentes, licencias y ground truth |
| [docs/reutilizacion.md](docs/reutilizacion.md) | Qué se reutilizó del I Proyecto |
| `docs/responsabilidades.md` · `docs/bitacora_ia.md` | *Pendientes* |

## Equipo
- **Sharon Blanco**: importación, dashboard, procesamiento distribuido, detección, dataset y M1.
- **Britany**: M2 (planificación) y M4 (seguridad).

La matriz de responsabilidades completa va en `docs/responsabilidades.md`.
