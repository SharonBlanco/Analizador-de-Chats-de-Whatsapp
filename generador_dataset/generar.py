"""
Genera el dataset del enunciado (B.3) a partir de las fuentes de fuentes_dataset/.

Cada caso es una exportación de WhatsApp tal como la produce el teléfono (carpeta con el
.txt y los adjuntos con sus nombres reales: IMG-…, PTT-…, VID-…), en tres formatos:
Android 12 h, Android 24 h e iOS. Los mensajes normales son sintéticos; los positivos
(ofensivos, odio, amenazas) salen de OffendES y HatEval, y las imágenes y audios de
Open Images y FLEURS. Como el generador decide qué va en cada mensaje, el ground truth
es exacto: dataset_generado/verdad/.

Composición (mínimos de B.3 entre paréntesis):
  40 casos (40) · 12 heterogéneos (10) · ≥ 20 000 mensajes (20 000) · 300 imágenes (300)
  150 notas de voz (150) · 5 casos con video ≥ 200 MB (5) · ground truth completo

Uso:
  python generador_dataset/generar.py                      # genera dataset_generado/
  python generador_dataset/generar.py --importar           # y además lo importa a dataset_whatsapp/
  python generador_dataset/generar.py --semilla 7 --sobrescribir
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import shutil
import subprocess
import sys
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

AQUI = Path(__file__).resolve().parent
RAIZ = AQUI.parent
sys.path.insert(0, str(AQUI))
sys.path.insert(0, str(RAIZ / "mensajeria"))
import contenido as C  # noqa: E402
from whatsapp_formato import parsear_archivo  # noqa: E402

FUENTES = RAIZ / "fuentes_dataset"
NNBSP, LRM = " ", "‎"
MARCA = ".generado_por_generador_dataset"       # solo se borra una salida que tenga esta marca

COMPOSICION = [("pesado", 5), ("heterogeneo", 7), ("texto_imagen", 8), ("texto_audio", 6), ("solo_texto", 14)]
IMAGENES_POR = {"pesado": 12, "heterogeneo": 12}       # el resto va a texto_imagen
AUDIOS_POR = {"pesado": 7, "heterogeneo": 7}           # el resto va a texto_audio
FORMATOS = [("android12", .70), ("android24", .15), ("ios", .15)]
PRIORIDADES = [(2, .30), (5, .40), (8, .20), (10, .10)]          # baja, media, alta, crítica
TASA_POSITIVOS = (0.05, 0.10)          # en casos "con incidencias"
TASA_POSITIVOS_BAJA = 0.003            # en el resto (alguno aislado)
TASA_NEGATIVOS_DATASET = 0.02          # comentarios reales no ofensivos (negativos difíciles)
CASOS_CON_INCIDENCIAS = 16


def elegir(rng: random.Random, pares):
    return rng.choices([p[0] for p in pares], weights=[p[1] for p in pares])[0]


def slug(texto: str) -> str:
    t = unicodedata.normalize("NFKD", texto).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", t.lower()).strip("_")[:40]


def limpiar(texto: str) -> str:
    """Texto de un dataset público → una línea de chat: sin menciones, enlaces ni saltos."""
    texto = re.sub(r"@\w+", "", texto)
    texto = re.sub(r"https?://\S+", "", texto)
    texto = re.sub(r"\b(RT|USER|URL)\b", "", texto)
    return re.sub(r"\s+", " ", texto).strip(" :")


# ============================================================ fuentes
def leer_tabla(ruta: Path) -> list[dict]:
    if ruta.suffix.lower() in (".jsonl", ".json"):
        texto = ruta.read_text(encoding="utf-8")
        try:
            datos = json.loads(texto)
            return datos if isinstance(datos, list) else datos.get("data", [])
        except json.JSONDecodeError:
            return [json.loads(l) for l in texto.splitlines() if l.strip()]
    with open(ruta, encoding="utf-8-sig", newline="") as f:
        muestra = f.read(4096)
        f.seek(0)
        delim = "\t" if ruta.suffix.lower() == ".tsv" or muestra.count("\t") > muestra.count(",") else ","
        return list(csv.DictReader(f, delimiter=delim))


def columna(filas: list[dict], opciones: list[str]) -> str | None:
    claves = {k.lower(): k for k in (filas[0] if filas else {})}
    return next((claves[o] for o in opciones if o in claves), None)


def cargar_textos() -> tuple[list[dict], list[dict], list[str]]:
    """→ (positivos, negativos_dataset, notas). Cada uno: {texto, categoria, fuente, id}."""
    pos, neg, notas = [], [], []
    base = FUENTES / "texto"
    # OffendES: etiquetas OFP (ofensivo a persona), OFG (a grupo), NOE (no ofensivo con groserías), NO
    for ruta in sorted((base / "offendes").glob("*")) if (base / "offendes").is_dir() else []:
        if ruta.suffix.lower() not in (".csv", ".tsv", ".json", ".jsonl"):
            continue
        filas = leer_tabla(ruta)
        ct = columna(filas, ["comment", "text", "texto", "tweet", "content"])
        ce = next((k for k in (filas[0] if filas else {}) if {str(f.get(k)).strip() for f in filas[:500]} <= {"OFP", "OFG", "NO", "NOE"}), None)
        ci = columna(filas, ["comment_id", "id", "tweet_id"])
        if not ct or not ce:
            notas.append(f"OffendES {ruta.name}: no se reconocieron las columnas ({', '.join(filas[0]) if filas else 'vacío'})")
            continue
        for i, f in enumerate(filas):
            t, e = limpiar(str(f[ct])), str(f[ce]).strip()
            if 3 <= len(t.split()) <= 60:
                d = {"texto": t, "fuente": f"OffendES/{ruta.name}", "id": str(f.get(ci) or i), "etiqueta_original": e}
                (pos if e in ("OFP", "OFG") else neg).append({**d, "categoria": "ofensivo" if e in ("OFP", "OFG") else "normal"})
        notas.append(f"OffendES {ruta.name}: {len(filas)} filas")
    # HatEval: HS (odio), TR (objetivo), AG (agresivo)
    for ruta in sorted((base / "hateval").glob("*")) if (base / "hateval").is_dir() else []:
        if ruta.suffix.lower() not in (".csv", ".tsv", ".json", ".jsonl"):
            continue
        filas = leer_tabla(ruta)
        ct, chs, cag, ci = (columna(filas, ["text", "tweet", "texto"]), columna(filas, ["hs"]),
                            columna(filas, ["ag"]), columna(filas, ["id"]))
        if not ct or not chs:
            notas.append(f"HatEval {ruta.name}: no se reconocieron las columnas ({', '.join(filas[0]) if filas else 'vacío'})")
            continue
        for i, f in enumerate(filas):
            t = limpiar(str(f[ct]))
            if not 3 <= len(t.split()) <= 60:
                continue
            hs, ag = str(f[chs]).strip() == "1", cag and str(f[cag]).strip() == "1"
            d = {"texto": t, "fuente": f"HatEval/{ruta.name}", "id": str(f.get(ci) or i),
                 "etiqueta_original": f"HS={int(hs)} AG={int(bool(ag))}"}
            if hs:
                pos.append({**d, "categoria": "odio_agresivo" if ag else "odio"})
            else:
                neg.append({**d, "categoria": "normal"})
        notas.append(f"HatEval {ruta.name}: {len(filas)} filas")
    if not pos:
        notas.append("Sin OffendES ni HatEval: se usan los positivos sintéticos de contenido.py (declarar en la bitácora de IA)")
        pos = [{"texto": t, "categoria": c, "fuente": "sintetico_ia", "id": f"S{i:02d}", "etiqueta_original": c}
               for i, (c, t) in enumerate(C.POSITIVOS_SINTETICOS)]
    return pos, neg, notas


def cargar_fleurs() -> list[dict]:
    d = FUENTES / "audio_fleurs"
    clips = []
    with open(d / "test.tsv", encoding="utf-8") as f:
        for fila in csv.reader(f, delimiter="\t"):
            wav = d / "wav" / fila[1]
            if wav.exists():
                clips.append({"id": fila[0], "archivo": wav, "texto": fila[2], "segundos": int(fila[5]) / 16000,
                              "genero": fila[6]})
    return clips


def cargar_grabaciones() -> list[dict]:
    """Notas de voz grabadas por el equipo con frases positivas (opcional)."""
    d = FUENTES / "grabaciones"
    if not (d / "grabaciones.csv").exists():
        return []
    salida = []
    for f in leer_tabla(d / "grabaciones.csv"):
        # el nombre puede venir sin extensión o con otra (el teléfono graba .opus, .m4a, .ogg…)
        ruta = d / f["archivo"]
        if not ruta.exists():
            ruta = next(iter(sorted(d.glob(Path(f["archivo"]).stem + ".*"))), None)
        if ruta and ruta.suffix != ".csv":
            salida.append({"archivo": ruta, "texto": f["transcripcion"], "categoria": f.get("categoria") or "amenaza"})
    return salida


# ============================================================ plan de casos
@dataclass
class Caso:
    n: int
    tipo: str
    nombre: str
    grupo: bool
    formato: str
    participantes: list[str]
    mensajes: int
    prioridad: int
    incidencias: bool
    imagenes: list = field(default_factory=list)
    audios: list = field(default_factory=list)
    videos: list = field(default_factory=list)
    documentos: list = field(default_factory=list)


def planear(rng: random.Random, total: int, imagenes: list, notas_voz: list, videos: list) -> list[Caso]:
    tipos = [t for t, k in COMPOSICION for _ in range(k)]
    rng.shuffle(tipos)
    pesos = [rng.lognormvariate(0, 0.6) * (1.6 if t in ("pesado", "heterogeneo") else 1) for t in tipos]
    tam = [max(60, round(total * p / sum(pesos))) for p in pesos]
    tam[tam.index(max(tam))] += total - sum(tam)                      # cuadrar el total exacto
    incid = set(rng.sample(range(len(tipos)), CASOS_CON_INCIDENCIAS))
    casos = []
    for i, tipo in enumerate(tipos):
        grupo = rng.random() < 0.7
        k = rng.randint(3, 9) if grupo else 2
        nombres = []
        while len(nombres) < k:
            n = f"{rng.choice(C.NOMBRES)} {rng.choice(C.APELLIDOS)}"
            if n not in nombres:
                nombres.append(n)
        nombre = (rng.choice(C.GRUPOS).format(apellido=rng.choice(C.APELLIDOS), nombre=rng.choice(C.NOMBRES))
                  if grupo else nombres[1])
        casos.append(Caso(i + 1, tipo, nombre, grupo, elegir(rng, FORMATOS), nombres, tam[i],
                          elegir(rng, PRIORIDADES), i in incid))

    def repartir(items, por_tipo, resto_tipo, atributo):
        cola = list(items)
        rng.shuffle(cola)
        for c in casos:
            if c.tipo in por_tipo:
                getattr(c, atributo).extend(cola[:por_tipo[c.tipo]])
                del cola[:por_tipo[c.tipo]]
        destino = [c for c in casos if c.tipo == resto_tipo]
        for j, item in enumerate(cola):
            getattr(destino[j % len(destino)], atributo).append(item)

    repartir(imagenes, IMAGENES_POR, "texto_imagen", "imagenes")
    repartir(notas_voz, AUDIOS_POR, "texto_audio", "audios")
    pesados = [c for c in casos if c.tipo == "pesado"]
    for j, c in enumerate(pesados):
        if videos:
            c.videos.append(videos[j % len(videos)])          # un video se repite: sirve para medir deduplicación (M3)
    for c in casos:
        if c.tipo == "heterogeneo":
            c.documentos.append(C.DOCUMENTOS[c.n % len(C.DOCUMENTOS)])
    return casos


def notas_de_voz(rng: random.Random, clips: list[dict], n: int, grabaciones: list[dict]) -> list[dict]:
    """n notas de voz de distinta duración: 1 a 8 frases de FLEURS concatenadas."""
    disponibles = clips[:]
    rng.shuffle(disponibles)
    notas = []
    for _ in range(n):
        k = elegir(rng, [(1, .50), (2, .25), (3, .10), (4, .05), (6, .06), (8, .04)])
        partes = [disponibles.pop() for _ in range(min(k, len(disponibles)))] or [rng.choice(clips)]
        notas.append({"clips": partes, "transcripcion": " ".join(p["texto"] for p in partes),
                      "segundos": round(sum(p["segundos"] for p in partes), 2), "positivo": False,
                      "categoria": "", "fuente": "FLEURS es_419 test: " + ", ".join(p["archivo"].stem for p in partes)})
    for g in grabaciones:
        notas.append({"clips": [{"archivo": g["archivo"]}], "transcripcion": g["texto"], "segundos": None,
                      "positivo": True, "categoria": g["categoria"], "fuente": f"grabación del equipo: {g['archivo'].name}"})
    return notas


# ============================================================ texto de los chats
def rellenar(rng, plantilla: str, c: Caso) -> str:
    texto = plantilla.format(nombre=rng.choice(c.participantes).split()[0], dia=rng.choice(C.DIAS),
                            hora=rng.choice(C.HORAS), lugar=rng.choice(C.LUGARES).format(nombre=rng.choice(C.NOMBRES)),
                            materia=rng.choice(C.MATERIAS), n=rng.randint(1, 99),
                            nombres=", ".join(p.split()[0] for p in c.participantes))
    return re.sub(r"\bde el\b", "del", re.sub(r"\ba el\b", "al", texto))


def tema_de(c: Caso) -> str | None:
    return next((t for t, claves in C.TEMAS_POR_GRUPO.items() if any(k in c.nombre for k in claves)), None)


def mensaje_normal(rng, c: Caso, tema) -> str:
    r = rng.random()
    if r < 0.012:
        return rellenar(rng, rng.choice(C.MULTILINEA), c)
    if r < 0.019:
        return "Se eliminó este mensaje"
    if r < 0.024 and c.tipo == "solo_texto":
        return "<Multimedia omitido>"
    grupo = tema if tema and rng.random() < 0.5 else rng.choice(list(C.PLANTILLAS))
    texto = rellenar(rng, rng.choice(C.PLANTILLAS[grupo]), c)
    if rng.random() < 0.02:
        texto += " " + rng.choice(C.ENLACES).format(n=rng.randint(10, 99))
    if rng.random() < 0.18:
        texto += " " + rng.choice(C.EMOJIS)
    return texto


def marcas_de_tiempo(rng, n: int, inicio: datetime) -> list[datetime]:
    t, salida = inicio, []
    for _ in range(n):
        r = rng.random()
        t += timedelta(minutes=rng.expovariate(1 / 2.5) if r < 0.72 else
                       rng.expovariate(1 / 120) if r < 0.96 else rng.uniform(600, 2400))
        if 1 <= t.hour < 6 and rng.random() < 0.9:                     # casi nadie escribe de madrugada
            t = t.replace(hour=rng.randint(6, 8), minute=rng.randint(0, 59))
        salida.append(t)
    return salida


def prefijo(formato: str, f: datetime) -> str:
    h, ap = f.hour % 12 or 12, "a." if f.hour < 12 else "p."
    if formato == "android12":
        return f"{f:%d/%m/%Y}, {h}:{f:%M}{NNBSP}{ap}{NNBSP}m. - "
    if formato == "android24":
        return f"{f:%d/%m/%y}, {f:%H:%M} - "
    return f"[{f:%d/%m/%y}, {h}:{f:%M:%S}{NNBSP}{ap}{NNBSP}m.] "


def nombre_adjunto(formato: str, tipo: str, f: datetime, k: int, ext: str, original: str = "") -> str:
    if tipo == "documento":
        return original
    if formato == "ios":
        return f"{k:08d}-{ {'imagen': 'PHOTO', 'voz': 'AUDIO', 'video': 'VIDEO'}[tipo]}-{f:%Y-%m-%d-%H-%M-%S}{ext}"
    pref = {"imagen": "IMG", "voz": "PTT", "video": "VID"}[tipo]
    return f"{pref}-{f:%Y%m%d}-WA{k:04d}{ext}"


def linea_adjunto(formato: str, archivo: str, pie: str) -> str:
    if formato == "ios":
        return f"{LRM}<adjunto: {archivo}>"
    return f"{LRM}{archivo} (archivo adjunto)" + (f"\n{pie}" if pie else "")


# ============================================================ archivos
def pdf_simple(lineas: list[str]) -> bytes:
    """PDF de una página con texto (sin dependencias)."""
    def esc(s):
        s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    flujo = "BT /F1 12 Tf 60 770 Td 18 TL " + " ".join(f"({esc(l)}) Tj T*" for l in lineas) + " ET"
    objetos = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
               f"<< /Length {len(flujo)} >>\nstream\n{flujo}\nendstream", "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    salida, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objetos, 1):
        offsets.append(len(salida))
        salida += f"{i} 0 obj\n{o}\nendobj\n".encode()
    xref = len(salida)
    salida += f"xref\n0 {len(objetos) + 1}\n0000000000 65535 f \n".encode()
    salida += "".join(f"{o:010d} 00000 n \n" for o in offsets).encode()
    salida += f"trailer\n<< /Size {len(objetos) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return salida


def a_opus(entradas: list[Path], destino: Path) -> None:
    """Concatena los audios y los codifica como nota de voz de WhatsApp (Opus mono)."""
    args = ["ffmpeg", "-v", "error", "-y"]
    for e in entradas:
        args += ["-i", str(e)]
    filtro = "".join(f"[{i}:a]" for i in range(len(entradas))) + f"concat=n={len(entradas)}:v=0:a=1[a]"
    args += ["-filter_complex", filtro, "-map", "[a]", "-ac", "1", "-ar", "48000", "-c:a", "libopus", "-b:a", "24k", str(destino)]
    subprocess.run(args, check=True, capture_output=True)


def enlazar(origen: Path, destino: Path) -> None:
    """Video pesado: enlace duro si se puede (mismo disco), si no, copia."""
    try:
        os.link(origen, destino)
    except OSError:
        shutil.copy2(origen, destino)


# ============================================================ un caso
def generar_caso(rng, c: Caso, salida: Path, positivos: list, negativos: list, tareas_audio: list) -> dict:
    carpeta = salida / "casos" / f"{c.n:03d}_{slug(c.nombre)}"
    carpeta.mkdir(parents=True)
    tema = tema_de(c)
    # --- eventos (uno por mensaje de participante)
    adjuntos = ([("imagen", x) for x in c.imagenes] + [("voz", x) for x in c.audios] +
                [("video", x) for x in c.videos] + [("documento", x) for x in c.documentos])
    n_pos = round(c.mensajes * rng.uniform(*TASA_POSITIVOS)) if c.incidencias else \
        sum(rng.random() < TASA_POSITIVOS_BAJA for _ in range(c.mensajes))
    n_neg = round(c.mensajes * TASA_NEGATIVOS_DATASET) if negativos else 0
    n_normal = max(0, c.mensajes - len(adjuntos) - n_pos - n_neg)
    eventos = ([("adjunto", a) for a in adjuntos] + [("positivo", rng.choice(positivos)) for _ in range(n_pos)] +
               [("negativo", rng.choice(negativos)) for _ in range(n_neg)] + [("normal", None)] * n_normal)
    rng.shuffle(eventos)

    inicio = datetime(2026, rng.randint(1, 9), rng.randint(1, 28), rng.randint(7, 20), rng.randint(0, 59))
    # Resolución del formato: Android muestra minutos; iOS, segundos (el ground truth guarda lo mismo que lee el parser)
    tiempos = [t.replace(second=0 if c.formato != "ios" else t.second, microsecond=0)
               for t in marcas_de_tiempo(rng, len(eventos) + 1, inicio)]
    actividad = [rng.paretovariate(1.2) for _ in c.participantes]
    lineas, verdad = [], {"mensajes": [], "imagenes": [], "audios": [], "videos": [], "documentos": []}
    num_linea, num_msg, k_adj = 1, 0, 0

    def escribir(texto_linea: str, f: datetime):
        nonlocal num_linea, num_msg
        num_msg += 1
        pos_linea = num_linea
        lineas.append(prefijo(c.formato, f) + texto_linea)
        num_linea += texto_linea.count("\n") + 1
        return pos_linea, num_msg

    # --- encabezado de sistema (Android)
    if c.formato != "ios":
        escribir("Los mensajes y las llamadas están cifrados de extremo a extremo. Solo las personas en este chat "
                 "pueden leerlos, escucharlos o compartirlos. Más información", tiempos[0])
        if c.grupo:
            escribir(f'{c.participantes[0]} creó el grupo "{c.nombre}"', tiempos[0])
            for p in c.participantes[1:]:
                escribir(f"{c.participantes[0]} añadió a {p}", tiempos[0])

    autor = c.participantes[0]
    for (tipo, dato), f in zip(eventos, tiempos[1:]):
        if rng.random() > 0.35:
            autor = rng.choices(c.participantes, weights=actividad)[0]
        if tipo == "normal":
            escribir(f"{autor}: {mensaje_normal(rng, c, tema)}", f)
        elif tipo in ("positivo", "negativo"):
            linea, idm = escribir(f"{autor}: {dato['texto']}", f)
            verdad["mensajes"].append({"linea": linea, "id": idm, "autor": autor, "fecha_hora": f.isoformat(),
                                       "etiqueta": "positivo" if tipo == "positivo" else "negativo",
                                       "categoria": dato["categoria"], "fuente": dato["fuente"],
                                       "id_fuente": dato["id"], "etiqueta_original": dato["etiqueta_original"],
                                       "texto": dato["texto"]})
        else:
            clase, item = dato
            k_adj += 1
            if clase == "imagen":
                nombre = nombre_adjunto(c.formato, "imagen", f, k_adj, ".jpg")
                shutil.copy2(FUENTES / "imagenes_openimages" / "jpg" / f"{item['image_id']}.jpg", carpeta / nombre)
                pie = rng.choice(C.PIES_FOTO)
                linea, idm = escribir(f"{autor}: {linea_adjunto(c.formato, nombre, pie)}", f)
                verdad["imagenes"].append({"linea": linea, "id": idm, "archivo": nombre, "positivo": item["positivo"],
                                           "clases_positivas": "|".join(item["clases_positivas"]),
                                           "image_id": item["image_id"], "autor_imagen": item["autor"],
                                           "licencia": item["licencia"], "url": item["url_original"]})
            elif clase == "voz":
                nombre = nombre_adjunto(c.formato, "voz", f, k_adj, ".opus")
                tareas_audio.append(([p["archivo"] for p in item["clips"]], carpeta / nombre))
                linea, idm = escribir(f"{autor}: {linea_adjunto(c.formato, nombre, '')}", f)
                verdad["audios"].append({"linea": linea, "id": idm, "archivo": nombre, "segundos": item["segundos"],
                                         "positivo": item["positivo"], "categoria": item["categoria"],
                                         "transcripcion": item["transcripcion"], "fuente": item["fuente"]})
            elif clase == "video":
                ext = Path(item["archivo"]).suffix
                nombre = nombre_adjunto(c.formato, "video", f, k_adj, ext)
                enlazar(FUENTES / "videos" / item["archivo"], carpeta / nombre)
                linea, idm = escribir(f"{autor}: {linea_adjunto(c.formato, nombre, '')}", f)
                verdad["videos"].append({"linea": linea, "id": idm, "archivo": nombre, "titulo": item["titulo"],
                                         "bytes": item["bytes"], "licencia": item["licencia"], "autor_video": item["autor"]})
            else:
                nombre_doc, plantilla = item
                nombre = f"{Path(nombre_doc).stem}_{c.n:03d}.pdf"
                (carpeta / nombre).write_bytes(pdf_simple([rellenar(rng, l, c) for l in plantilla]))
                linea, idm = escribir(f"{autor}: {linea_adjunto(c.formato, nombre, '')}", f)
                verdad["documentos"].append({"linea": linea, "id": idm, "archivo": nombre})

    chat = carpeta / ("_chat.txt" if c.formato == "ios" else f"Chat de WhatsApp con {c.nombre}.txt")
    chat.write_text("\n".join(lineas) + "\n", encoding="utf-8")
    return {"caso": c, "carpeta": carpeta, "chat": chat, "verdad": verdad, "mensajes_escritos": num_msg}


# ============================================================ verificación
def verificar(r: dict) -> list[str]:
    """Parsea el chat generado y comprueba que el ground truth apunta a los mensajes correctos."""
    errores = []
    msgs = parsear_archivo(r["chat"])
    if len(msgs) != r["mensajes_escritos"]:
        errores.append(f"se escribieron {r['mensajes_escritos']} mensajes y el parser leyó {len(msgs)}")
    por_linea = {m.linea: m for m in msgs}
    for clase in ("mensajes", "imagenes", "audios", "videos", "documentos"):
        for v in r["verdad"][clase]:
            m = por_linea.get(v["linea"])
            if not m or m.id != v["id"]:
                errores.append(f"{clase}: la línea {v['linea']} no coincide con el mensaje #{v['id']}")
            elif clase == "mensajes" and m.texto != v["texto"]:
                errores.append(f"línea {v['linea']}: texto distinto al etiquetado")
            elif clase != "mensajes" and m.adjunto != v["archivo"]:
                errores.append(f"línea {v['linea']}: adjunto {m.adjunto!r} ≠ {v['archivo']!r}")
            elif clase != "mensajes" and not (r["carpeta"] / v["archivo"]).exists():
                errores.append(f"falta el archivo {v['archivo']}")
    return errores


# ============================================================ salida
def escribir_csv(ruta: Path, filas: list[dict]) -> None:
    if not filas:
        ruta.write_text("", encoding="utf-8")
        return
    with open(ruta, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(filas[0]))
        w.writeheader()
        w.writerows(filas)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--salida", type=Path, default=RAIZ / "dataset_generado")
    ap.add_argument("--fuentes", type=Path, default=RAIZ / "fuentes_dataset", help="carpeta de fuentes (por defecto fuentes_dataset/)")
    ap.add_argument("--mensajes", type=int, default=20400, help="mensajes de participantes en total (mín. 20 000)")
    ap.add_argument("--semilla", type=int, default=2026)
    ap.add_argument("--sobrescribir", action="store_true")
    ap.add_argument("--importar", action="store_true", help="importar los casos a dataset_whatsapp/ al terminar")
    ap.add_argument("--destino-importar", type=Path, default=RAIZ / "dataset_whatsapp")
    a = ap.parse_args()
    global FUENTES
    FUENTES = a.fuentes.resolve()
    rng = random.Random(a.semilla)

    salida = a.salida.resolve()
    if salida.exists():
        if not a.sobrescribir:
            sys.exit(f"[✗] Ya existe {salida} (use --sobrescribir)")
        if not (salida / MARCA).exists():
            sys.exit(f"[✗] {salida} no fue creado por este generador; no se borra")
        shutil.rmtree(salida)
    if not shutil.which("ffmpeg"):
        sys.exit("[✗] Falta ffmpeg (se usa para codificar las notas de voz)")

    # --- fuentes
    cat_img = FUENTES / "imagenes_openimages" / "catalogo.json"
    if not cat_img.exists() or not (FUENTES / "audio_fleurs" / "test.tsv").exists():
        sys.exit("[✗] Faltan fuentes. Corra primero: python generador_dataset/descargar_fuentes.py")
    imagenes = json.loads(cat_img.read_text(encoding="utf-8"))
    clips = cargar_fleurs()
    cat_vid = FUENTES / "videos" / "catalogo.json"
    videos = json.loads(cat_vid.read_text(encoding="utf-8")) if cat_vid.exists() else []
    positivos, negativos, notas = cargar_textos()
    grabaciones = cargar_grabaciones()
    print("Fuentes:")
    print(f"  imágenes {len(imagenes)} ({sum(i['positivo'] for i in imagenes)} positivas) · audios FLEURS {len(clips)} · "
          f"videos {len(videos)} · grabaciones del equipo {len(grabaciones)}")
    print(f"  textos: {len(positivos)} positivos, {len(negativos)} negativos de datasets")
    for n in notas:
        print(f"  · {n}")
    if not videos:
        print("  [!] Sin videos: los 5 casos pesados quedan sin su archivo de 200 MB o más "
              "(python generador_dataset/descargar_fuentes.py --solo videos)")

    # --- plan y generación
    notas_voz = notas_de_voz(rng, clips, 150, grabaciones)
    casos = planear(rng, a.mensajes, imagenes, notas_voz, videos)
    salida.mkdir(parents=True)
    (salida / MARCA).touch()
    tareas_audio: list = []
    resultados = [generar_caso(rng, c, salida, positivos, negativos, tareas_audio) for c in casos]
    print(f"Codificando {len(tareas_audio)} notas de voz…")
    with ThreadPoolExecutor(max(2, (os.cpu_count() or 4) // 2)) as ex:
        list(ex.map(lambda t: a_opus(*t), tareas_audio))

    # --- verificación y ground truth
    errores = {r["caso"].n: e for r in resultados if (e := verificar(r))}
    verdad_dir = salida / "verdad"
    verdad_dir.mkdir()
    tablas: dict[str, list] = {k: [] for k in ("mensajes", "imagenes", "audios", "videos", "documentos")}
    filas_casos = []
    for r in resultados:
        c, v = r["caso"], r["verdad"]
        for k in tablas:
            tablas[k] += [{"caso": r["carpeta"].name, **x} for x in v[k]]
        tam = sum(p.stat().st_size for p in r["carpeta"].iterdir())
        filas_casos.append({
            "caso": r["carpeta"].name, "chat": c.nombre, "tipo": c.tipo, "formato": c.formato,
            # heterogéneo de verdad: texto + imagen + audio + (video o documento)
            "heterogeneo": bool(v["imagenes"] and v["audios"] and (v["videos"] or v["documentos"])),
            "prioridad": c.prioridad,
            "con_incidencias": c.incidencias, "participantes": len(c.participantes),
            "mensajes_participantes": c.mensajes, "mensajes_total": r["mensajes_escritos"],
            "imagenes": len(v["imagenes"]), "audios": len(v["audios"]), "videos": len(v["videos"]),
            "documentos": len(v["documentos"]),
            "positivos_texto": sum(x["etiqueta"] == "positivo" for x in v["mensajes"]),
            "positivos_imagen": sum(x["positivo"] for x in v["imagenes"]),
            "positivos_audio": sum(x["positivo"] for x in v["audios"]), "megabytes": round(tam / 2**20, 1)})
    escribir_csv(verdad_dir / "casos.csv", filas_casos)
    for k, filas in tablas.items():
        escribir_csv(verdad_dir / f"{'mensajes_etiquetados' if k == 'mensajes' else k}.csv", filas)

    total_msgs = sum(f["mensajes_participantes"] for f in filas_casos)
    pesados = sum(1 for f in filas_casos if f["videos"] and f["megabytes"] >= 200)
    minimos = {
        "casos >= 40": len(casos) >= 40,
        "heterogeneos >= 10": sum(f["heterogeneo"] for f in filas_casos) >= 10,
        "mensajes >= 20000": total_msgs >= 20000,
        "imagenes >= 300": len(tablas["imagenes"]) >= 300,
        "audios >= 150": len(tablas["audios"]) >= 150,
        "casos pesados (video, >= 200 MB) >= 5": pesados >= 5,
        "ground truth": bool(tablas["mensajes"] and tablas["imagenes"]),
    }
    resumen = {"semilla": a.semilla, "generado": datetime.now().isoformat(timespec="seconds"),
               "casos": len(casos), "mensajes_participantes": total_msgs,
               "imagenes": len(tablas["imagenes"]), "imagenes_positivas": sum(x["positivo"] for x in tablas["imagenes"]),
               "audios": len(tablas["audios"]), "audios_positivos": sum(x["positivo"] for x in tablas["audios"]),
               "videos": len(tablas["videos"]), "documentos": len(tablas["documentos"]),
               "mensajes_positivos": sum(x["etiqueta"] == "positivo" for x in tablas["mensajes"]),
               "positivos_por_categoria": dict(Counter(x["categoria"] for x in tablas["mensajes"] if x["etiqueta"] == "positivo")),
               "negativos_de_datasets": sum(x["etiqueta"] == "negativo" for x in tablas["mensajes"]),
               "formatos": dict(Counter(f["formato"] for f in filas_casos)),
               "megabytes": round(sum(f["megabytes"] for f in filas_casos), 1),
               "fuentes_texto": sorted({x["fuente"] for x in tablas["mensajes"]}),
               "cumple_b3": minimos, "errores_verificacion": errores}
    (verdad_dir / "resumen.json").write_text(json.dumps(resumen, ensure_ascii=False, indent=2), encoding="utf-8")
    escribir_atribuciones(salida, imagenes, videos)

    print(f"\n[✓] {len(casos)} casos en {salida.relative_to(RAIZ) if salida.is_relative_to(RAIZ) else salida}/casos/  ({resumen['megabytes']} MB)")
    print(f"    {total_msgs} mensajes · {resumen['mensajes_positivos']} positivos {resumen['positivos_por_categoria']}")
    print(f"    {resumen['imagenes']} imágenes ({resumen['imagenes_positivas']} positivas) · {resumen['audios']} notas de voz "
          f"· {resumen['videos']} videos · {resumen['documentos']} documentos · formatos {resumen['formatos']}")
    for k, ok in minimos.items():
        print(f"    {'✓' if ok else '✗'} {k}")
    if errores:
        print(f"[✗] Verificación con el parser: errores en {len(errores)} casos (ver verdad/resumen.json)")
    else:
        print("    ✓ todos los chats se parsean y el ground truth coincide línea por línea")

    if a.importar:
        importar_casos(resultados, a.destino_importar.resolve())


def escribir_atribuciones(salida: Path, imagenes: list, videos: list) -> None:
    lineas = ["# Atribuciones", "",
              "- Audio: **FLEURS** (Google), configuración es_419, split test. Licencia CC BY 4.0. "
              "https://huggingface.co/datasets/google/fleurs", ""]
    for v in videos:
        lineas.append(f"- Video: **{v['titulo']}**, {v['autor']}. Licencia {v['licencia']}. {v['url']}")
    lineas += ["", "## Imágenes (Open Images V7, split validation)", "",
               "Anotaciones © Google, CC BY 4.0. Cada imagen con su autor y licencia original:", "",
               "| Imagen | Autor | Licencia | Fuente |", "|---|---|---|---|"]
    for i in imagenes:
        autor = (i["autor"] or "").replace("|", "/")
        lineas.append(f"| {i['image_id']} | {autor} | {i['licencia']} | {i['url_original']} |")
    (salida / "ATRIBUCIONES.md").write_text("\n".join(lineas) + "\n", encoding="utf-8")


def importar_casos(resultados: list, destino: Path) -> None:
    from importar import ErrorImportacion, importar
    from organizacion import ErrorOrganizacion, Organizacion
    org = Organizacion(destino)
    destino.mkdir(exist_ok=True)
    try:
        carpeta = org.crear("Dataset sintético")["id"]
    except ErrorOrganizacion:
        carpeta = next(c["id"] for c in org.estado()["carpetas"] if c["nombre"] == "Dataset sintético")
    print(f"\nImportando {len(resultados)} casos a {destino} …")
    for r in resultados:
        try:
            # iOS exporta "_chat.txt" dentro de "WhatsApp Chat - <nombre>.zip": el nombre sale del zip
            entrada = r["chat"].name if r["caso"].formato != "ios" else f"WhatsApp Chat - {r['caso'].nombre}.zip"
            caso = importar(r["carpeta"], destino, fuente="generador", declaro=False, nombre=r["carpeta"].name,
                            prioridad=r["caso"].prioridad, sobrescribir=True, nombre_entrada=entrada)
            org.mover([caso.name], carpeta)
        except ErrorImportacion as e:
            print(f"  [✗] {r['carpeta'].name}: {e}")
    print(f"  listo: carpeta \"Dataset sintético\" en la interfaz")


if __name__ == "__main__":
    main()
