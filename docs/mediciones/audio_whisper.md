# Medición: modelo de Whisper para las notas de voz

Evidencia para **M1** (memoria por modelo) y **M5** (CPU frente a GPU, modelo cuantizado frente a no cuantizado). También justifica la política del worker de audio.

- **Fecha:** 11/11/2026
- **Equipo:** Intel i7-12700H (20 hilos; se usaron 8 para Whisper en CPU), NVIDIA RTX 3050 Laptop 4 GB, faster-whisper 1.2.1 (CTranslate2 4.9)
- **Datos:** las primeras 20 notas de voz del dataset (`dataset_generado/verdad/audios.csv`), 477 s en total. Son frases de FLEURS, leídas en español latinoamericano, con su transcripción de referencia. También una nota de voz real grabada por el equipo, de 3,5 s, con la frase de prueba "…vender drogas y asaltar a una persona".
- **Método:** `WhisperModel(tamaño, device, compute_type).transcribe(audio, language="es", beam_size=1)`, con una inferencia de calentamiento antes. El WER (tasa de error por palabra) se calcula con `coordinador.wer()`: distancia de edición entre palabras, sin tildes ni puntuación. La VRAM es la diferencia de `torch.cuda.mem_get_info()` antes y después de cargar el modelo. Mientras se medía había otro worker usando la GPU (~1,7 GB).

| Modelo | Dispositivo | Precisión | WER promedio | WER mediana | Velocidad (× tiempo real) | VRAM | Nota de voz real |
|---|---|---|---|---|---|---|---|
| small | GPU | float16 | 0.074 | 0.048 | 57.0 | 576 MB | "a saltar" ✗ |
| small | GPU | int8_float16 | 0.075 | 0.048 | 61.1 | 288 MB | "a saltar" ✗ |
| **medium** | **GPU** | **int8_float16** | 0.081 | 0.048 | 26.0 | 928 MB | **"asaltar" ✓** |
| medium | GPU | float16 | — | — | — | sin memoria (OOM) | — |
| **small** | **CPU** | **int8** | 0.076 | 0.048 | 7.7 | — | "a saltar" ✗ |
| medium | CPU | int8 | 0.085 | 0.048 | 2.3 | — | "asaltar" ✓ |

## Interpretación
- **En las lecturas de FLEURS, *medium* no mejora a *small*:** la mediana es igual (4,8 %), y el promedio lo mueven unos pocos audios difíciles. Una primera prueba con solo 6 audios sugería que *medium* era mucho mejor (WER 0.028); con 20 audios esa diferencia desapareció. Era una muestra demasiado chica.
- **En la nota de voz real**, voz espontánea y más parecida al uso del sistema, solo *medium* transcribió "asaltar". Las reglas de actividades ilícitas dependen de esa palabra. Es un solo audio: falta medirlo con más grabaciones del equipo.
- **Cuantizar (int8_float16) no empeora el error** y reduce la VRAM a la mitad (576 → 288 MB en *small*). En una GPU de 4 GB es la diferencia entre que *medium* entre o no junto a los otros dos modelos: *medium* en float16 se quedó sin memoria.
- **CPU frente a GPU:** el mismo modelo va de 7 a 11 veces más rápido en la GPU.

## Política del worker (`WA_WHISPER_MODELO=auto`)
- **Nodo con GPU → *medium* int8_float16:** 26× tiempo real alcanza de sobra, y gana en voz espontánea.
- **Nodo solo con CPU → *small* int8:** *medium* bajaría a 2,3× tiempo real; una nota de voz de 1 minuto tardaría 26 s.
- Los tres modelos del worker (texto, YOLO l y Whisper *medium*) ocupan juntos unos **1,76 GB de VRAM**.
