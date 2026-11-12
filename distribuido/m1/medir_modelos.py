"""
M1 · Huella de memoria de cada modelo (RSS, PSS y USS).

Cada medición corre en un PROCESO NUEVO (así un modelo no hereda la memoria de otro) y en
tres fases; antes de cada una se reinicia el pico de RSS del proceso (escribir "5" en
/proc/self/clear_refs pone VmHWM = RSS actual), para saber el pico de CADA fase:
  1. importar la librería (torch, transformers, ultralytics, ctranslate2…)
  2. cargar el modelo
  3. una inferencia típica (un bloque de mensajes, una foto, una nota de voz)

Resultado: distribuido/worker/huella_modelos.json, que usa el plan de admisión del worker
(memoria.py), y una tabla Markdown para la ficha.

Se ejecuta DENTRO de la imagen del worker, que es lo que de verdad corre en los nodos
(PyTorch solo CPU). Ver el comando exacto en docs/modulo-M1-memoria.md.
  python medir_modelos.py --medios /medios --salida /salida
"""

from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

MB = 1024


def mem() -> dict:
    d = {}
    for linea in Path("/proc/self/smaps_rollup").read_text().splitlines():
        p = linea.split()
        if len(p) >= 2 and p[0].endswith(":") and p[1].isdigit():
            d[p[0][:-1]] = int(p[1])
    hwm = next(int(l.split()[1]) for l in Path("/proc/self/status").read_text().splitlines() if l.startswith("VmHWM:"))
    return {"rss": round(d["Rss"] / MB, 1), "pss": round(d["Pss"] / MB, 1),
            "uss": round((d.get("Private_Clean", 0) + d.get("Private_Dirty", 0)) / MB, 1), "hwm": round(hwm / MB, 1)}


def reiniciar_pico() -> None:
    Path("/proc/self/clear_refs").write_text("5")


def fase(nombre: str, accion, registro: dict):
    gc.collect()
    reiniciar_pico()
    antes, t0 = mem(), time.perf_counter()
    salida = accion()
    gc.collect()
    despues = mem()
    registro[nombre] = {"segundos": round(time.perf_counter() - t0, 2), "antes": antes, "despues": despues,
                        "delta_rss": round(despues["rss"] - antes["rss"], 1),
                        "delta_pss": round(despues["pss"] - antes["pss"], 1),
                        "pico_sobre_inicio": round(despues["hwm"] - antes["rss"], 1)}
    return salida


TEXTOS = [  # un bloque típico de mensajes (cortos, como en WhatsApp); 256 mensajes
    "hola, ya llegaste?", "jajaja qué risa", "mañana nos vemos a las 3", "sos un idiota, no te soporto",
    "¿quién saca la basura hoy?", "te voy a matar si no venís", "mae qué pereza el examen", "listo, gracias!",
] * 32


def medir(modelo: str, medios: Path) -> dict:
    r: dict = {"modelo": modelo}
    r["inicio"] = mem()
    os.environ.setdefault("OMP_NUM_THREADS", str(os.cpu_count()))
    if modelo == "base":
        # lo que el worker carga siempre (sin modelos): pika, psutil, estadísticas
        fase("importar", lambda: [__import__(m) for m in ("pika", "psutil", "estadisticas", "whatsapp_formato", "comun")], r)
    elif modelo == "torch":
        fase("importar", lambda: __import__("torch"), r)
    elif modelo == "texto":
        fase("importar_torch", lambda: __import__("torch"), r)
        fase("importar", lambda: __import__("pysentimiento"), r)
        from pysentimiento import create_analyzer
        an = fase("cargar", lambda: create_analyzer(task="hate_speech", lang="es"), r)
        fase("inferencia", lambda: an.predict(TEXTOS), r)
        fase("inferencia2", lambda: an.predict(TEXTOS), r)
        r["entrada"] = f"{len(TEXTOS)} mensajes"
    elif modelo.startswith("yolo"):
        fase("importar_torch", lambda: __import__("torch"), r)
        fase("importar", lambda: __import__("ultralytics"), r)
        from ultralytics import YOLO
        pesos = os.environ.get("WA_YOLO_MODELO", "/pesos/yolov8l-oiv7.pt")
        m = fase("cargar", lambda: YOLO(pesos), r)
        fotos = sorted((medios / "imagenes").glob("*.jpg"))[:5]
        fase("inferencia", lambda: [m.predict(str(f), conf=0.10, device="cpu", verbose=False) for f in fotos], r)
        fase("inferencia2", lambda: [m.predict(str(f), conf=0.10, device="cpu", verbose=False) for f in fotos], r)
        r["entrada"] = f"{len(fotos)} fotos de Open Images"
        r["pesos"] = Path(pesos).name
    elif modelo.startswith("whisper-"):
        tam = modelo.split("-", 1)[1]
        fase("importar", lambda: __import__("faster_whisper"), r)
        import numpy as np
        from faster_whisper import WhisperModel
        m = fase("cargar", lambda: WhisperModel(tam, device="cpu", compute_type="int8"), r)
        wavs = sorted((medios / "audios").glob("*.wav"))[:3]

        def transcribir():
            for w in wavs:
                pcm = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(w), "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
                                     capture_output=True, check=True).stdout
                audio = np.frombuffer(pcm, dtype=np.int16).astype(np.float32) / 32768.0
                list(m.transcribe(audio, language="es", beam_size=1)[0])
        fase("inferencia", transcribir, r)
        fase("inferencia2", transcribir, r)
        r["entrada"] = f"{len(wavs)} audios de FLEURS"
    r["final"] = mem()
    return r


def resumir(resultados: dict) -> dict:
    """→ formato de huella_modelos.json (lo que necesita el plan de admisión)."""
    def d(m, f, k="delta_rss"):
        return max(0.0, resultados[m][f][k]) if f in resultados.get(m, {}) else 0.0

    torch = max(d("torch", "importar"), d("texto", "importar_torch"), d("yolo", "importar_torch"))
    comp = {"torch": torch, "pysentimiento": d("texto", "importar"), "ultralytics": d("yolo", "importar")}
    w = [m for m in resultados if m.startswith("whisper-")]
    comp["faster_whisper"] = max((d(m, "importar") for m in w), default=0)
    modelos = {}

    def entrada(clave, origen, requiere):
        """Los pesos se leen con mmap y entran a la RAM en la PRIMERA inferencia (demand paging), así que:
          queda    = lo que el modelo deja residente tras la 1.ª inferencia (memoria fija del modelo)
          trabajo  = pico de la 2.ª inferencia sobre el RSS previo (costo de procesar una sub-tarea)
          primera  = pico de la 1.ª inferencia sobre el RSS tras importar (lo máximo que se llega a pedir)
        carga_mb se ajusta para que carga + trabajo ≥ primera: el plan nunca subestima el arranque."""
        x = resultados[origen]
        base = x["cargar"]["antes"]["rss"]
        queda = x["inferencia"]["despues"]["rss"] - base
        segunda = x.get("inferencia2", x["inferencia"])
        trabajo = max(segunda["despues"]["hwm"] - segunda["antes"]["rss"], 1)
        primera = x["inferencia"]["despues"]["hwm"] - base
        carga = max(queda, x["cargar"]["pico_sobre_inicio"], primera - trabajo)
        modelos[clave] = {"requiere": requiere, "carga_mb": round(carga, 1), "trabajo_mb": round(trabajo, 1),
                          "queda_mb": round(queda, 1), "primera_inferencia_mb": round(primera, 1),
                          "medido_con": x.get("entrada")}
    if "texto" in resultados:
        entrada("texto", "texto", ["torch", "pysentimiento"])
    if "yolo" in resultados:
        entrada("yolo", "yolo", ["torch", "ultralytics"])
    for m in w:
        entrada(m, m, ["faster_whisper"])
    # sin modelo: las reglas de texto y la verificación de adjuntos solo usan unos pocos MB
    modelos["texto-reglas"] = {"requiere": [], "carga_mb": 0, "trabajo_mb": 30, "medido_con": "estimado"}
    modelos["adjuntos"] = {"requiere": [], "carga_mb": 0, "trabajo_mb": 20,
                           "medido_con": "estimado: descarga en bloques de 1 MB + ffprobe"}
    return {"medido": time.strftime("%Y-%m-%d %H:%M"), "equipo": f"{platform.processor() or platform.machine()} · "
            f"{os.cpu_count()} núcleos · Python {platform.python_version()}",
            "base_mb": resultados.get("base", {}).get("final", {}).get("rss"),
            "componentes": {k: round(v, 1) for k, v in comp.items()}, "modelos": modelos, "crudo": resultados}


def tabla(h: dict) -> str:
    r = h["crudo"]
    filas = ["| Modelo | Importar librerías | Al cargar (RSS) | Residente tras 1.ª inferencia | Pico 1.ª inferencia | Pico 2.ª inferencia (trabajo) | Proceso al final RSS / PSS / USS | Entrada |",
             "|---|---|---|---|---|---|---|---|"]
    for m, x in r.items():
        if m in ("base", "torch"):
            continue
        imp = sum(x[f]["delta_rss"] for f in ("importar_torch", "importar") if f in x)
        k = h["modelos"].get(m, {})
        filas.append(f"| {m} | {imp:.0f} MB | +{x['cargar']['delta_rss']:.0f} MB | +{k.get('queda_mb', 0):.0f} MB | "
                     f"+{k.get('primera_inferencia_mb', 0):.0f} MB | +{k.get('trabajo_mb', 0):.0f} MB | "
                     f"{x['final']['rss']:.0f} / {x['final']['pss']:.0f} / {x['final']['uss']:.0f} MB | {x.get('entrada', '')} |")
    return "\n".join(filas)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--uno", help="(interno) mide un solo modelo en este proceso")
    ap.add_argument("--modelos", default="base,torch,texto,yolo,whisper-tiny,whisper-base,whisper-small,whisper-medium")
    ap.add_argument("--medios", default="/medios")
    ap.add_argument("--salida", default="/salida")
    ap.add_argument("--agregar", action="store_true", help="conservar las mediciones previas de otros modelos")
    a = ap.parse_args()
    if a.uno:
        print("RESULTADO " + json.dumps(medir(a.uno, Path(a.medios))))
        sys.exit(0)
    resultados = {}
    previo = Path(a.salida) / "huella_modelos.json"
    if a.agregar and previo.exists():
        resultados = json.loads(previo.read_text(encoding="utf-8")).get("crudo", {})
    for m in a.modelos.split(","):
        print(f"[m1] midiendo {m}…", flush=True)
        p = subprocess.run([sys.executable, __file__, "--uno", m, "--medios", a.medios], capture_output=True, text=True)
        linea = next((l for l in p.stdout.splitlines() if l.startswith("RESULTADO ")), None)
        if p.returncode or not linea:
            print(f"[m1] {m} falló (código {p.returncode}):\n{p.stderr[-1500:]}")
            continue
        resultados[m] = json.loads(linea[len("RESULTADO "):])
        x = resultados[m]
        print(f"     final: RSS {x['final']['rss']} MB · PSS {x['final']['pss']} MB · USS {x['final']['uss']} MB")
    h = resumir(resultados)
    salida = Path(a.salida)
    salida.mkdir(parents=True, exist_ok=True)
    (salida / "huella_modelos.json").write_text(json.dumps(h, indent=2, ensure_ascii=False), encoding="utf-8")
    (salida / "huella_modelos.md").write_text(tabla(h) + "\n", encoding="utf-8")
    print(tabla(h))
