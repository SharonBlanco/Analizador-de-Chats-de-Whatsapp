# Matriz de responsabilidades

**Proyecto:** II Proyecto Programado, Alternativa B: Plataforma distribuida de análisis de datos de mensajería
**Curso:** IC-6600 Principios de Sistemas Operativos · TEC · II Semestre 2026

**Integrantes:**
- Sharon Blanco (GitHub: `SharonBlanco`)
- Britany (GitHub: `Britany`)

El responsable principal de cada componente puede explicarlo, modificarlo en vivo y defenderlo. El apoyo colaboró en su diseño, sus pruebas o su revisión.

## Módulos de Sistemas Operativos (B.5)

| Módulo | Responsable principal | Apoyo | Ruta en el repositorio | Ficha de evidencia |
|---|---|---|---|---|
| M1 · Gestión de memoria | Sharon Blanco | — | `distribuido/worker/memoria.py`, `distribuido/worker/supervisor.py`, `distribuido/worker/huella_modelos.json`, `distribuido/m1/` | [docs/modulo-M1-memoria.md](modulo-M1-memoria.md) |
| M2 · Planificación de cargas | Britany | — | `distribuido/planificador.py`, `distribuido/tests/test_planificador.py`, `distribuido/coordinador.py` (despacho) | `docs/modulo-M2-planificacion.md` |
| M3 · E/S y almacenamiento | Sharon Blanco | — | `mensajeria/almacen.py`, `mensajeria/receptor.py`, `distribuido/worker/worker.py` (`descargar_por_bloques`), `distribuido/m3/` | [docs/modulo-M3-almacenamiento.md](modulo-M3-almacenamiento.md) |
| M4 · Protección y seguridad | Britany | Sharon Blanco | `distribuido/worker/Dockerfile` (worker sin root, solo lectura), `mensajeria/web.py` (token de los workers), `distribuido/comun.py` (credenciales fuera del código, TLS con `RABBIT_URL`) | `docs/modulo-M4-seguridad.md` |
| M5 · Cómputo heterogéneo | Sharon Blanco | — | `distribuido/worker/detectores.py` (`dispositivo`, `tamano_whisper`), `distribuido/m5/` | [docs/modulo-M5-heterogeneo.md](modulo-M5-heterogeneo.md) |

## Componentes de la plataforma

| Componente | Responsable principal | Apoyo | Ruta en el repositorio | Evidencia |
|---|---|---|---|---|
| Importación de chats de WhatsApp (Android/iOS, .txt/.zip) y parser | Sharon Blanco | — | `mensajeria/importar.py`, `mensajeria/whatsapp_formato.py` | — |
| Importación desde un teléfono Android por USB | Sharon Blanco | — | `mensajeria/telefono.py`, `exportar_whatsapp.py` | — |
| Interfaz web y dashboard (panorama, riesgos, conversaciones, workers, alertas) | Sharon Blanco | — | `mensajeria/web.py`, `mensajeria/web/index.html` | — |
| Pipeline distribuido: coordinador, workers y RabbitMQ (reutilizado del I Proyecto) | Sharon Blanco | — | `distribuido/coordinador.py`, `distribuido/worker/worker.py`, `distribuido/comun.py` | [docs/reutilizacion.md](reutilizacion.md) |
| Detección de agresiones (texto con Ollama, imagen y audio) y evaluación contra el ground truth | Sharon Blanco | — | `distribuido/worker/detectores.py`, `distribuido/worker/contexto_ollama.md`, `distribuido/comparar_detectores.py` | [docs/mediciones/](mediciones/) |
| Dataset sintético y ground truth (B.3) | Sharon Blanco | — | `generador_dataset/` | [docs/dataset.md](dataset.md) |
| Workers en la nube (Google Colab + CloudAMQP con TLS) | Sharon Blanco | — | `distribuido/worker_colab.ipynb`, `distribuido/comun.py` (`RABBIT_URL`) | — |
| Pruebas de carga y tolerancia a fallos (B.6) | Sharon Blanco y Britany | — | `distribuido/m1/rafaga.py`, `distribuido/m3/rafaga_subidas.py` | Informe final |
| Informe final | Sharon Blanco y Britany | — | — | Informe final (PDF) |

