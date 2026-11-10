"""
Descarga las fuentes públicas del dataset (enunciado B.3) en fuentes_dataset/.

  audio    FLEURS es_419, split test (Google, CC BY 4.0): 908 frases leídas con transcripción
  imagen   Open Images V7, split validation (Google; imágenes CC BY 2.0, anotaciones CC BY 4.0):
           300 imágenes, 60 con armas (positivos) y 240 sin ellas
  video    Películas abiertas de Blender (CC BY): archivos de 263 MB a 1,1 GB para los casos pesados
  texto    OffendES y HatEval se piden con un formulario: se descargan a mano y se dejan en
           fuentes_dataset/texto/ (este script dice dónde y revisa que estén)

Se puede interrumpir y volver a correr: lo que ya está completo no se baja de nuevo.

Uso:
  python generador_dataset/descargar_fuentes.py                 # todo
  python generador_dataset/descargar_fuentes.py --sin-videos    # sin los ~2,2 GB de videos
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import tarfile
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
DESTINO = RAIZ / "fuentes_dataset"
AGENTE = {"User-Agent": "generador-dataset-academico/1.0"}
SEMILLA = 2026

FLEURS = "https://huggingface.co/datasets/google/fleurs/resolve/main/data/es_419"
OPENIMAGES = {
    "clases": "https://storage.googleapis.com/openimages/v7/oidv7-class-descriptions-boxable.csv",
    "cajas": "https://storage.googleapis.com/openimages/v5/validation-annotations-bbox.csv",
    "imagenes": "https://storage.googleapis.com/openimages/2018_04/validation/validation-images-with-rotation.csv",
}
URL_IMAGEN = "https://open-images-dataset.s3.amazonaws.com/validation/{}.jpg"
CLASES_POSITIVAS = ["Handgun", "Rifle", "Shotgun", "Knife", "Kitchen knife", "Dagger", "Sword", "Weapon"]
N_POSITIVAS, N_NEGATIVAS = 60, 240

VIDEOS = [  # (archivo, url, título, licencia, autor)
    ("ElephantsDream.avi", "https://download.blender.org/ED/ED_1024.avi",
     "Elephants Dream", "CC BY 2.5", "Orange Open Movie Project / Blender Foundation"),
    ("TearsOfSteel_720p.mov", "https://download.blender.org/demo/movies/ToS/tears_of_steel_720p.mov",
     "Tears of Steel", "CC BY 3.0", "Blender Foundation (mango.blender.org)"),
    ("BigBuckBunny_1080p.mp4.zip", "https://download.blender.org/demo/movies/BBB/bbb_sunflower_1080p_30fps_normal.mp4.zip",
     "Big Buck Bunny", "CC BY 3.0", "Blender Foundation (peach.blender.org)"),
    ("Sintel_1080p.mkv", "https://download.blender.org/demo/movies/Sintel.2010.1080p.mkv",
     "Sintel", "CC BY 3.0", "Blender Foundation (durian.blender.org)"),
]


def bajar(url: str, destino: Path, etiqueta: str | None = None) -> Path:
    """Descarga con reanudación (Range) y barra de avance en la terminal."""
    destino.parent.mkdir(parents=True, exist_ok=True)
    completo = destino.with_name(destino.name + ".ok")
    if destino.exists() and completo.exists():
        return destino
    parcial = destino.with_name(destino.name + ".part")
    ya = parcial.stat().st_size if parcial.exists() else 0
    pedido = urllib.request.Request(url, headers={**AGENTE, **({"Range": f"bytes={ya}-"} if ya else {})})
    with urllib.request.urlopen(pedido, timeout=60) as r:
        if ya and r.status != 206:          # el servidor no admite reanudar: empezar de cero
            ya = 0
        total = ya + int(r.headers.get("Content-Length") or 0)
        with open(parcial, "ab" if ya else "wb") as f:
            hecho = ya
            while bloque := r.read(1024 * 1024):
                f.write(bloque)
                hecho += len(bloque)
                if etiqueta and total:
                    print(f"\r  {etiqueta}: {hecho / 2**20:7.1f} / {total / 2**20:.1f} MB", end="", flush=True)
    if etiqueta:
        print()
    parcial.replace(destino)
    completo.touch()
    return destino


# ============================================================ audio
def audio() -> None:
    print("[audio] FLEURS es_419 (test)")
    d = DESTINO / "audio_fleurs"
    tsv = bajar(f"{FLEURS}/test.tsv", d / "test.tsv")
    wavs = d / "wav"
    if wavs.is_dir() and len(list(wavs.glob("*.wav"))) >= 900:
        print(f"  ya extraído: {len(list(wavs.glob('*.wav')))} audios")
        return
    tar = bajar(f"{FLEURS}/audio/test.tar.gz", d / "test.tar.gz", "test.tar.gz")
    wavs.mkdir(exist_ok=True)
    with tarfile.open(tar) as t:
        for m in t:
            if m.isfile() and m.name.endswith(".wav"):
                m.name = Path(m.name).name          # sin carpetas (y sin rutas raras)
                t.extract(m, wavs, filter="data")
    tar.unlink()                                     # 555 MB que ya no hacen falta
    n = sum(1 for _ in open(tsv, encoding="utf-8"))
    print(f"  {len(list(wavs.glob('*.wav')))} audios extraídos, {n} filas en test.tsv")


# ============================================================ imágenes
def imagenes() -> None:
    print("[imagen] Open Images V7 (validation)")
    d = DESTINO / "imagenes_openimages"
    archivos = {k: bajar(u, d / "csv" / Path(u).name, Path(u).name) for k, u in OPENIMAGES.items()}
    clases = {}
    with open(archivos["clases"], encoding="utf-8") as f:
        for fila in csv.reader(f):
            clases[fila[0]] = fila[1]
    positivas = {mid for mid, nombre in clases.items() if nombre in CLASES_POSITIVAS}

    etiquetas: dict[str, set[str]] = {}
    with open(archivos["cajas"], encoding="utf-8") as f:
        for fila in csv.DictReader(f):
            etiquetas.setdefault(fila["ImageID"], set()).add(fila["LabelName"])
    licencias = {}
    with open(archivos["imagenes"], encoding="utf-8") as f:
        for fila in csv.DictReader(f):
            if "creativecommons.org/licenses/by/" in fila["License"]:     # solo CC BY
                licencias[fila["ImageID"]] = fila

    candidatas = sorted(i for i in etiquetas if i in licencias)
    con_arma = [i for i in candidatas if etiquetas[i] & positivas]
    sin_arma = [i for i in candidatas if not etiquetas[i] & positivas]
    azar = random.Random(SEMILLA)
    elegidas = [(i, True) for i in azar.sample(con_arma, min(N_POSITIVAS, len(con_arma)))] + \
               [(i, False) for i in azar.sample(sin_arma, N_NEGATIVAS)]
    print(f"  candidatas CC BY: {len(con_arma)} con armas, {len(sin_arma)} sin armas → "
          f"{sum(p for _, p in elegidas)} positivas + {sum(not p for _, p in elegidas)} negativas")

    carpeta = d / "jpg"
    def una(par):
        i, _ = par
        try:
            bajar(URL_IMAGEN.format(i), carpeta / f"{i}.jpg")
            return True
        except Exception as e:  # noqa: BLE001
            print(f"  [!] {i}: {e}")
            return False
    with ThreadPoolExecutor(8) as ex:
        ok = list(ex.map(una, elegidas))
    catalogo = []
    for (i, pos), bien in zip(elegidas, ok):
        if not bien:
            continue
        lic = licencias[i]
        catalogo.append({"image_id": i, "positivo": pos,
                         "clases": sorted(clases.get(c, c) for c in etiquetas[i]),
                         "clases_positivas": sorted(clases[c] for c in etiquetas[i] & positivas),
                         "licencia": lic["License"], "autor": lic["Author"], "titulo": lic["Title"],
                         "url_original": lic["OriginalLandingURL"]})
    (d / "catalogo.json").write_text(json.dumps(catalogo, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"  {len(catalogo)} imágenes listas ({sum(c['positivo'] for c in catalogo)} positivas)")


# ============================================================ videos
def videos() -> None:
    print("[video] Películas abiertas de Blender")
    d = DESTINO / "videos"
    catalogo = []
    for nombre, url, titulo, licencia, autor in VIDEOS:
        ruta = bajar(url, d / nombre, nombre)
        if ruta.suffix == ".zip":
            mp4 = ruta.with_suffix("")
            if not mp4.exists():
                with zipfile.ZipFile(ruta) as z:
                    interno = next(n for n in z.namelist() if n.endswith(".mp4"))
                    with z.open(interno) as src, open(mp4, "wb") as dst:
                        while b := src.read(1024 * 1024):
                            dst.write(b)
            ruta = mp4
        catalogo.append({"archivo": ruta.name, "titulo": titulo, "licencia": licencia, "autor": autor,
                         "url": url, "bytes": ruta.stat().st_size})
    (d / "catalogo.json").write_text(json.dumps(catalogo, ensure_ascii=False, indent=1), encoding="utf-8")
    for v in catalogo:
        print(f"  {v['archivo']}: {v['bytes'] / 2**20:.0f} MB ({v['licencia']})")


# ============================================================ textos con formulario
def textos() -> None:
    d = DESTINO / "texto"
    d.mkdir(parents=True, exist_ok=True)
    print("[texto] Datasets con formulario (se descargan a mano):")
    for nombre, carpeta, url in [
        ("OffendES (SINAI, Universidad de Jaén)", "offendes", "https://huggingface.co/datasets/SINAI/OffendES"),
        ("HatEval 2019, parte en español", "hateval", "https://hatespeech.di.unito.it/hateval.html"),
    ]:
        c = d / carpeta
        c.mkdir(exist_ok=True)
        archivos = [p for p in c.iterdir() if p.suffix.lower() in (".csv", ".tsv", ".json", ".jsonl", ".parquet")]
        estado = f"✓ {len(archivos)} archivo(s)" if archivos else "falta"
        extra = "\n    luego: python generador_dataset/descargar_offendes.py" if carpeta == "offendes" else ""
        print(f"  {nombre}: {estado}\n    pedirlo en {url}{extra}\n    y dejar los .csv/.tsv/.jsonl en {c.relative_to(RAIZ)}/")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sin-videos", action="store_true", help="no bajar los videos pesados (~2,2 GB)")
    ap.add_argument("--solo", choices=["audio", "imagenes", "videos", "textos"])
    a = ap.parse_args()
    pasos = {"audio": audio, "imagenes": imagenes, "videos": videos, "textos": textos}
    for nombre, paso in pasos.items():
        if (a.solo and nombre != a.solo) or (nombre == "videos" and a.sin_videos and a.solo != "videos"):
            continue
        try:
            paso()
        except Exception as e:  # noqa: BLE001
            print(f"\n[✗] {nombre}: {type(e).__name__}: {e}\n    Vuelva a correr el script para reanudar.")
            sys.exit(1)
    print(f"\nFuentes en {DESTINO.relative_to(RAIZ)}/. Siguiente paso: python generador_dataset/generar.py")


if __name__ == "__main__":
    main()
