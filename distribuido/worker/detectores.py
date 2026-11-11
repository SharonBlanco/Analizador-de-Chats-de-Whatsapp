"""
Detectores de agresiones para los workers (texto, imagen y audio). Todo gratuito y local.

  Texto   1) reglas de amenaza (expresiones regulares)          → amenaza  (alta)
          2) pysentimiento / RoBERTuito hate_speech (HatEval):
             probabilidad de "agresivo" y de "odio" (incluye
             discurso machista y xenófobo)                     → agresion, odio (media)
          3) lista de insultos                                   → insulto  (media)
  Imagen  YOLOv8 entrenado con Open Images V7 (Ultralytics):
          pistolas, rifles, escopetas, cuchillos, espadas…       → arma     (alta)
  Audio   faster-whisper (Whisper) transcribe por segmentos con
          su hora; cada segmento pasa por el detector de texto   → según el texto

Cada hallazgo dice POR QUÉ se marcó (regla, palabra o probabilidad del modelo).

Los modelos se cargan una sola vez por proceso, la primera vez que se usan, y se
registra cuánto tardaron y cuánta memoria agregaron (evidencia para el módulo M1).

Configuración (variables de entorno o distribuido/config.env):
  WA_DISPOSITIVO=auto|cpu|cuda      dónde corren los modelos (auto: GPU si hay)
  WA_WHISPER_MODELO=small           tiny | base | small | medium
  WA_WHISPER_PRECISION=auto         int8 | float16 | float32 (auto: float16 en GPU, int8 en CPU)
  WA_YOLO_MODELO=yolov8l-oiv7.pt    yolov8{n,s,m,l,x}-oiv7.pt (l: mejor F1, ver docs/mediciones/imagen_yolo.md)
  WA_TEXTO_MODELO=1                  0 = texto solo con reglas e insultos (sin modelo: ~100 veces más rápido)
  WA_UMBRAL_TEXTO=0.5  WA_UMBRAL_IMAGEN=0.10
"""

from __future__ import annotations

import ctypes
import glob
import os
import re
import subprocess
import sys
import threading
import time
import unicodedata

try:
    import psutil
except ImportError:
    psutil = None

from comun import config

SEVERIDAD = {"amenaza": "alta", "arma": "alta", "agresion": "media", "odio": "media", "insulto": "media"}
UMBRAL_TEXTO = float(config("WA_UMBRAL_TEXTO", "0.5"))
UMBRAL_IMAGEN = float(config("WA_UMBRAL_IMAGEN", "0.10"))   # elegido con docs/mediciones/imagen_yolo.md
CLASES_ARMA = {"Handgun", "Rifle", "Shotgun", "Weapon", "Knife", "Kitchen knife", "Dagger", "Sword"}


class DeteccionNoDisponible(Exception):
    """Falta una librería o el modelo no se pudo cargar."""


def _sin_tildes(t: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", t.lower()) if unicodedata.category(c) != "Mn")


def _rss_mb() -> float | None:
    return round(psutil.Process().memory_info().rss / 2 ** 20, 1) if psutil else None


def dispositivo() -> str:
    pedido = config("WA_DISPOSITIVO", "auto")
    if pedido != "auto":
        return pedido
    try:
        import torch
        return "cuda" if torch.cuda.is_available() else "cpu"
    except ImportError:
        return "cpu"


# ============================================================ carga perezosa de modelos
_modelos: dict[str, object] = {}
_cargas: dict[str, dict] = {}             # nombre → {segundos, rss_antes_mb, rss_despues_mb, dispositivo}
_disp_real: dict[str, str] = {}           # dónde terminó corriendo cada modelo (si tuvo que pasar a CPU)
_candado = threading.Lock()


def _cargar(nombre: str, fabrica):
    with _candado:
        if nombre not in _modelos:
            antes, t0 = _rss_mb(), time.perf_counter()
            try:
                _modelos[nombre] = fabrica()
            except ImportError as e:
                raise DeteccionNoDisponible(f"falta la librería {e.name} (pip install -r requirements.txt)") from e
            except Exception as e:  # noqa: BLE001
                raise DeteccionNoDisponible(f"no se pudo cargar {nombre}: {type(e).__name__}: {e}") from e
            disp = _disp_real.get(nombre, dispositivo())
            _cargas[nombre] = {"segundos": round(time.perf_counter() - t0, 2), "rss_antes_mb": antes,
                               "rss_despues_mb": _rss_mb(), "dispositivo": disp}
            print(f"  [modelo] {nombre} cargado en {_cargas[nombre]['segundos']} s "
                  f"({antes} → {_cargas[nombre]['rss_despues_mb']} MB de RSS, {disp})")
        return _modelos[nombre]


def modelos_cargados() -> dict:
    return dict(_cargas)


# ============================================================ texto
# Reglas de amenaza (texto sin tildes y en minúscula). Cubren voseo y tuteo.
AMENAZAS = [
    # "te voy a buscar/encontrar" queda fuera: casi siempre es benigno ("te voy a buscar a la parada")
    r"\bte (voy|vamos) a (matar|partir la|reventar|quebrar|destruir|hacer pagar|joder|romper la)",
    r"\b(te|lo|la|los|las) (mato|parto|reviento|quiebro|rajo)\b",
    r"\b(vas|van) a (pagar|morir|sufrir|arrepentir|terminar (muy )?mal|ver lo que)",
    r"\bte vas a arrepentir",
    r"\b(se|sabemos) donde (vivis|vives|vive|trabaj|estudi|esta tu)",
    r"\bya (sabemos|se) a que hora (salis|sales|sale)",
    r"\b(te|lo|la|los) estamos buscando\b",
    r"\besto no se queda asi\b",
    r"\bte va a ir (muy )?mal\b",
    r"\bno (me hago|respondo) responsable\b|\bno respondo\b",
    r"\bmas te vale\b.*\bo (vas|te)\b",
    r"\bla proxima (vez )?no te la vas a acabar\b",
    r"\b(cuidate|cuidese|ten cuidado)\b.*\b(se|sabemos|vas|te)\b",
    # deseos de muerte o de daño ("ojalá se mueran", "deberían matarlos")
    r"\bojala (te|se|lo|la|los|las) (muera|mueran|maten|mate|pudra|pudran)\b",
    r"\b(deberian|habria que|hay que) (matar|matarlos|matarlas|matarte|quemar|quemarlos)\b",
]
_RE_AMENAZA = [re.compile(p) for p in AMENAZAS]

# Insultos frecuentes en español (incluye insultos machistas y homófobos, porque hay que detectarlos).
# Se excluyen palabras con un uso cotidiano frecuente ("basura", "cerdo", "animal", "gordo"): en un chat
# familiar daban falsas alarmas ("¿quién saca la basura?").
INSULTOS = [
    "idiota", "imbecil", "estupido", "estupida", "inutil", "tarado", "tarada", "pendejo", "pendeja",
    "huevon", "huevona", "cabron", "cabrona", "malparido", "malparida", "hijueputa", "hijo de puta",
    "hija de puta", "puta", "perra", "zorra", "maricon", "marica", "mierda", "escoria",
    "payaso", "payasa", "ridiculo", "ridicula", "fracasado", "fracasada", "asqueroso", "asquerosa",
    "mongolico", "retrasado", "retrasada", "subnormal", "gilipollas", "lacra",
    "parasito", "das asco", "me das asco", "ignorante", "tonto", "tonta",
]
# las frases largas primero, para que "hijo de puta" gane sobre "puta"
_RE_INSULTO = re.compile(r"\b(" + "|".join(re.escape(w) for w in sorted(INSULTOS, key=len, reverse=True)) + r")\b")


def _modelo_odio():
    from pysentimiento import create_analyzer
    return create_analyzer(task="hate_speech", lang="es")


def analizar_textos(textos: list[str], usar_modelo: bool | None = None) -> list[list[dict]]:
    """Para cada texto, la lista de hallazgos (vacía si no hay nada).
    usar_modelo=None → según WA_TEXTO_MODELO (0 = solo reglas e insultos, sin cargar el modelo)."""
    if usar_modelo is None:
        usar_modelo = config("WA_TEXTO_MODELO", "1") not in ("0", "no", "false")
    resultados: list[list[dict]] = [[] for _ in textos]
    for i, t in enumerate(textos):
        limpio = _sin_tildes(t)
        for patron in _RE_AMENAZA:
            m = patron.search(limpio)
            if m:
                resultados[i].append({"categoria": "amenaza", "motivo": f"regla: «{m.group(0)}»", "puntaje": 1.0})
                break
        m = _RE_INSULTO.search(limpio)
        if m:
            resultados[i].append({"categoria": "insulto", "motivo": f"palabra: «{m.group(0)}»", "puntaje": 1.0})

    if usar_modelo and textos:
        modelo = _cargar("pysentimiento hate_speech", _modelo_odio)
        salidas = modelo.predict(textos)
        for i, s in enumerate(salidas if isinstance(salidas, list) else [salidas]):
            p = s.probas
            if p.get("aggressive", 0) >= UMBRAL_TEXTO:
                resultados[i].append({"categoria": "agresion", "motivo": f"modelo: agresivo {p['aggressive']:.2f}",
                                      "puntaje": round(p["aggressive"], 3)})
            if p.get("hateful", 0) >= UMBRAL_TEXTO:
                resultados[i].append({"categoria": "odio", "motivo": f"modelo: odio {p['hateful']:.2f}"
                                      + (" (dirigido)" if p.get("targeted", 0) >= UMBRAL_TEXTO else ""),
                                      "puntaje": round(p["hateful"], 3)})
    for lista in resultados:
        for h in lista:
            h["severidad"] = SEVERIDAD[h["categoria"]]
    return resultados


# ============================================================ imagen
def _modelo_yolo():
    from ultralytics import YOLO
    return YOLO(config("WA_YOLO_MODELO", "yolov8l-oiv7.pt"))


def analizar_imagen(ruta: str) -> tuple[dict, list[dict]]:
    """→ (resumen de objetos detectados, hallazgos de armas)."""
    modelo = _cargar("YOLOv8 Open Images V7", _modelo_yolo)
    r = modelo.predict(ruta, conf=UMBRAL_IMAGEN, device=0 if dispositivo() == "cuda" else "cpu", verbose=False)[0]
    objetos, hallazgos = {}, []
    for caja, clase, conf in zip(r.boxes.xyxy.tolist(), r.boxes.cls.tolist(), r.boxes.conf.tolist()):
        nombre = r.names[int(clase)]
        objetos[nombre] = max(objetos.get(nombre, 0), round(conf, 3))
        if nombre in CLASES_ARMA:
            hallazgos.append({"categoria": "arma", "severidad": "alta", "puntaje": round(conf, 3),
                              "motivo": f"objeto: {nombre} {conf:.2f}", "clase": nombre,
                              "caja": [round(v) for v in caja]})
    return {"objetos": dict(sorted(objetos.items(), key=lambda x: -x[1])[:10]),
            "ancho": r.orig_shape[1], "alto": r.orig_shape[0]}, hallazgos


# ============================================================ audio
def _precargar_cuda12() -> None:
    """faster-whisper (CTranslate2) está compilado para CUDA 12, mientras que PyTorch trae CUDA 13.
    Si pip instaló cuBLAS 12 (nvidia-cublas-cu12), se carga desde site-packages para que CTranslate2 lo encuentre."""
    for base in {p for p in sys.path if p.endswith("site-packages")}:
        for patron in ("nvidia/cublas/lib/libcublasLt.so.12", "nvidia/cublas/lib/libcublas.so.12", "nvidia/cudnn/lib/libcudnn*.so.9"):
            for lib in sorted(glob.glob(os.path.join(base, patron))):
                try:
                    ctypes.CDLL(lib, mode=ctypes.RTLD_GLOBAL)
                except OSError:
                    pass


def _modelo_whisper():
    import numpy as np
    from faster_whisper import WhisperModel
    nombre = f"Whisper {config('WA_WHISPER_MODELO', 'small')}"
    disp = config("WA_WHISPER_DISPOSITIVO", dispositivo())

    def crear(d):
        precision = config("WA_WHISPER_PRECISION", "auto")
        if precision == "auto":
            precision = "float16" if d == "cuda" else "int8"
        return WhisperModel(config("WA_WHISPER_MODELO", "small"), device=d, compute_type=precision,
                            cpu_threads=int(config("WA_WHISPER_HILOS", "0")))
    if disp == "cuda":
        _precargar_cuda12()
        try:
            modelo = crear("cuda")
            list(modelo.transcribe(np.zeros(16000, dtype=np.float32), language="es")[0])   # prueba real en la GPU
            _disp_real[nombre] = "cuda"
            return modelo
        except Exception as e:  # noqa: BLE001 — falta una librería de CUDA: se sigue en CPU
            print(f"  [modelo] Whisper no pudo usar la GPU ({type(e).__name__}: {str(e)[:120]}); se usa la CPU")
    _disp_real[nombre] = "cpu"
    return crear("cpu")


def _decodificar(ruta: str):
    """Audio → muestras float32 mono a 16 kHz con ffmpeg. (faster-whisper decodifica con PyAV,
    pero PyAV 19 rompió su interfaz; ffmpeg ya lo usa el resto del proyecto.)"""
    import numpy as np
    r = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", ruta, "-f", "s16le", "-ac", "1", "-ar", "16000", "-"],
                       capture_output=True, check=True)
    return np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768.0


def analizar_audio(ruta: str) -> tuple[dict, list[dict]]:
    """→ (transcripción con segmentos, hallazgos con la hora del segmento)."""
    modelo = _cargar(f"Whisper {config('WA_WHISPER_MODELO', 'small')}", _modelo_whisper)
    segmentos, info = modelo.transcribe(_decodificar(ruta), language="es", beam_size=1)
    segmentos = [{"inicio": round(s.start, 2), "fin": round(s.end, 2), "texto": s.text.strip()} for s in segmentos]
    por_segmento = analizar_textos([s["texto"] for s in segmentos])
    hallazgos = []
    for seg, lista in zip(segmentos, por_segmento):
        for h in lista:
            hallazgos.append({**h, "inicio": seg["inicio"], "fin": seg["fin"], "texto": seg["texto"]})
    return {"transcripcion": " ".join(s["texto"] for s in segmentos), "segmentos": segmentos,
            "duracion_s": round(info.duration, 2)}, hallazgos
