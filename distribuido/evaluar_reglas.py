"""
Evalúa reglas CANDIDATAS del detector de texto antes de agregarlas (para no repetir el error de "basura").

Cada candidata se prueba por separado contra:
  · comentarios NO ofensivos de OffendES (train + validation + test): falsas alarmas
  · mensajes normales de los chats sintéticos (dataset_generado/): falsas alarmas en chats tipo WhatsApp
  · comentarios ofensivos de OffendES: cuántos encuentra que las reglas actuales NO encontraban (ganancia)
Se acepta una candidata si no genera más de MAX_FP falsas alarmas y ninguna en los chats sintéticos.

  python distribuido/evaluar_reglas.py            → tabla de candidatas y la lista de aceptadas
"""

from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

R = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(R / "distribuido"), str(R / "distribuido" / "worker"), str(R / "mensajeria")]
import detectores as d  # noqa: E402
from whatsapp_formato import parsear_archivo  # noqa: E402

MAX_FP = 2

CANDIDATAS = {
    "insulto": [
        "idiotas", "imbeciles", "estupidos", "estupidas", "inutiles", "tarados", "taradas", "pendejos", "pendejas",
        "huevones", "cabrones", "putas", "perras", "zorras", "maricones", "payasos", "ridiculos", "fracasados",
        "fracasadas", "retrasados", "parasitos", "ignorantes", "asquerosos", "lacras", "baboso", "babosa", "babosos",
        "mamon", "mamona", "malnacido", "malnacida", "malnacidos", "hijueputas", "hijos de puta", "hijo e puta",
        "carepicha", "carapicha", "mamaguevo", "mamahuevo", "mamahuevos", "comemierda", "comemierdas", "culero",
        "culera", "culeros", "cretino", "cretina", "tarupido", "tarupida", "mongolo", "mongola", "anormal", "anormales",
        "lameculos", "chupamedias", "puto", "putos", "joto", "jotos", "playo", "playos", "travelo", "machorra",
        "sudaca", "sudacas", "malparidos", "pedazo de mierda", "saco de mierda", "bueno para nada", "buena para nada",
        "muerto de hambre", "muerta de hambre", "cerebro de mosquito",
    ],
    "dano": ["torturar", "envenenar", "linchar", "descuartizar", "estrangular", "fusilar", "acribillar", "rematar",
             "volar los sesos", "volarle los sesos", "volarle la cabeza", "sacar los ojos", "sacarle los ojos",
             "mandar al hospital", "mandarte al hospital", "cortar la cara", "cortarle la cara", "desfigurar",
             "dejar en silla de ruedas", "dejarte en silla de ruedas", "quebrar", "pelar"],
    "amenaza": [
        r"\bte quiero (ver )?(muerto|muerta|bajo tierra)\b",
        r"\bno (sabes|sabis|sabe) con quien te (metiste|estas metiendo)\b",
        r"\bte (voy|vamos) a hacer la vida (imposible|un infierno|cuadritos)\b",
        r"\b(voy|vamos) a hacer que te arrepientas\b",
        r"\bte va a pesar\b",
        r"\bte (tengo|tenemos) (vigilado|vigilada|fichado|fichada)\b|\bte (estoy|estamos) vigilando\b",
        r"\bcuid(a|ate|ese) a tu (familia|mama|hija|hijo|novia|esposa)\b",
        r"\bsi no\b.{0,40}\b(publico|subo|paso|mando|reenvio) (tus|las|esas) (fotos|videos|nudes)\b",
        r"\b(voy|vamos) a (publicar|subir|filtrar) tus (fotos|videos|nudes)\b",
        r"\b(vas|van) a amanecer (muerto|muerta|con la boca llena de moscas|en una zanja)\b",
        r"\bte (voy|vamos) a mandar a matar\b|\bte mando a matar\b",
        r"\bte (voy|vamos) a dejar (como coladera|sin dientes|tieso|tiesa)\b",
    ],
    "ilicito": [
        r"\b(lavar|lavo|lavamos|lavando) (plata|dinero|la plata|el dinero)\b|\blavado de (dinero|activos)\b",
        r"\b(falsificar|falsifico|falsificamos|falsificando) (documentos|billetes|la cedula|cedulas|firmas|la firma|pasaportes)\b",
        r"\b(trafico|trata) de (personas|armas|organos)\b",
        r"\b(bajarle|bajarte|le bajamos|le bajaron) el (cel|celular|telefono|carro|bolso)\b",
        r"\b(vender|vendo|vende|vendemos|venden|conseguir|consigo|comprar|compro)\b.{0,25}\b(monte|porro|porros|tachas|molly|lsd|ketamina|keta|anfetaminas|bareta)\b",
        r"\b(vender|vendo|vende|vendemos|conseguir|consigo|comprar|compro)\b.{0,20}\b(cuete|cuetes|fierro|revolver|escopeta|chaleco antibalas)\b",
        r"\b(pagar|pague|pagamos) (un|a un) sicario\b",
        r"\bhay que (desaparecer|quitar del medio) a\b",
    ],
}


def cargar_offendes():
    pos, neg = [], []
    for f in sorted((R / "fuentes_dataset/texto/offendes").glob("*.csv")):
        filas = list(csv.DictReader(open(f, encoding="utf-8")))
        col = next(c for c in filas[0] if c.lower() in ("comment", "text", "texto"))
        et = next(c for c in filas[0] if c.lower() in ("label", "etiqueta"))
        for x in filas:
            (pos if x[et] in ("OFP", "OFG") else neg).append(x[col])
    return pos, neg


def cargar_sinteticos():
    etiquetados = {(x["caso"], x["linea"]) for x in csv.DictReader(open(R / "dataset_generado/verdad/mensajes_etiquetados.csv", encoding="utf-8"))
                   if x.get("etiqueta") == "positivo"}
    textos = []
    for caso in sorted((R / "dataset_generado/casos").iterdir()):
        chat = next((p for p in caso.glob("*.txt")), None)
        if not chat:
            continue
        for m in parsear_archivo(chat):
            if m.tipo == "texto" and (caso.name, str(m.linea)) not in etiquetados:
                textos.append(m.texto)
    return textos


def compilar(cat: str, c: str):
    if cat == "insulto":
        return re.compile(rf"\b{re.escape(c)}\b")
    if cat == "dano":
        # mismo contexto que las reglas actuales: "voy a / vamos a / te van a + verbo"
        return re.compile(rf"\b(?<!me )(?<!nos )(?:(?:te|lo|la|los|las|le|les)\s+)?(?:voy|vamos|pienso|quiero|queremos)\s+a?\s*{c}\b"
                          rf"|\bte\s+van\s+a\s+{c}\b")
    return re.compile(c)


if __name__ == "__main__":
    pos, neg = cargar_offendes()
    sint = cargar_sinteticos()
    pos_l, neg_l, sint_l = [d._sin_tildes(t) for t in pos], [d._sin_tildes(t) for t in neg], [d._sin_tildes(t) for t in sint]
    ya = set(i for i, h in enumerate(d.analizar_textos(pos, usar_modelo=False)) if h)
    print(f"OffendES: {len(neg)} no ofensivos · {len(pos)} ofensivos ({len(ya)} ya detectados por las reglas) · "
          f"sintéticos normales: {len(sint)}\n")
    aceptadas, filas = {k: [] for k in CANDIDATAS}, []
    for cat, lista in CANDIDATAS.items():
        for c in lista:
            rx = compilar(cat, c)
            fp = [t for t in neg_l if rx.search(t)]
            fps = [t for t in sint_l if rx.search(t)]
            gana = sum(1 for i, t in enumerate(pos_l) if i not in ya and rx.search(t))
            ok = len(fp) <= MAX_FP and not fps
            filas.append({"categoria": cat, "candidata": c, "falsas_offendes": len(fp), "falsas_sinteticos": len(fps),
                          "nuevos_ofensivos": gana, "aceptada": ok, "ejemplo_falsa": (fp or fps or [""])[0][:90]})
            if ok:
                aceptadas[cat].append(c)
    for f in filas:
        marca = "✓" if f["aceptada"] else "✗"
        print(f"{marca} {f['categoria']:>8} | {f['candidata'][:55]:<55} | FP OffendES {f['falsas_offendes']:>3} | "
              f"FP chats {f['falsas_sinteticos']:>2} | +{f['nuevos_ofensivos']:>3}" + (f"  «{f['ejemplo_falsa']}»" if not f["aceptada"] else ""))
    salida = R / "docs" / "mediciones" / "reglas_candidatas.json"
    salida.write_text(json.dumps({"max_falsas": MAX_FP, "filas": filas, "aceptadas": aceptadas}, indent=2, ensure_ascii=False),
                      encoding="utf-8")
    print(f"\naceptadas: { {k: len(v) for k, v in aceptadas.items()} } → {salida}")
