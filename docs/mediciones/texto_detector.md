# Medición: detector de agresiones en texto

Evaluación independiente del detector de texto de los workers (`distribuido/worker/detectores.py`). También es evidencia de memoria y tiempo para **M1** y **M5**.

> **Detector actual (semana 15): Ollama con qwen2.5:3b**, ver la [última sección](#detector-actual-modelo-de-lenguaje-local-ollama). Las secciones anteriores miden el detector **anterior** (reglas + lista de insultos + pysentimiento/RoBERTuito), que se conserva como alternativa para nodos sin Ollama (`WA_TEXTO_MOTOR=pysentimiento`).

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

## Ajuste por errores de transcripción en las notas de voz (12/11/2026)
**Problema:** con 8 notas de voz grabadas por el equipo, el detector marcó 6. Los dos fallos vinieron de la transcripción y no del detector. Whisper (tanto small como medium) transcribió "vamos a **asaltar** la pulpería" como "vamos a **saltar** la pulpería", y "te voy a apuñalar", dicho rápido, como "te voy a **puñalar**".

**Cambio:** dos reglas tolerantes a esos errores:
- "voy / vamos / van / hay que + **saltar** + un local" (pulpería, banco, tienda, farmacia…) se marca como actividad ilícita, porque nadie "salta" un local;
- "puñalar" se trata como variante de "apuñalar".

| Prueba | Resultado |
|---|---|
| Las 8 notas de voz del equipo | 6 → **8** detectadas |
| Frases parecidas inofensivas ("saltar la cuerda", "saltar al agua", "saltar la tapia") | 0 marcadas |
| Comentarios de OffendES no ofensivos marcados por las reglas nuevas | 0 de 11 055 |

## Ampliación de las reglas, evaluando cada candidata (12/11/2026)
**Motivo:** las listas de insultos, amenazas y actividades ilícitas eran cortas. Para ampliarlas sin repetir el error de "basura", cada palabra o regla candidata se probó **por separado** antes de agregarla.

**Método:** `python distribuido/evaluar_reglas.py`. Cada candidata se compara contra:
- 25 576 comentarios **no ofensivos** de OffendES (train, validation y test) → falsas alarmas;
- 18 952 mensajes **normales** de los chats sintéticos → falsas alarmas en conversaciones tipo WhatsApp;
- 4 840 comentarios **ofensivos** de OffendES → cuántos nuevos encuentra.

**Criterio:** como máximo 2 falsas alarmas en OffendES y ninguna en los chats. El detalle de cada candidata está en `docs/mediciones/reglas_candidatas.json`.

| Categoría | Candidatas | Aceptadas | Ejemplos agregados | Descartadas (falsas alarmas) |
|---|---|---|---|---|
| Insultos | 77 | 52 | carepicha, playo, mamahuevo, malnacido, comemierda, cretino, sudaca, travelo, pedazo de mierda | "puto" (602: intensificador en España), "putos" (64), "putas" (34), "estúpidos" (17), "idiotas" (10), "ignorantes" (10), "payasos" (6) |
| Verbos de daño | 22 | 19 | torturar, envenenar, linchar, estrangular, acribillar, volarle la cabeza, dejarte en silla de ruedas | "pelar", "quebrar" y "rematar": sin falsas alarmas en los datos, pero descartadas **por criterio** ("voy a pelar las papas", "vamos a quebrar", "voy a rematar la tarea") |
| Amenazas | 12 | 11 | "te quiero ver muerto", "no sabés con quién te metiste", sextorsión ("si no… publico tus fotos"), "te tengo vigilada" | "cuidá a tu familia" (marcó "cuidá a tu mamá que está enferma") |
| Actividades ilícitas | 8 | 8 | lavado de dinero, falsificación de documentos, trata y tráfico, "le bajaron el celular", jerga de drogas y armas | "monte" (marcó "vende monte de jardín") |

**Verificación con frases de prueba:**
- 25 frases agresivas (amenazas, ilícitos e insultos nuevos): **25 detectadas**.
- 18 frases normales parecidas ("voy a pelar las papas", "voy a publicar las fotos del paseo", "le bajaron el precio al celular"…): **0 falsas alarmas**.
- "qué tarados los de la tele" se marca como insulto. Es correcto: es un insulto, aunque sea en broma.

**Efecto sobre OffendES test (solo reglas, sin modelo):**

| | Precisión | Exhaustividad | F1 | Verdaderos positivos | Falsas alarmas |
|---|---|---|---|---|---|
| Antes | 0,530 | 0,392 | 0,450 | 999 | 887 |
| Después | 0,531 | 0,397 | 0,455 | 1014 | 895 |

En OffendES (comentarios de YouTube, sobre todo de España y México) la mejora es pequeña: +15 aciertos con +8 falsas alarmas, y la precisión se mantiene. El aporte principal está en lo que OffendES **no contiene**: amenazas, sextorsión, actividades ilícitas y jerga costarricense, que son justo lo que aparece en chats reales de WhatsApp.

## Detector actual: modelo de lenguaje local (Ollama)

Desde la semana 15 el texto se clasifica con un **modelo de lenguaje local**: [Ollama](https://ollama.com) con **qwen2.5:3b** (3 000 millones de parámetros, 1,9 GB, gratuito y sin conexión a internet). El modelo recibe un contexto escrito por el equipo (`distribuido/worker/contexto_ollama.md`) con la definición de cada categoría, la severidad, jerga de Costa Rica y lo que **no** se debe marcar.

**Cómo clasifica (dos pasadas):**
1. **Pasada rápida por lotes:** recibe 25 mensajes seguidos del chat y devuelve solo la categoría de cada uno (`amenaza`, `ilicito`, `odio`, `agresion`, `insulto` o `normal`), en JSON validado con un esquema.
2. **Candidatos de las reglas y de RoBERTuito:** las reglas de amenaza e ilícito, la lista de insultos y RoBERTuito (con umbral 0,4) **ya no marcan nada por sí solos**; solo proponen mensajes para revisar (versión final, ver *Detector combinado*). Las groserías de muletilla ("mierda", "puta", "hijueputa" sueltas) no se proponen.
3. **Revisión individual:** cada mensaje marcado (~5 %) se revisa solo, con los 3 mensajes anteriores como contexto. El modelo primero explica qué dice el mensaje y después decide la categoría; si concluye `normal`, se descarta.
4. **Severidad (criterio del equipo):** ilícito siempre **alta**; odio e insulto siempre **media** (machismo, homofobia, maltrato verbal); en amenaza y agresión decide el modelo: **alta** si hay riesgo o violencia física (muerte, golpiza, extorsión), **media** si no ("le voy a contar a tu mamá", ciberbullying).

**Por qué dos pasadas:** con una sola pasada el modelo de 3B marcaba mensajes cotidianos ("Necesito la factura de ayer" → ilícito, "Se fue el internet otra vez" → ilícito) porque elegía la etiqueta antes de razonar. Con la revisión individual esos errores desaparecieron, y como solo se explican los mensajes marcados, quedó unas 4,5 veces más rápido que la primera versión (478 s → 107 s en 406 mensajes).

### Comparación con el detector anterior (mismos mensajes)

- **Script:** `distribuido/comparar_detectores.py` (`WA_TEXTO_MOTOR=ollama` o `pysentimiento`). Resultados completos, con cada error, en `docs/mediciones/comparacion_detectores/`.
- **Equipo:** RTX 3050 Laptop (4 GB); los dos detectores en GPU.

| Conjunto | Mensajes | Anterior: P / E / F1 | Falsas alarmas | Ollama: P / E / F1 | Falsas alarmas |
|---|---|---|---|---|---|
| Chats sintéticos (6 casos, ground truth) | 1917 | 0,92 / 0,75 / **0,83** | 2 | 0,89 / 0,50 / **0,64** | 2 |
| OffendES (150 ofensivos + 150 no ofensivos, semilla 2026) | 300 | 0,90 / 0,69 / **0,78** | 12 | 0,98 / 0,57 / **0,72** | 2 |
| Frases de desafío (jerga tica, amenazas, quejas, muletillas) ⚠️ | 36 | 0,78 / 0,78 / **0,78** | 4 | 0,94 / 0,89 / **0,91** | 1 |

P = precisión, E = exhaustividad. ⚠️ Las frases de desafío se usaron para **ajustar el contexto** de Ollama, así que su resultado es optimista; las comparaciones justas son las dos primeras filas, que ningún detector vio antes.

**Velocidad:** el detector anterior tarda ~27 ms por mensaje; Ollama, ~277 ms en los chats y ~623 ms en OffendES (comentarios más largos y más candidatos a revisar). Es decir, **10 a 25 veces más lento**.

**Interpretación:**
- **Ollama es más preciso:** en OffendES da 2 falsas alarmas contra 12. Entiende el contexto: no marca groserías de muletilla ("puta, se me olvidó la llave", "hijueputa, ¡ganamos el partido!"), ni quejas cotidianas, ni bromas ("te mato si no venís a la fiesta jajaja" es su único error en ese sentido). Detecta amenazas sin palabras clave ("te espero afuera y vas a ver"), violencia contada ("ayer le pegué a mi ex hasta dejarla morada") y jerga tica ("¿y si le bajamos el cel a ese carajillo?"), que el detector anterior no detectaba.
- **El anterior encuentra más ofensas leves:** "esa tipa está dañada", "la loca", "a cada video se ve más gorda". Casi todos los positivos del dataset sintético vienen de OffendES (comentarios de YouTube de España y México), y RoBERTuito se entrenó con datos parecidos (tweets). Por eso su exhaustividad es mayor (0,75 contra 0,50 en los chats).
- **Costo:** Ollama necesita GPU para ser práctico; en CPU (Colab sin GPU, Azure) es mucho más lento.

### Detector combinado (versión final)

Después de la comparación se probó **unir los dos**: RoBERTuito y las reglas **proponen** candidatos (rápido y con buena exhaustividad) y Ollama **revisa** cada candidato por separado (precisión y contexto). Hay dos variantes, según si la pasada por lotes de Ollama también propone candidatos (`OLLAMA_CANDIDATOS=modelo` o `ambos`). Mismos mensajes y mismo script:

| Variante | Chats sintéticos: P / E / **F1** · FA | OffendES: P / E / **F1** · FA | Desafío ⚠️ F1 | ms/mensaje (chats) |
|---|---|---|---|---|
| Anterior (reglas + RoBERTuito) | 0,92 / 0,75 / **0,83** · 2 | 0,90 / 0,69 / **0,78** · 12 | 0,78 | 27 |
| Ollama solo (dos pasadas) | 0,89 / 0,50 / **0,64** · 2 | 0,98 / 0,57 / **0,72** · 2 | 0,91 | 277 |
| Combinado: RoBERTuito propone, Ollama revisa | 0,95 / 0,56 / **0,71** · 1 | 0,95 / 0,59 / **0,72** · 5 | 0,77 | 40 |
| **Combinado ambos: RoBERTuito y Ollama proponen, Ollama revisa (en uso)** | 0,88 / 0,66 / **0,75** · 3 | 0,95 / 0,75 / **0,84** · 6 | 0,91 | 309 |

P = precisión, E = exhaustividad, FA = falsas alarmas. ⚠️ Las frases de desafío se usaron para ajustar el contexto de Ollama (resultado optimista).

- **"Combinado ambos" es la mejor variante con Ollama:** frente a Ollama solo, sube la exhaustividad (0,50 → 0,66 en los chats, 0,57 → 0,75 en OffendES) manteniendo la precisión alta. En OffendES es **la mejor de las cuatro** (F1 0,84 frente a 0,78 del anterior), con la mitad de falsas alarmas que el anterior (6 frente a 12).
- **En los chats sintéticos el anterior sigue con el mejor F1** (0,83 frente a 0,75): sus positivos son sobre todo ofensas leves de OffendES, que RoBERTuito propone pero Ollama a veces descarta al revisarlas.
- **"Combinado" (solo RoBERTuito propone)** es casi tan rápido como el anterior (~40 ms por mensaje), pero pierde lo que solo detecta Ollama ("te espero afuera y vas a ver", "ayer le pegué a mi ex"), así que su exhaustividad es menor.

**Decisión del equipo:** se usa el **combinado "ambos"** (`OLLAMA_CANDIDATOS=ambos`, valor por defecto). Es la variante con mejor equilibrio: mejor F1 en OffendES de las cuatro, pocas falsas alarmas (en un análisis de chats, señalar a alguien sin razón tiene un costo alto) y detecta amenazas sin palabras clave, violencia contada y jerga tica. A cambio es la más lenta (~0,3 s por mensaje en GPU), lo que se compensa repartiendo los bloques entre varios workers con GPU. El detector anterior queda como alternativa (`WA_TEXTO_MOTOR=pysentimiento`) y como respaldo automático en los nodos sin Ollama.

**Limitaciones conocidas:**
- El modelo de 3B no es perfecto: en las pruebas con Ollama solo, "¿tenés mota para el finde?" quedó como normal (lo considera consumo personal) y "le voy a contar a tu mamá" como odio en vez de amenaza (con la severidad correcta, media).
- Con `temperature: 0` el resultado es estable entre corridas con el mismo lote, pero puede cambiar si cambian los mensajes vecinos del lote.
- La segunda pasada se puede desactivar (`OLLAMA_REVISAR=0`): es más rápido, pero vuelven los falsos positivos.

