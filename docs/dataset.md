# Dataset

Cumple la sección **B.3** del enunciado: solo datos **sintéticos o públicos con licencia que permite uso académico**. No contiene conversaciones, audios ni imágenes reales de terceros.

El dataset no se guarda en el repositorio. Se **reconstruye** con dos scripts y siempre da el mismo resultado para una misma semilla:

```bash
python generador_dataset/descargar_fuentes.py          # fuentes públicas → fuentes_dataset/
python generador_dataset/generar.py                    # casos + ground truth → dataset_generado/
python generador_dataset/generar.py --importar         # además, los importa a la plataforma
```

## 1. Fuentes y licencias

| Fuente | Se usa para | Licencia | Acceso |
|---|---|---|---|
| **Mensajes sintéticos** (`generador_dataset/contenido.py`) | Conversación normal: plantillas con nombres, grupos y temas ficticios | Producidos por el equipo, con apoyo de IA (declarado en `bitacora_ia.md`) | En el repositorio |
| **[OffendES](https://huggingface.co/datasets/SINAI/OffendES)** (SINAI, Universidad de Jaén) | Mensajes **positivos** (`OFP`/`OFG` → *ofensivo*) y negativos difíciles (`NO`/`NOE`) | La ficha declara **CC BY-NC-SA 4.0** en los metadatos y **Apache-2.0** en el texto; ambas permiten uso académico | Formulario en Hugging Face; se coloca en `fuentes_dataset/texto/offendes/` |
| **[HatEval 2019](https://hatespeech.di.unito.it/hateval.html)**, parte en español (Basile et al., SemEval-2019 Task 5) | Positivos: `HS=1, AG=1` → *odio_agresivo*; `HS=1, AG=0` → *odio*. Negativos: `HS=0` | **CC BY-NC 4.0** | Formulario; se coloca en `fuentes_dataset/texto/hateval/` |
| Positivos sintéticos de respaldo (`contenido.py`: `POSITIVOS_SINTETICOS`) | Solo si faltan OffendES y HatEval: 20 frases ficticias de *amenaza* y *ofensivo* | Producidas por el equipo con IA | En el repositorio |
| **[Open Images V7](https://github.com/openimages/dataset/blob/main/READMEV2.md)** (Google), split *validation* | 300 imágenes: **60 positivas** (con armas) y 240 negativas | Anotaciones **CC BY 4.0**. Imágenes: solo las listadas como **CC BY 2.0**; autor y licencia de cada una en `dataset_generado/ATRIBUCIONES.md` | Descarga automática |
| **[FLEURS](https://huggingface.co/datasets/google/fleurs)** (Google), `es_419`, split *test* | 150 notas de voz de 1 a 8 frases leídas, con **transcripción** | **CC BY 4.0** | Descarga automática |
| **Grabaciones del equipo** (opcional) | Notas de voz **positivas**, leídas del guion `generador_dataset/guion_grabaciones.csv` | Producidas por el equipo | Se colocan en `fuentes_dataset/grabaciones/` |
| **Películas abiertas de Blender**: Elephants Dream, Tears of Steel, Big Buck Bunny y Sintel | Un video de 263 MB a 1,1 GB en cada uno de los 5 casos pesados | **CC BY** (2.5 / 3.0) | Descarga automática |
| Documentos PDF | Adjuntos de los casos heterogéneos (actas, horarios, listas) | Generados por el script, con contenido ficticio | — |

**Tratamiento de los textos públicos:** los comentarios de OffendES y HatEval son publicaciones públicas de redes sociales. Antes de insertarlos se eliminan las `@menciones`, los enlaces y los marcadores `RT`/`USER`/`URL`. Solo se usan como texto de mensajes; ningún autor original aparece en los chats.

**Conversaciones reales:** el único chat exportado de un teléfono que se usa en las pruebas lo creó el equipo, con integrantes del equipo y contenido ficticio. Se importa con la declaración de consentimiento del importador.

## 2. Construcción

`generar.py` produce cada caso como una **exportación real de WhatsApp**: una carpeta con el `.txt` y los adjuntos con sus nombres reales (`IMG-…jpg`, `PTT-…opus`, `VID-…`). Usa tres formatos para ejercitar el parser:

| Formato | Ejemplo de línea | Peso aproximado |
|---|---|---|
| Android 12 h | `10/11/2026, 9:51 a. m. - Autor: texto` (con los espacios U+202F reales) | 70 % |
| Android 24 h | `10/11/26, 21:51 - Autor: texto` | 15 % |
| iOS | `[10/11/26, 9:51:32 a. m.] Autor: texto`, archivo `_chat.txt` | 15 % |

- **Mensajes:** el tamaño de cada caso sigue una distribución log-normal, y los casos con adjuntos son más grandes. Los autores siguen una distribución de actividad tipo Pareto, y los tiempos entre mensajes mezclan ráfagas, pausas y cambios de día, con poca actividad de madrugada.
- **Positivos de texto:** 16 casos "con incidencias" llevan entre 5 % y 10 % de mensajes positivos. El resto lleva alguno aislado (0,3 %). Todos los casos llevan además un 2 % de **negativos reales** de los datasets, para que el detector no aprenda que "texto de dataset = positivo".
- **Notas de voz:** son frases de FLEURS concatenadas y codificadas en Opus mono, como las de WhatsApp. Con la semilla por defecto, su duración va de 6 s a casi 2 min (mediana de 18 s; 16 notas superan el minuto).
- **Videos:** se enlazan sin modificar. Un mismo video aparece en dos casos, a propósito, para medir la **deduplicación** del módulo M3.
- **Prioridad:** cada caso recibe una (baja 30 %, media 40 %, alta 20 %, crítica 10 %) para los experimentos del módulo M2.
- **Verificación automática:** al terminar, cada chat se vuelve a leer con el parser de la plataforma (`mensajeria/whatsapp_formato.py`), y se comprueba que cada etiqueta del ground truth apunta exactamente a su mensaje o adjunto (número de línea, id y texto o nombre de archivo).

## 3. Composición frente a los mínimos de B.3

| Elemento | Mínimo exigido | Generado |
|---|---|---|
| Casos | 40 | **40** |
| Casos heterogéneos (texto + imagen + audio + video/documento) | 10 | **12**: 5 con video y 7 con PDF |
| Mensajes de participantes | 20 000 | **20 400** |
| Imágenes, con proporción conocida de positivos | 300 | **300** (60 positivas = 20 %) |
| Notas de voz de distintas duraciones | 150 | **150**, más las grabaciones del equipo |
| Casos pesados (video o adjunto ≥ 200 MB) | 5 | **5** |
| Ground truth | una muestra | **todo** lo insertado (ver sección 4) |

Las cifras exactas de cada ejecución, como positivos por categoría, tamaño total y formatos, quedan en `dataset_generado/verdad/resumen.json`, junto con la comprobación de cada mínimo.

## 4. Ground truth (`dataset_generado/verdad/`)

| Archivo | Una fila por | Columnas principales |
|---|---|---|
| `casos.csv` | caso | tipo, formato, prioridad, `con_incidencias`, mensajes, adjuntos por tipo, positivos por tipo, MB |
| `mensajes_etiquetados.csv` | mensaje tomado de un dataset | `linea`, `id`, autor, fecha, **`etiqueta`** (positivo/negativo), **`categoria`**, fuente, `id_fuente`, etiqueta original, texto |
| `imagenes.csv` | imagen | `linea`, `id`, archivo, **`positivo`**, clases positivas (Handgun, Knife…), `image_id`, autor y licencia |
| `audios.csv` | nota de voz | `linea`, `id`, archivo, duración, **`transcripcion`** (referencia para medir el speech-to-text), `positivo`, categoría |
| `videos.csv`, `documentos.csv` | adjunto | `linea`, `id`, archivo, título y licencia |

`linea` es la línea del `.txt` donde empieza el mensaje, e `id` es su número de orden. Son los mismos valores que asigna el parser de la plataforma, así que los hallazgos del análisis se comparan directamente con el ground truth. Los mensajes que no figuran en `mensajes_etiquetados.csv` son sintéticos y, por construcción, **negativos**.

## 5. Estado de las fuentes y cifras finales

- **OffendES:** descargado (30 416 comentarios: train 16 710, validation 100, test 13 606; 4 818 ofensivos `OFP`/`OFG`).
- **HatEval:** no se usa. OffendES cubre la categoría *ofensivo*, y el detector de odio anterior (pysentimiento, hoy alternativa del detector con Ollama) ya está entrenado con HatEval, así que no se evalúa con sus mismos datos.
- **Videos:** descargados (Big Buck Bunny, Elephants Dream, Sintel y Tears of Steel, con licencia CC BY).
- **Grabaciones del equipo:** 8 notas de voz con amenazas, actividad ilícita e insultos, en `fuentes_dataset/grabaciones/` con su `grabaciones.csv`.
- **Uso de IA** en las plantillas y los positivos sintéticos: registrado en `docs/bitacora_ia.md` (entrada 4).

**Cifras finales** (`dataset_generado/verdad/resumen.json`, semilla 2026):

| Requisito B.3 | Valor | Cumple |
|---|---|---|
| Casos | 40 (29 Android 12 h, 2 Android 24 h, 9 iOS) | ✅ |
| Casos heterogéneos | ≥ 10 | ✅ |
| Mensajes de participantes | 20 400 (832 positivos y 408 negativos de datasets etiquetados) | ✅ |
| Imágenes | 300 (60 con armas) | ✅ |
| Audios | 158 (8 con agresiones: las grabaciones del equipo) | ✅ |
| Casos pesados con video (≥ 200 MB) | 5 | ✅ |
| Documentos | 7 | — |
| Tamaño total | 2,68 GB | — |
| Ground truth | `dataset_generado/verdad/` | ✅ |
