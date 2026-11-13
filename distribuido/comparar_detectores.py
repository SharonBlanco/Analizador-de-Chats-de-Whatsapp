"""
Compara los dos detectores de texto con LOS MISMOS mensajes:
  · anterior: reglas + lista de insultos + pysentimiento/RoBERTuito   (WA_TEXTO_MOTOR=pysentimiento)
  · actual:   Ollama qwen2.5:3b en dos pasadas (reglas solo como candidatos)   (WA_TEXTO_MOTOR=ollama)

Conjuntos:
  offendes   muestra balanceada de OffendES (N ofensivos + N no ofensivos, semilla fija)
  sinteticos chats del dataset generado contra su ground truth (mensajes_etiquetados.csv)
  desafio    frases escritas por el equipo: jerga tica, amenazas, drogas, quejas cotidianas y muletillas

  WA_TEXTO_MOTOR=ollama        python distribuido/comparar_detectores.py
  WA_TEXTO_MOTOR=pysentimiento python distribuido/comparar_detectores.py
  OLLAMA_CANDIDATOS=modelo|ollama|ambos   quién propone los candidatos que Ollama revisa
→ docs/mediciones/comparacion_detectores/<variante>.json  (pysentimiento, ollama, combinado, combinado_ambos)
"""
from __future__ import annotations

import csv
import json
import os
import random
import sys
import time
from pathlib import Path

R = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(R / "distribuido"), str(R / "distribuido" / "worker"), str(R / "mensajeria")]
import detectores as d  # noqa: E402
from whatsapp_formato import parsear_archivo  # noqa: E402

N_OFFENDES = int(os.environ.get("N_OFFENDES", "150"))
N_CASOS = int(os.environ.get("N_CASOS", "6"))

# (texto, ¿debe marcarse?)
DESAFIO = [
    ("te voy a matar, hijueputa", 1), ("sos un idiota", 1), ("nadie te quiere, das asco", 1),
    ("las mujeres solo sirven para la cocina", 1), ("a esos nicas hay que sacarlos a golpes", 1),
    ("tengo un kilo de coca para vender", 1), ("diay, ¿y si le bajamos el cel a ese carajillo?", 1),
    ("sé dónde vive tu hija", 1), ("jajaja todos se burlan de vos en el colegio, gordo asqueroso", 1),
    ("te espero afuera y vas a ver", 1), ("si no me mandás plata publico tus fotos", 1),
    ("compa, ¿tenés mota para el finde?", 1), ("ayer le pegué a mi ex hasta dejarla morada", 1),
    ("vieja tenías que ser, andá a lavar platos", 1), ("mañana te espero a la salida y te quiebro la cara", 1),
    ("vamos a saltar la pulpería de la esquina en la noche", 1), ("callate, puta", 1), ("sos un hijueputa", 1),
    ("necesito la factura de ayer", 0), ("se fue el internet otra vez", 0), ("pura vida, nos vemos mañana", 0),
    ("qué mierda de día", 0), ("mandame el documento porfa", 0), ("¿a qué hora llegás?", 0), ("ya llegué", 0),
    ("qué presa la de hoy", 0), ("me muero de risa 😂", 0), ("hay que pagar el recibo de la luz", 0),
    ("puta, se me olvidó la llave", 0), ("hijueputa, ¡ganamos el partido!", 0), ("esa película de acción estuvo buenísima, matan a todos", 0),
    ("estoy muerta de cansancio", 0), ("llueve horrible", 0), ("diay mae, qué chiva el concierto", 0),
    ("te mato si no venís a la fiesta jajaja", 0), ("se me cayó el cel en el bus", 0),
]


def offendes():
    pos, neg = [], []
    for f in sorted((R / "fuentes_dataset/texto/offendes").glob("*.csv")):
        filas = list(csv.DictReader(open(f, encoding="utf-8")))
        col = next(c for c in filas[0] if c.lower() in ("comment", "text", "texto"))
        et = next(c for c in filas[0] if c.lower() in ("label", "etiqueta"))
        for x in filas:
            (pos if x[et] in ("OFP", "OFG") else neg).append(x[col])
    rnd = random.Random(2026)
    return [(t, 1) for t in rnd.sample(pos, N_OFFENDES)] + [(t, 0) for t in rnd.sample(neg, N_OFFENDES)]


def sinteticos():
    pos = {(x["caso"], x["linea"]) for x in csv.DictReader(open(R / "dataset_generado/verdad/mensajes_etiquetados.csv", encoding="utf-8"))
           if x.get("etiqueta") == "positivo"}
    lotes = []
    for c in sorted((R / "dataset_generado/casos").iterdir())[:N_CASOS]:
        ms = [m for m in parsear_archivo(next(c.glob("*.txt"))) if m.tipo == "texto" and m.texto.strip()]
        lotes.append([(m.texto, int((c.name, str(m.linea)) in pos)) for m in ms])
    return lotes


def medir(nombre, lotes):
    vp = fp = fn = vn = 0
    errores, n, t0 = [], 0, time.perf_counter()
    for lote in lotes:                         # cada lote se analiza junto (como un bloque de un chat)
        for i in range(0, len(lote), 100):
            parte = lote[i:i + 100]
            res = d.analizar_textos([t for t, _ in parte])
            for (t, y), h in zip(parte, res):
                n += 1
                p = int(bool(h))
                vp += p and y; fp += p and not y; fn += (not p) and y; vn += (not p) and (not y)
                if p != y:
                    errores.append({"texto": t[:120], "esperado": y, "obtenido": [x["categoria"] for x in h]})
    seg = time.perf_counter() - t0
    pr = vp / (vp + fp) if vp + fp else 0
    ex = vp / (vp + fn) if vp + fn else 0
    f1 = 2 * pr * ex / (pr + ex) if pr + ex else 0
    r = {"mensajes": n, "vp": vp, "fp": fp, "fn": fn, "vn": vn, "precision": round(pr, 3), "exhaustividad": round(ex, 3),
         "f1": round(f1, 3), "ms_por_mensaje": round(1000 * seg / max(1, n), 1), "errores": errores}
    print(f"{nombre:<11} n={n:<5} P={pr:.2f} E={ex:.2f} F1={f1:.2f}  FP={fp:<3} FN={fn:<3} {1000*seg/max(1,n):.1f} ms/msg", flush=True)
    return r


if __name__ == "__main__":
    motor = d.motor_texto()
    print(f"motor: {motor}  dispositivo: {d.dispositivo()}")
    d.analizar_textos(["hola"])                # cargar el modelo antes de medir tiempos
    salida = {"motor": motor, "modelo": d.OLLAMA_MODELO if motor == "ollama" else "pysentimiento hate_speech + reglas",
              "dispositivo": d.dispositivo(),
              "desafio": medir("desafio", [DESAFIO]),
              "sinteticos": medir("sinteticos", sinteticos()),
              "offendes": medir("offendes", [offendes()])}
    dest = R / "docs" / "mediciones" / "comparacion_detectores"
    dest.mkdir(parents=True, exist_ok=True)
    nombre = motor if motor != "ollama" else {"ollama": "ollama", "modelo": "combinado", "ambos": "combinado_ambos"}[d.OLLAMA_CANDIDATOS]
    salida["variante"] = nombre
    (dest / f"{nombre}.json").write_text(json.dumps(salida, indent=2, ensure_ascii=False), encoding="utf-8")
    print("→", dest / f"{nombre}.json")
