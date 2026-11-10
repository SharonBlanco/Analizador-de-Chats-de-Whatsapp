"""
Estadísticas de una conversación, calculadas a partir de los mensajes parseados.

Mismas métricas que analyze() de AnalyzeWhatsApp.py, pero solo con la biblioteca
estándar (sin pandas), para que las sirva la interfaz web y los workers.

Está dividido en dos fases para poder repartir el trabajo (map-reduce):
  parcial(bloque)      → agregados de un bloque de mensajes. Lo ejecuta cada worker.
  combinar(parciales)  → une los agregados de todos los bloques (barrier/join).
calcular(mensajes) es combinar([parcial(mensajes)]): el análisis local y el
distribuido usan exactamente el mismo código, así que deben dar el mismo resultado.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta
from statistics import median
from urllib.parse import urlparse

from whatsapp_formato import Mensaje

# Palabras vacías básicas en español e inglés (compartidas con AnalyzeWhatsApp.py)
STOPWORDS = set("""
a al algo ante antes así aun aunque bien cada como con contra cual cuando de del desde donde
dos e el ella ellas ellos en entre era es esa ese eso esta este esto estoy está están fue
ha hay he la las le les lo los me mi mis muy más ni no nos o os para pero por porque que qué
se si sí sin sobre son su sus también te ti tu tus un una uno unos y ya yo vos usted ustedes
jaja jajaja jajajaja xd ok q pq tmb voy va ver bueno pues eh ah oh
the and to of i you it is a in that for on my me be are was this with so just do not have
""".split())

URL = re.compile(r"https?://\S+")
NO_PALABRA = re.compile(r"[^\wáéíóúüñ\s]", re.IGNORECASE)
MULTIMEDIA = {"imagen", "voz", "audio", "video", "documento", "omitido"}
RESPUESTA_MAX_MIN = 12 * 60   # una "respuesta" a más de 12 h no cuenta (mismo criterio que AnalyzeWhatsApp)


def tokens(texto: str) -> list[str]:
    limpio = NO_PALABRA.sub(" ", URL.sub(" ", texto).lower())
    return [p for p in limpio.split() if p not in STOPWORDS and len(p) > 2 and not p.isdigit()]


def previos(mensajes: list[Mensaje]) -> dict[int, tuple[str, str]]:
    """Para el tiempo de respuesta: el mensaje anterior de cada uno en orden cronológico
    (autor, fecha_hora), indexado por el id del mensaje. Se calcula sobre el chat
    completo y se reparte con cada bloque: así un bloque puede medir la respuesta a un
    mensaje que quedó en el bloque anterior."""
    usuarios = sorted((m for m in mensajes if m.tipo != "sistema"), key=lambda m: (m.fecha_hora, m.id))
    return {b.id: (a.autor, a.fecha_hora) for a, b in zip(usuarios, usuarios[1:])}


# ============================================================ fase 1: un bloque
def parcial(mensajes: list[Mensaje], anteriores: dict[int, tuple[str, str]] | None = None) -> dict:
    """Agregados de un bloque de mensajes. El resultado es JSON (viaja por RabbitMQ).
    anteriores=None omite el tiempo de respuesta."""
    autores: dict[str, dict] = {}
    esperas: dict[str, list[float]] = {}
    por_dia: Counter = Counter()
    calor = [[0] * 24 for _ in range(7)]
    por_tipo: Counter = Counter()
    palabras: Counter = Counter()
    dominios: Counter = Counter()
    usuarios = adjuntos = 0

    for m in mensajes:
        por_tipo[m.tipo] += 1
        if m.adjunto:
            adjuntos += 1
        if m.tipo == "sistema":
            continue
        usuarios += 1
        a = autores.setdefault(m.autor, {"mensajes": 0, "palabras": 0, "textos": 0, "multimedia": 0,
                                         "primero": m.fecha_hora, "ultimo": m.fecha_hora})
        a["mensajes"] += 1
        if m.tipo == "texto":
            a["textos"] += 1
            a["palabras"] += len(m.texto.split())
            palabras.update(tokens(m.texto))
        elif m.tipo in MULTIMEDIA:
            a["multimedia"] += 1
        a["primero"] = min(a["primero"], m.fecha_hora)
        a["ultimo"] = max(a["ultimo"], m.fecha_hora)

        f = datetime.fromisoformat(m.fecha_hora)
        por_dia[m.fecha_hora[:10]] += 1
        calor[f.weekday()][f.hour] += 1
        dominios.update(urlparse(u).netloc.replace("www.", "") for u in URL.findall(m.texto))

        previo = anteriores.get(m.id) if anteriores is not None else None
        if previo and previo[0] != m.autor:
            minutos = (f - datetime.fromisoformat(previo[1])).total_seconds() / 60
            if 0 <= minutos < RESPUESTA_MAX_MIN:
                esperas.setdefault(m.autor, []).append(minutos)

    return {"total": len(mensajes), "usuarios": usuarios, "adjuntos": adjuntos,
            "autores": autores, "esperas": esperas, "por_dia": dict(por_dia), "calor": calor,
            "por_tipo": dict(por_tipo), "palabras": dict(palabras), "dominios": dict(dominios)}


# ============================================================ fase 2: unir bloques
def combinar(parciales: list[dict], respuestas: bool = True) -> dict:
    """Une los agregados de los bloques EN ORDEN (el orden de primera aparición decide
    los empates, igual que en un único recorrido)."""
    autores: dict[str, dict] = {}
    esperas: dict[str, list[float]] = {}
    por_dia: Counter = Counter()
    calor = [[0] * 24 for _ in range(7)]
    por_tipo: Counter = Counter()
    palabras: Counter = Counter()
    dominios: Counter = Counter()
    total = usuarios = adjuntos = 0

    for p in parciales:
        total += p["total"]
        usuarios += p["usuarios"]
        adjuntos += p["adjuntos"]
        for autor, x in p["autores"].items():
            a = autores.get(autor)
            if a is None:
                autores[autor] = dict(x)
                continue
            for k in ("mensajes", "palabras", "textos", "multimedia"):
                a[k] += x[k]
            a["primero"] = min(a["primero"], x["primero"])
            a["ultimo"] = max(a["ultimo"], x["ultimo"])
        for autor, lista in p["esperas"].items():
            esperas.setdefault(autor, []).extend(lista)
        por_dia.update(p["por_dia"])
        for d in range(7):
            for h in range(24):
                calor[d][h] += p["calor"][d][h]
        por_tipo.update(p["por_tipo"])
        palabras.update(p["palabras"])
        dominios.update(p["dominios"])

    total_usuarios = usuarios or 1
    participantes = []
    for autor, a in autores.items():
        e = esperas.get(autor, []) if respuestas else []
        participantes.append({
            "autor": autor, "mensajes": a["mensajes"],
            "porcentaje": round(100 * a["mensajes"] / total_usuarios, 1),
            "palabras_promedio": round(a["palabras"] / a["textos"], 1) if a["textos"] else 0,
            "multimedia": a["multimedia"],
            "respuesta_mediana_min": round(median(e), 1) if e else None,
            "respuestas": len(e),
            "primero": a["primero"], "ultimo": a["ultimo"],
        })
    participantes.sort(key=lambda p: -p["mensajes"])

    # actividad temporal (días sin mensajes incluidos con 0)
    serie_dias = []
    if por_dia:
        d, fin = datetime.fromisoformat(min(por_dia)), datetime.fromisoformat(max(por_dia))
        while d <= fin:
            k = d.date().isoformat()
            serie_dias.append({"fecha": k, "mensajes": por_dia.get(k, 0)})
            d += timedelta(days=1)
    por_hora = [sum(calor[d][h] for d in range(7)) for h in range(24)]
    por_dia_semana = [sum(fila) for fila in calor]

    hora_pico = max(range(24), key=lambda h: por_hora[h]) if usuarios else None
    dia_pico = max(serie_dias, key=lambda x: x["mensajes"]) if serie_dias else None
    return {
        "resumen": {
            "mensajes": total,
            "mensajes_usuarios": usuarios,
            "participantes": len(participantes),
            "dias_periodo": len(serie_dias),
            "dias_activos": sum(1 for x in serie_dias if x["mensajes"]),
            "promedio_diario": round(usuarios / len(serie_dias), 1) if serie_dias else 0,
            "adjuntos": adjuntos,
            "hora_pico": hora_pico,
            "dia_pico": dia_pico,
        },
        "participantes": participantes,
        "por_dia": serie_dias,
        "por_hora": por_hora,
        "por_dia_semana": por_dia_semana,
        "calor": calor,
        "por_tipo": dict(por_tipo.most_common()),
        "palabras": por_lista(palabras.most_common(25)),
        "dominios": por_lista(dominios.most_common(10)),
    }


def por_lista(pares) -> list[list]:
    # listas (no tuplas): así el resultado es idéntico antes y después de pasar por JSON
    return [list(p) for p in pares]


def calcular(mensajes: list[Mensaje], respuestas: bool = True) -> dict:
    """Análisis completo en un solo proceso (un único bloque).
    respuestas=False omite el tiempo de respuesta: al juntar varios chats, el mensaje
    "siguiente" puede ser de otra conversación y la medida no tendría sentido."""
    return combinar([parcial(mensajes, previos(mensajes) if respuestas else None)], respuestas)
