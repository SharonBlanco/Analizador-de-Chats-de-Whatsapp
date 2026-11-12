# Medición: detector de agresiones en texto

Evaluación independiente del detector de texto de los workers (`distribuido/worker/detectores.py`). También es evidencia de memoria y tiempo para **M1** y **M5**.

- **Fecha:** 10/11/2026
- **Equipo:** Intel i7-12700H, NVIDIA RTX 3050 Laptop (4 GB), PyTorch 2.15 + CUDA, pysentimiento 0.7.3
- **Datos:** OffendES, split *test* (`fuentes_dataset/texto/offendes/test.csv`): 13 606 comentarios en español etiquetados por personas, de los cuales 2 551 son ofensivos (`OFP` u `OFG`).
- **Positivo esperado:** `OFP` u `OFG`. **Positivo predicho:** al menos un hallazgo del detector.
- **Variantes:**
  - *reglas + insultos*: las expresiones regulares de amenaza y la lista de insultos, sin modelo;
  - *modelo*: pysentimiento `hate_speech` (RoBERTuito entrenado con HatEval), positivo si `aggressive` u `hateful` ≥ umbral;
  - *combinado*: cualquiera de las dos.
- **Método:** `analizar_textos(textos, usar_modelo=False)` para las reglas y `modelo.predict(textos)` en lote para el modelo, en una sola pasada, con el modelo ya cargado.

| Variante | Precisión | Exhaustividad | F1 | ms/mensaje |
|---|---|---|---|---|
| Reglas + insultos | 0.53 | 0.39 | 0.45 | 0.06 |
| Modelo (umbral 0.5) | 0.61 | 0.38 | 0.47 | 6.36 (GPU) |
| **Combinado (umbral 0.5)** | **0.53** | **0.64** | **0.58** | 6.42 (GPU) |
| Modelo (umbral 0.3) | 0.51 | 0.43 | 0.46 | 6.36 (GPU) |
| Combinado (umbral 0.3) | 0.48 | 0.67 | 0.56 | 6.42 (GPU) |

Medido con la lista de insultos final, sin "basura" ni "cerdo/cerda" (ver abajo).

**Memoria:** cargar el modelo agrega unos 536 MB de RSS al proceso (de 742 a 1 278 MB). El pico de VRAM fue de 548 MB.

## Interpretación
- **Cada técnica detecta cosas distintas.** Las reglas y la lista encuentran insultos explícitos que el modelo deja pasar; el modelo detecta odio y agresividad dichos sin palabras groseras, por ejemplo discurso machista. Combinadas suben la exhaustividad de 0.38–0.39 a 0.64.
- **El modelo se entrenó con HatEval** (odio hacia mujeres e inmigrantes), un concepto más estrecho que "ofensivo" en OffendES. Eso explica su exhaustividad baja en este conjunto, y por eso la evaluación es honesta: el modelo no vio estos datos.
- **Umbral:** 0.5 da mejor F1 que 0.3. Bajarlo agrega más falsas alarmas que aciertos.
- **Costo:** las reglas son unas 100 veces más rápidas que el modelo. Un worker sin GPU puede usar solo las reglas (`WA_TEXTO_MODELO=0`) y aun así detectar buena parte de los insultos explícitos (exhaustividad 0.39).

## Ajuste de la lista de insultos con el dataset generado
La primera versión incluía "basura", "cerdo" y "cerda". Al procesar los chats del dataset generado, **73 de 77 falsas alarmas** venían de "basura" en mensajes como "¿Quién saca la basura mañana?". Se quitaron esas palabras, que tienen un uso cotidiano frecuente.

| Caso (dataset generado) | Precisión antes → después | Exhaustividad antes → después | F1 antes → después |
|---|---|---|---|
| 027_vecinos_del_condominio | 0.43 → 0.91 | 0.70 → 0.64 | 0.53 → 0.75 |
| 017_familia_quesada | 0.43 → 0.96 | 0.62 → 0.60 | 0.51 → 0.73 |

Las 4 falsas alarmas restantes son comentarios reales de OffendES que sus anotadores marcaron como no ofensivos, aunque son discutibles, por ejemplo "que mina tan ridícula".

## Ajuste de las reglas de amenaza (11/11/2026)
**Problema reportado:** el mensaje "voy a matar a alguien" no generó alerta. La regla exigía que la amenaza fuera dirigida con "te" ("te voy a matar"), y el modelo (entrenado con discurso de odio, no con amenazas) no lo marcó como agresivo.

**Cambio:** una regla general de intención de daño, "(te/lo/le…) voy / vamos / pienso / quiero + matar, apuñalar, disparar, golpear, quemar, secuestrar…", más "te van a…" y "le voy a dar un tiro / una paliza". Se excluyen:
- usos inofensivos: "matar el tiempo / el hambre";
- frases sobre uno mismo, con "me" o "nos" delante: "me voy a matar estudiando";
- narraciones en pasado: "la mató" se compara respetando la tilde, para no confundirlo con "la mato".

También se quitó la regla de "cuídate / ten cuidado", que marcaba frases cariñosas.

| Prueba | Antes | Después |
|---|---|---|
| 24 frases de prueba (15 amenazas y 9 normales parecidas) | no detectaba "voy a matar a alguien", entre otras | 0 errores |
| Comentarios de OffendES no ofensivos marcados como amenaza | 31 de 11 055 | 11 de 11 055 (0,1 %) |
| Mensajes del dataset generado marcados como amenaza | — | 3 de 19 813 (los 3 son comentarios ofensivos reales) |

Las 11 falsas alarmas restantes son ambiguas incluso para una persona, por ejemplo citas de amenazas ("y si te mato aquí mismo") o "la mato" escrito sin tilde.
