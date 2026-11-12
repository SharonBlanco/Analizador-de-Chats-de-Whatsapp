"""
M5 · Cómputo heterogéneo: latencia, throughput y memoria de cada modelo en CPU y en GPU.

Para cada combinación modelo × dispositivo se lanza un PROCESO NUEVO (la memoria de una no se
mezcla con la de otra), se carga el modelo, se hace una inferencia de calentamiento y se mide:
  · latencia: segundos por elemento (mensaje, foto o audio)
  · throughput: elementos por segundo (y, en audio, cuántas veces más rápido que el tiempo real)
  · RAM: RSS del proceso al final   · VRAM: memoria de la GPU que ocupó (si hay GPU)

  python distribuido/m5/medir_dispositivos.py                 todo (texto, imagen, audio × cpu, cuda)
  python distribuido/m5/medir_dispositivos.py --rapido        con menos datos, para probar
Resultados: docs/mediciones/m5/dispositivos.json y dispositivos.md
Datos: OffendES (texto), Open Images (fotos), FLEURS (audios): solo datos públicos.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from pathlib import Path

R = Path(__file__).resolve().parents[2]


def rss_mb() -> float:
    for l in Path("/proc/self/status").read_text().splitlines():
        if l.startswith("VmRSS:"):
            return round(int(l.split()[1]) / 1024, 1)
    return 0.0


def vram_usada_mb() -> float | None:
    try:
        import torch
        if not torch.cuda.is_available():
            return None
        libre, total = torch.cuda.mem_get_info()
        return (total - libre) / 2**20
    except Exception:  # noqa: BLE001
        return None


def datos(modelo: str, rapido: bool):
    if modelo == "texto":
        filas = list(csv.DictReader(open(R / "fuentes_dataset/texto/offendes/test.csv", encoding="utf-8")))
        col = next(c for c in filas[0] if c.lower() in ("comment", "text", "texto"))
        return [f[col] for f in filas[: 128 if rapido else 512]]
    if modelo == "imagen":
        return [str(p) for p in sorted((R / "fuentes_dataset/imagenes_openimages/jpg").glob("*.jpg"))[: 5 if rapido else 30]]
    return [str(p) for p in sorted((R / "fuentes_dataset/audio_fleurs/wav").glob("*.wav"))[: 3 if rapido else 15]]


def medir(modelo: str, disp: str, rapido: bool) -> dict:
    """Corre dentro de un proceso nuevo."""
    sys.path[:0] = [str(R / "distribuido"), str(R / "distribuido" / "worker")]
    os.environ["WA_DISPOSITIVO"] = disp
    os.environ["WA_WHISPER_DISPOSITIVO"] = disp
    os.environ["WA_WHISPER_MODELO"] = "small"
    entrada = datos(modelo, rapido)
    vram0 = vram_usada_mb()
    import detectores as d
    t0 = time.perf_counter()
    if modelo == "texto":
        fn = lambda: d.analizar_textos(entrada, usar_modelo=True)           # noqa: E731
        d.analizar_textos(entrada[:8], usar_modelo=True)                     # carga + calentamiento
    elif modelo == "imagen":
        fn = lambda: [d.analizar_imagen(f) for f in entrada]                 # noqa: E731
        d.analizar_imagen(entrada[0])
    else:
        fn = lambda: [d.analizar_audio(f) for f in entrada]                  # noqa: E731
        d.analizar_audio(entrada[0])
    carga = time.perf_counter() - t0
    disp_real = next(iter(d.modelos_cargados().values()), {}).get("dispositivo", disp)
    t0 = time.perf_counter()
    salida = fn()
    seg = time.perf_counter() - t0
    r = {"modelo": modelo, "dispositivo": disp, "dispositivo_real": disp_real, "elementos": len(entrada),
         "segundos": round(seg, 3), "latencia_ms": round(1000 * seg / len(entrada), 2),
         "throughput_s": round(len(entrada) / seg, 2), "carga_s": round(carga, 2), "ram_mb": rss_mb()}
    v = vram_usada_mb()
    if v is not None and vram0 is not None:
        r["vram_mb"] = round(v - vram0)
    if modelo == "audio":
        dur = sum(a["duracion_s"] for a, _ in salida)
        r["audio_s"] = round(dur, 1)
        r["veces_tiempo_real"] = round(dur / seg, 1)
    return r


def tabla(filas: list[dict]) -> str:
    nombres = {"texto": "Texto (RoBERTuito)", "imagen": "Fotos (YOLOv8-l)", "audio": "Audio (Whisper small, transcripción)"}
    unidad = {"texto": "mensajes/s", "imagen": "fotos/s", "audio": "audios/s"}
    out = ["| Modelo | Dispositivo | Latencia por elemento | Throughput | RAM del proceso | VRAM | Aceleración GPU/CPU |",
           "|---|---|---|---|---|---|---|"]
    for f in filas:
        if "error" in f:
            out.append(f"| {nombres[f['modelo']]} | {f['dispositivo'].upper()} | — | — | — | — | {f['error']} |")
            continue
        cpu = next((x for x in filas if x["modelo"] == f["modelo"] and x["dispositivo"] == "cpu" and "error" not in x), None)
        acel = f"{cpu['latencia_ms'] / f['latencia_ms']:.1f}×" if cpu and f["dispositivo"] == "cuda" else "—"
        extra = f" ({f['veces_tiempo_real']}× tiempo real)" if "veces_tiempo_real" in f else ""
        out.append(f"| {nombres[f['modelo']]} | {f['dispositivo_real'].upper()} | {f['latencia_ms']} ms | "
                   f"{f['throughput_s']} {unidad[f['modelo']]}{extra} | {f['ram_mb']:.0f} MB | "
                   f"{str(f.get('vram_mb', '—')) + ' MB' if f.get('vram_mb') is not None else '—'} | {acel} |")
    return "\n".join(out)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--uno", nargs=2, metavar=("MODELO", "DISPOSITIVO"))
    ap.add_argument("--modelos", default="texto,imagen,audio")
    ap.add_argument("--dispositivos", default="cpu,cuda")
    ap.add_argument("--rapido", action="store_true")
    ap.add_argument("--salida", type=Path, default=R / "docs" / "mediciones" / "m5")
    a = ap.parse_args()
    if a.uno:
        print("RESULTADO " + json.dumps(medir(a.uno[0], a.uno[1], a.rapido)), flush=True)
        sys.exit(0)
    filas = []
    for m in a.modelos.split(","):
        for disp in a.dispositivos.split(","):
            print(f"[M5] {m} en {disp.upper()}…", flush=True)
            entorno = dict(os.environ)
            if disp == "cpu":
                entorno["CUDA_VISIBLE_DEVICES"] = ""      # CPU de verdad: el proceso no ve la GPU
            if m == "audio":
                entorno["WA_TEXTO_MODELO"] = "0"          # se mide Whisper: el texto de los segmentos, solo con reglas
            p = subprocess.run([sys.executable, __file__, "--uno", m, disp] + (["--rapido"] if a.rapido else []),
                               capture_output=True, text=True, env=entorno)
            linea = next((l for l in p.stdout.splitlines() if l.startswith("RESULTADO ")), None)
            if not linea:
                filas.append({"modelo": m, "dispositivo": disp, "error": f"falló (código {p.returncode})"})
                print(p.stderr[-800:])
                continue
            filas.append(json.loads(linea[10:]))
            f = filas[-1]
            print(f"      {f['latencia_ms']} ms por elemento · {f['throughput_s']}/s · RAM {f['ram_mb']:.0f} MB"
                  + (f" · VRAM {f['vram_mb']} MB" if f.get("vram_mb") is not None else ""), flush=True)
    a.salida.mkdir(parents=True, exist_ok=True)
    (a.salida / "dispositivos.json").write_text(json.dumps(filas, indent=2, ensure_ascii=False), encoding="utf-8")
    (a.salida / "dispositivos.md").write_text(tabla(filas) + "\n", encoding="utf-8")
    print("\n" + tabla(filas))
