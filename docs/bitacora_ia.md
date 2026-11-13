# Bitácora de uso de IA

**Herramienta usada en todo el proyecto:** Claude (Anthropic), modelo Claude Opus, como asistente de programación dentro de VS Code (Claude Code).

## Cómo usamos la IA (declaración del equipo)
- **Para qué la usamos:**
  - **generar código:** el importador, la interfaz web, los workers, los detectores, los módulos M1, M2, M3, M4 y M5, y los scripts de medición;
  - **sugerir ideas:** qué componentes y herramientas convenía usar, y qué medir en cada módulo;
  - **depurar:** encontrar errores de instalación, de configuración y de ejecución.
- **Qué decidimos y validamos nosotras:**
  - **El uso de la aplicación y sus componentes:** qué hace la plataforma (importar chats, detectar agresiones en texto, imagen y audio, clasificarlas por severidad, mostrarlas en el dashboard), qué severidad tiene cada tipo de agresión, qué datos se usan y qué no (nunca chats reales de terceros).
  - **Las herramientas:** probamos las que propuso la IA y medimos su eficiencia antes de quedarnos con ellas. Por ejemplo, comparamos tamaños de YOLO y de Whisper y los umbrales del detector de texto (ver `docs/mediciones/`). La idea de cambiar el detector de texto por un modelo de lenguaje local (Ollama) fue nuestra; la IA lo implementó y lo comparó con el detector anterior, y con esos datos decidimos quedarnos con Ollama (entrada 12).
  - **Las mediciones:** definimos qué medir en cada módulo a partir de lo que pide el enunciado y de las ideas de la IA. La IA escribió el código de los scripts de medición; nosotras los ejecutamos, revisamos que los números tuvieran sentido, pedimos correcciones cuando no lo tenían (ver los errores documentados) e interpretamos los resultados.
  - **La infraestructura:** probamos otras opciones para los workers en la nube (máquinas virtuales en Azure, y evaluamos Oracle Cloud y Firebase). Por recomendación del profesor nos quedamos con **Google Colab**, y además usamos una máquina virtual de **Azure for Students** para fotos, adjuntos y audios.
- Todo el código se ejecutó y se probó antes de incorporarlo. Ninguna salida de la IA se aceptó sin correrla.

> Cada integrante registra las consultas de los componentes de los que es responsable principal (ver `docs/responsabilidades.md`).

---

## Entradas

### 1. Importación de chats de WhatsApp
- **Fecha · integrante:** semana 14 · Sharon Blanco y Britany
- **Componente:** importación y parser (`mensajeria/importar.py`, `mensajeria/whatsapp_formato.py`)
- **Propósito:** generar código y entender los formatos de exportación.
- **Consulta resumida:** "Hacer un importador para los chats exportados de WhatsApp (Android e iOS) con sus adjuntos, a partir de este ejemplo de formato."
- **Resultado obtenido:** parser para los formatos Android de 12 h y 24 h y para iOS; importador que registra el SHA-256 de cada adjunto en `metadata.json`.
- **Validado / corregido / descartado:**
  - **Validado:** el parser contra nuestros chats de prueba y los tres formatos del dataset generado.
  - **Corregido:** el importador terminaba con `sys.exit`, que no sirve dentro del servidor web; se cambió por una excepción. Los casos de iOS se importaban con el nombre genérico "chat"; se corrigió tomando el nombre del `.zip`.
- **Cómo se validó:** importando chats reales del equipo y los 40 casos generados, y revisando la cantidad de mensajes, participantes y adjuntos en la interfaz.

### 2. Interfaz web y dashboard
- **Fecha · integrante:** semana 14 · Sharon Blanco y Britany
- **Componente:** `mensajeria/web.py`, `mensajeria/web/index.html`
- **Propósito:** generar código a partir de nuestro diseño.
- **Consulta resumida:** "Rediseñar la interfaz con estilo SaaS en tonos verde agua, con estadísticas generales, búsqueda, carpetas y filtro por fechas." Después: "una pestaña de riesgos", "elegir qué chats procesar" y "eliminar chats".
- **Resultado obtenido:** interfaz de una sola página con panorama general, conversaciones, riesgos, workers y alertas.
- **Validado / corregido / descartado:**
  - **Corregido:** la tabla de workers volvía al inicio del scroll horizontal cada 2 s, porque se redibujaba completa; se cambió para conservar la posición. Una edición de la IA rompió íconos SVG (borró una comilla); lo detectamos visualmente.
- **Cómo se validó:** usando la interfaz en el navegador y con `node --check` sobre el JavaScript.

### 3. Pipeline distribuido (reutilización del I Proyecto)
- **Fecha · integrante:** semana 14 · Sharon Blanco y Britany
- **Componente:** `distribuido/coordinador.py`, `distribuido/worker/worker.py`
- **Propósito:** adaptar los workers y el flujo de tareas del I Proyecto.
- **Consulta resumida:** "Reutilizar del proyecto anterior solo los workers conectados y el flujo de tareas, y agregar un botón para dividir un chat entre workers."
- **Resultado obtenido:** coordinador con colas por tipo, reintentos, cancelación, barrera y reporte consolidado.
- **Validado / corregido / descartado:**
  - **Corregido:** una condición de carrera hacía que se perdieran eventos "inicio" (el trabajo se registraba después de publicarlo). Los workers que se conectaban tarde seguían procesando trabajos cancelados. Las dos fallas se detectaron en las pruebas de estrés y se corrigieron.
- **Cómo se validó:** 15 trabajos con workers rápidos sin eventos perdidos, caída de un worker, reinicio del coordinador y cancelación. Los resultados distribuidos se compararon contra el análisis local y dieron idénticos.

### 4. Dataset sintético y ground truth
- **Fecha · integrante:** semana 14 · Sharon Blanco y Britany
- **Componente:** `generador_dataset/`
- **Propósito:** explorar alternativas (qué datasets públicos usar) y generar datos sintéticos.
- **Consulta resumida:** "Buscar datasets públicos de texto ofensivo, imágenes con armas y audio en español, y generar 40 casos que cumplan B.3."
- **Resultado obtenido:**
  - propuesta de OffendES, Open Images V7, FLEURS y videos con licencia CC BY;
  - generador de 40 casos con ground truth;
  - un guion de frases para grabar.
- **Validado / corregido / descartado:**
  - **Descartado:** un dataset de Kaggle que propusimos nosotras; elegimos OffendES porque está en español y lo etiquetaron personas.
  - **Corregido:** la verificación de "casos heterogéneos" contaba casos pesados sin video.
  - **Agregado por el equipo:** grabamos nuestras propias notas de voz con amenazas.
- **Cómo se validó:** el resumen del generador (`cumple_b3` todo en `True`), las licencias revisadas en cada fuente y las grabaciones transcritas con Whisper.

### 5. Detección de agresiones (texto, imagen y audio)
- **Fecha · integrante:** semana 14 · Sharon Blanco y Britany
- **Componente:** `distribuido/worker/detectores.py`
- **Propósito:** explorar herramientas gratuitas, generar código y evaluar.
- **Consulta resumida:** "Detectar agresiones en texto, imágenes y audios con herramientas gratuitas. Alta: amenazas de cualquier tipo; media: agresividad, insultos, machismo y violencia verbal."
- **Resultado obtenido:** reglas de amenaza, lista de insultos, pysentimiento (odio y agresividad), YOLOv8 entrenado con Open Images (armas) y faster-whisper (audio).
- **Validado / corregido / descartado:** ver los **errores 1, 2 y 3**. Elegimos YOLOv8-l y Whisper *small*/*medium* según nuestras mediciones, no según la primera sugerencia.
- **Cómo se validó:** precisión, exhaustividad y F1 contra OffendES y el ground truth; WER contra FLEURS (`docs/mediciones/`).

### 6. Workers en la nube
- **Fecha · integrante:** semana 15 · Sharon Blanco y Britany
- **Componente:** `distribuido/worker_colab.ipynb`, `distribuido/comun.py` (`RABBIT_URL`)
- **Propósito:** explorar alternativas de infraestructura.
- **Consulta resumida:** "¿Qué herramienta gratuita usar para poner dos workers en la nube?"
- **Resultado obtenido:** primero propuso Azure for Students y Oracle Cloud con Tailscale. Después, CloudAMQP (RabbitMQ con TLS) más Google Colab, con un túnel de Cloudflare para los adjuntos.
- **Validado / corregido / descartado:**
  - **Descartado:** Azure (se nos acabaron los créditos), Oracle (requiere tarjeta) y Firebase (no admite procesos que corran todo el tiempo).
  - **Elegido:** Google Colab, por recomendación del profesor.
  - **Corregido:** el túnel no conectaba porque la red bloqueaba QUIC y TCP 7844. Lo resolvimos activando la VPN de Cloudflare (WARP).
  - **Incidente nuestro:** guardamos el notebook en GitHub con la URL de RabbitMQ, que incluye la contraseña. Lo detectamos, rotamos la credencial y desde entonces la llenamos solo en Colab.
- **Cómo se validó:** los workers de Colab aparecen activos en la vista Workers y procesan sub-tareas.

### 7. M1 · Gestión de memoria
- **Fecha · integrante:** semana 15 · Sharon Blanco
- **Componente:** M1 (`memoria.py`, `supervisor.py`, `distribuido/m1/`)
- **Propósito:** generar código e ideas de medición.
- **Consulta resumida:** "Implementar M1: límite con cgroups, huella por modelo, control de admisión, detección de OOM con reencolado y comparación de estrategias de carga."
- **Resultado obtenido:** límite con Docker `--memory`, plan de admisión con las huellas medidas, supervisor que detecta el OOM con `memory.events`, política de reencolado y scripts de medición.
- **Validado / corregido / descartado:** ver los **errores 4 y 5**.
- **Cómo se validó:**
  - el OOM provocado con `--memory=700m` y sin admisión (4 OOM, el caso falla sin bucle) frente al mismo caso con admisión (0 OOM);
  - la ráfaga de 32 sub-tareas sin OOM;
  - el gráfico de memoria;
  - `docker logs` y `memory.events` del contenedor.

### 8. M3 · E/S y almacenamiento
- **Fecha · integrante:** semana 15 · Sharon Blanco
- **Componente:** M3 (`almacen.py`, `receptor.py`, `descargar_por_bloques`)
- **Propósito:** generar código e ideas de medición.
- **Consulta resumida:** "Implementar M3: almacenamiento por SHA-256 con deduplicación, transferencia por bloques verificada, recepción asíncrona y contrapresión."
- **Resultado obtenido:** almacén con enlaces duros, manifiesto y descarga por bloques con HTTP Range, receptor con asyncio y cola acotada.
- **Validado / corregido / descartado:**
  - **Validado:** la prueba de bloque dañado (se repite solo ese bloque) y la deduplicación (el mismo chat importado dos veces da 50 %).
  - **Corregido:** ver el **error 6**.
  - **Decisión de diseño:** la contrapresión se aplicó en la recepción de archivos y no en el despacho de tareas, para no interferir con M2.
- **Cómo se validó:** `almacen.py --verificar` (SHA-256 de los 764 archivos), throughput con 5 tamaños de bloque y la ráfaga de 120 subidas con 3 políticas.

### 9. M5 · Cómputo heterogéneo
- **Fecha · integrante:** semana 15 · Sharon Blanco
- **Componente:** M5 (`distribuido/m5/medir_dispositivos.py`, `detectores.py`)
- **Propósito:** ideas de medición y código del script.
- **Consulta resumida:** "Cómo medir los modelos en CPU frente a GPU para definir la política de routing."
- **Resultado obtenido:** script que mide latencia, throughput, RAM y VRAM por modelo y dispositivo.
- **Validado / corregido / descartado:**
  - **Corregido:** ver el **error 7**.
  - **Ejecución:** la medición final la corrió Sharon y la captura está en `docs/mediciones/m5/`.
  - **Decisión:** con esos datos se definió la política de routing, que se aplica junto con el despacho de M2.
- **Cómo se validó:** comparando con las mediciones previas de Whisper (`audio_whisper.md`) y revisando que la VRAM fuera 0 en las corridas de CPU.

### 10. M2 · Planificación de cargas
- **Fecha · integrante:** semana 15 · Britany
- **Herramienta:** 
- **Componente:** M2 · Planificación de cargas
- **Propósito:** 
- **Consulta resumida:** 
- **Resultado obtenido:** 
- **Validado / corregido / descartado:** 
- **Cómo se validó:** 

### 11. M4 · Protección y seguridad
- **Fecha · integrante:** semana 15 · Britany
- **Herramienta:** 
- **Componente:** M4 · Protección y seguridad
- **Propósito:** 
- **Consulta resumida:** 
- **Resultado obtenido:** 
- **Validado / corregido / descartado:** 
- **Cómo se validó:** 

### 12. Detector de texto con un modelo de lenguaje local (Ollama)
- **Fecha · integrante:** semana 15 · Sharon Blanco
- **Componente:** `distribuido/worker/detectores.py`, `distribuido/worker/contexto_ollama.md`, `distribuido/comparar_detectores.py`
- **Propósito:** generar código y evaluar una alternativa propuesta por el equipo.
- **Consulta resumida:** "Reemplazar el clasificador de texto por Ollama corriendo en local, con un archivo de contexto que le explique qué es cada categoría. Alta: amenazas de muerte, golpizas, drogas y cosas ilegales; media: ciberbullying, machismo y maltrato verbal."
- **Resultado obtenido:** clasificación con Ollama (qwen2.5:3b) en dos pasadas, contexto con las categorías, la severidad y jerga de Costa Rica, instalación de Ollama en Colab y en Azure, y un script que compara los dos detectores con los mismos mensajes.
- **Validado / corregido / descartado:**
  - **Corregido:** ver los **errores 8, 9 y 10**. Los falsos positivos los encontramos revisando los hallazgos en el dashboard.
  - **Severidad:** la definimos nosotras; como el modelo no la respetaba en todas las categorías, quedó fija para ilícito (alta), odio e insulto (media), y el modelo decide solo en amenaza y agresión.
  - **Decisión:** con la comparación (`docs/mediciones/texto_detector.md`) vimos que Ollama da muchas menos falsas alarmas pero encuentra menos ofensas leves y es más lento. Le pedimos a la IA unir los dos detectores y medir dos variantes; elegimos la que juzgamos mejor (RoBERTuito y Ollama proponen, Ollama revisa), que los datos confirmaron: mejor F1 en OffendES de las cuatro opciones. El detector anterior queda como alternativa.
- **Cómo se validó:** precisión, exhaustividad y F1 de los dos detectores con los mismos mensajes (chats sintéticos con ground truth y una muestra de OffendES), más frases de prueba escritas por nosotras.

---

## Errores e imprecisiones de la IA detectados por el equipo

| # | Componente | Qué hizo mal la IA | Cómo lo detectamos | Cómo se resolvió |
|---|---|---|---|---|
| 1 | Detección de texto | La lista de insultos incluía "basura", "cerdo" y "cerda", palabras de uso cotidiano. | Al procesar el dataset generado, **73 de 77 falsas alarmas** venían de frases como "¿quién saca la basura?". | Se quitaron esas palabras. La precisión subió de 0,43 a 0,91–0,96 (`docs/mediciones/texto_detector.md`). |
| 2 | Detección de texto | La regla de amenazas solo detectaba amenazas dirigidas con "te" ("te voy a matar"). | Escribimos "voy a matar a alguien" en un chat de prueba y **no generó alerta**. | Se amplió la regla. La versión siguiente marcaba "me voy a matar estudiando" y "la mató" (narración), y también se corrigió. Falsas alarmas en OffendES: de 31 a 11 de 11 055. |
| 3 | Audio (Whisper) | Afirmó que Whisper *medium* cometía unas 3 veces menos errores que *small*, con solo 6 audios. | Al medir con 20 audios, *medium* **no era mejor** en FLEURS. | Se corrigió la conclusión: *medium* solo en GPU (mejor en voz espontánea) y *small* en CPU. Además, ninguno de los dos transcribía bien "asaltar" en nuestras grabaciones ("a saltar"), así que se agregó una regla tolerante a ese error. |
| 4 | M1 (Docker) | El código del worker calculaba mal su ruta dentro del contenedor (`parents[1]` de `/app`) y **no arrancaba en Docker**. La imagen tenía un `torchvision` incompatible que rompía YOLO. Faltaba `.dockerignore`, así que la imagen habría incluido **nuestros chats reales**. | Al correr las mediciones de M1 en Docker: el contenedor se caía (`IndexError`) y YOLO fallaba (`torchvision::nms does not exist`). | Se corrigió la ruta, se instalaron `torch` y `torchvision` del mismo índice de CPU con versiones fijas, y se creó un `.dockerignore` que solo deja pasar el código. |
| 5 | M1 (medición) | La primera tabla de huellas decía que cargar el modelo de texto solo sumaba 32 MB, cuando en realidad usa ~940 MB. | El número no tenía sentido frente al tamaño del modelo (416 MB en disco). | Causa: los pesos se cargan con `mmap` y entran a la RAM recién en la primera inferencia (paginación por demanda). Se midieron dos inferencias para separar la memoria fija de la de trabajo. |
| 6 | M3 (contrapresión) | La primera ráfaga fue demasiado liviana para mostrar diferencias, y el `Retry-After` pedía esperar 6 s cuando el lugar se liberaba en 0,1 s. Por eso "rechazar" salía como la peor política. | Los tiempos de la primera corrida no mostraban saturación y la política con límite era más lenta sin razón. | El `Retry-After` se calcula ahora con el tiempo medido de importación. La prueba se repitió con 120 clientes y chats con adjuntos. |
| 7 | M5 (medición) | El script medía el texto "en CPU", pero pysentimiento **usaba la GPU igual**. CPU y GPU daban lo mismo (1,0×). | La fila "CPU" mostraba **688 MB de VRAM** usados. | Se oculta la GPU en las corridas de CPU (`CUDA_VISIBLE_DEVICES=""`). También se corrigió el worker: un nodo configurado como "solo CPU" ahora no toca la GPU. Con eso, el texto da 5,2× a favor de la GPU. |
| 8 | Detector de texto (Ollama) | La primera versión clasificaba 25 mensajes juntos en una sola pasada y marcaba mensajes cotidianos: "Se fue el internet otra vez" y "Necesito la factura de ayer" como **actividad ilícita**. | Los vimos en los hallazgos del dashboard, con motivos sin sentido ("Referencia a desconexión de internet"). | Se pasó a dos pasadas: una rápida por lotes y una revisión individual de cada mensaje marcado, en la que el modelo explica primero qué dice el mensaje y después decide. Esos mensajes ahora salen normales. |
| 9 | Detector de texto (Ollama) | En la revisión se le pedía al modelo un campo sí/no ("¿hay agresión?"), y se contradecía: explicaba "es un insulto dirigido a alguien" y respondía *no*. También descartaba drogas porque "no son agresión". Además, al quitar las reglas se dejaban de detectar casos obvios ("tengo un kilo de coca para vender"). | Al repetir las frases de prueba, las detecciones cambiaban de una versión a otra. | Se decide por la categoría que elige después de explicar, y las reglas y la lista de insultos volvieron como **candidatos** que el modelo revisa (sin las groserías de muletilla). El resultado quedó idéntico en dos corridas seguidas. |
| 10 | Detector de texto (comparación) | La IA presentó primero el F1 de 0,91 de Ollama en las frases de prueba como mejora, pero esas mismas frases se habían usado para ajustar su contexto. | Al comparar con conjuntos que ningún detector había visto, el anterior ganaba en F1 (0,83 frente a 0,64 en los chats sintéticos). | En la documentación se marca ese resultado como optimista y la decisión se justifica con precisión y falsas alarmas, no con ese F1 (`docs/mediciones/texto_detector.md`). |
