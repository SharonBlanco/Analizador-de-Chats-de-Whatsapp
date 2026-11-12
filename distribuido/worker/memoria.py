"""
M1 · Gestión de memoria del worker.

1. Límite del nodo. El worker corre dentro de un cgroup v2 con límite (docker run --memory=…,
   systemd MemoryMax=…). Aquí se lee ese límite y el uso real del cgroup:
     /sys/fs/cgroup/memory.max      límite en bytes ("max" = sin límite)
     /sys/fs/cgroup/memory.current  memoria cargada al cgroup ahora (RSS + caché de páginas)
     /sys/fs/cgroup/memory.peak     pico desde que arrancó el contenedor
     /sys/fs/cgroup/memory.events   contador oom_kill: cuántos procesos mató el kernel por memoria

2. Huella de cada modelo (huella_modelos.json, medida con distribuido/m1/medir_modelos.py):
   cuánto cuesta importar cada librería, cargar cada modelo y procesar una sub-tarea.

3. Plan de admisión del nodo (al arrancar). Con el límite y las huellas decide QUÉ acepta:
   qué pools atiende, qué tamaño de Whisper usa y cuántas sub-tareas corre a la vez.
   Si algo no cabe, se degrada en lugar de reventar: texto sin modelo (solo reglas),
   Whisper más pequeño o no atender ese pool (otro nodo con más memoria lo hará).

4. Control de admisión en ejecución (Admision): antes de empezar una sub-tarea reserva su
   memoria de trabajo; si no hay, la sub-tarea espera a que otra termine. Además compara con
   el uso REAL del cgroup, por si las estimaciones se quedaron cortas.

5. OOM. Si aun así el kernel mata al worker por memoria, supervisor.py lo detecta (contador
   oom_kill), anota qué sub-tareas tenía en curso y lo reinicia. Política de reencolado:
     1.ª vez que una sub-tarea provoca OOM en este nodo → se reintenta SOLA (modo exclusivo:
         sin otras sub-tareas en paralelo).
     2.ª vez → el nodo la rechaza como fallo reintentable: el coordinador la re-encola
         (máx. WA_MAX_REINTENTOS) y puede tomarla un nodo con más memoria. Nunca un bucle infinito.

Uso como herramienta:
  python distribuido/worker/memoria.py                    → estado del cgroup y plan con la memoria actual
  python distribuido/worker/memoria.py --nodo 12g         → cuántos workers caben en un nodo de 12 GB
"""

from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path

try:
    from comun import config
except ImportError:                     # uso como herramienta fuera del worker
    def config(clave, defecto=None):
        return os.environ.get(clave, defecto)

AQUI = Path(__file__).resolve().parent
CGROUP = Path("/sys/fs/cgroup")
ARCHIVO_HUELLAS = AQUI / "huella_modelos.json"
MB = 2 ** 20
MAX_OOM = 2                              # OOM de la misma sub-tarea en este nodo antes de rechazarla
ORDEN_WHISPER = ["large-v3-turbo", "medium", "small", "base", "tiny"]


# ============================================================ lectura del cgroup y de /proc
def _leer(nombre: str) -> str | None:
    for ruta in (CGROUP / nombre, CGROUP / "memory" / nombre):
        try:
            return ruta.read_text().strip()
        except OSError:
            pass
    return None


def limite_mb() -> float | None:
    """Límite de memoria del cgroup (v2; v1 como respaldo). None si no hay límite."""
    v = _leer("memory.max") or _leer("memory.limit_in_bytes")
    if not v or v == "max" or int(v) >= 2 ** 60:
        return None
    return round(int(v) / MB, 1)


def uso_mb() -> float | None:
    v = _leer("memory.current") or _leer("memory.usage_in_bytes")
    return round(int(v) / MB, 1) if v else None


def pico_mb() -> float | None:
    v = _leer("memory.peak") or _leer("memory.max_usage_in_bytes")
    return round(int(v) / MB, 1) if v else None


def oom_kills() -> int | None:
    """Procesos de este cgroup que el kernel mató por falta de memoria (memory.events)."""
    v = _leer("memory.events")
    if v is None:
        return None
    for linea in v.splitlines():
        clave, _, n = linea.partition(" ")
        if clave == "oom_kill":
            return int(n)
    return 0


def proceso_mb(pid: int | str = "self") -> dict:
    """RSS, PSS y USS de un proceso (/proc/<pid>/smaps_rollup).
    RSS cuenta completas las páginas compartidas; PSS las reparte entre quienes las comparten
    (la suma de PSS de varios procesos es la memoria real que usan juntos); USS = solo las privadas."""
    datos = {}
    try:
        for linea in Path(f"/proc/{pid}/smaps_rollup").read_text().splitlines():
            partes = linea.split()
            if len(partes) >= 2 and partes[0].endswith(":") and partes[1].isdigit():
                datos[partes[0][:-1]] = int(partes[1]) / 1024
    except OSError:
        return {}
    return {"rss": round(datos.get("Rss", 0), 1), "pss": round(datos.get("Pss", 0), 1),
            "uss": round(datos.get("Private_Clean", 0) + datos.get("Private_Dirty", 0), 1)}


def pico_proceso_mb(pid: int | str = "self") -> float | None:
    """VmHWM: el RSS máximo que alcanzó el proceso."""
    try:
        for linea in Path(f"/proc/{pid}/status").read_text().splitlines():
            if linea.startswith("VmHWM:"):
                return round(int(linea.split()[1]) / 1024, 1)
    except OSError:
        pass
    return None


def disponible_sistema_mb() -> float | None:
    try:
        for linea in Path("/proc/meminfo").read_text().splitlines():
            if linea.startswith("MemAvailable:"):
                return round(int(linea.split()[1]) / 1024, 1)
    except OSError:
        pass
    return None


def margen() -> float:
    """Fracción del límite que se reserva para caché de páginas, fragmentación y picos no medidos."""
    return float(config("WA_MARGEN_MEMORIA", "0.10"))


def presupuesto_mb() -> tuple[float | None, str]:
    """Memoria que el plan puede repartir → (MB, de dónde salió)."""
    fijo = config("WA_MEMORIA_MB")
    if fijo:
        return float(fijo) * (1 - margen()), "WA_MEMORIA_MB"
    lim = limite_mb()
    if lim:
        return round(lim * (1 - margen()), 1), "cgroup (memory.max)"
    disp = disponible_sistema_mb()
    if disp:
        return round(disp * (1 - margen()), 1), "MemAvailable (sin límite de cgroup)"
    return None, "desconocido"


# ============================================================ huellas medidas
def huellas() -> dict:
    try:
        datos = json.loads(ARCHIVO_HUELLAS.read_text(encoding="utf-8"))
        if datos.get("modelos"):
            return datos
    except (OSError, ValueError):
        pass
    # Sin mediciones: valores conservadores (se reemplazan corriendo distribuido/m1/medir_modelos.py)
    return {"provisional": True, "componentes": {"torch": 400, "pysentimiento": 250, "ultralytics": 150, "faster_whisper": 60},
            "modelos": {
                "texto": {"requiere": ["torch", "pysentimiento"], "carga_mb": 550, "trabajo_mb": 250},
                "texto-reglas": {"requiere": [], "carga_mb": 0, "trabajo_mb": 30},
                "yolo": {"requiere": ["torch", "ultralytics"], "carga_mb": 300, "trabajo_mb": 400},
                "whisper-tiny": {"requiere": ["faster_whisper"], "carga_mb": 100, "trabajo_mb": 200},
                "whisper-base": {"requiere": ["faster_whisper"], "carga_mb": 150, "trabajo_mb": 250},
                "whisper-small": {"requiere": ["faster_whisper"], "carga_mb": 350, "trabajo_mb": 350},
                "whisper-medium": {"requiere": ["faster_whisper"], "carga_mb": 900, "trabajo_mb": 500},
                "adjuntos": {"requiere": [], "carga_mb": 0, "trabajo_mb": 20},
            }}


# ============================================================ plan de admisión del nodo
def planificar(pools: list[str], presupuesto: float | None, base_mb: float, *, texto_modelo: bool,
               whisper_preferido: str, deteccion: bool = True, concurrencia_max: int = 1,
               admision: bool = True, h: dict | None = None) -> dict:
    """Decide qué atiende el nodo para que todo quepa en el presupuesto.
    Devuelve pools aceptados, rechazados (con motivo), modelos elegidos y concurrencia."""
    h = h or huellas()
    comp, mod = h["componentes"], h["modelos"]
    cargados: set[str] = set()
    persistente = base_mb
    trabajo: dict[str, float] = {}
    aceptados, rechazados, decisiones = [], {}, []
    elegido = {"texto": "texto" if (texto_modelo and deteccion) else "texto-reglas", "whisper": None, "yolo": None}

    def costo(claves: list[str]) -> tuple[float, set[str]]:
        """MB persistentes que agregan esos modelos (las librerías ya importadas no se cobran dos veces)."""
        nuevos, total = set(), 0.0
        for k in claves:
            for c in mod[k]["requiere"]:
                if c not in cargados and c not in nuevos:
                    nuevos.add(c)
                    total += comp.get(c, 0)
            total += mod[k]["carga_mb"] if k not in cargados else 0
        return total, nuevos

    def cabe(claves: list[str], trabajo_mb: float) -> tuple[bool, float, set[str]]:
        extra, nuevos = costo(claves)
        pico_trabajo = max([trabajo_mb, *trabajo.values()])
        return (not admision or presupuesto is None or persistente + extra + pico_trabajo <= presupuesto), extra, nuevos

    def aceptar(pool: str, claves: list[str], trabajo_mb: float, extra: float, nuevos: set[str]) -> None:
        nonlocal persistente
        persistente += extra
        cargados.update(nuevos)
        cargados.update(claves)
        trabajo[pool] = trabajo_mb
        aceptados.append(pool)

    orden = sorted(pools, key=lambda p: ["texto", "adjuntos", "imagen", "audio"].index(p))   # texto primero: imprescindible
    for pool in orden:
        if pool == "adjuntos":
            ok, extra, nuevos = cabe([], mod["adjuntos"]["trabajo_mb"])
            aceptar(pool, [], mod["adjuntos"]["trabajo_mb"], extra, nuevos) if ok else rechazados.__setitem__(pool, "sin memoria")
        elif pool == "texto":
            candidatos = [elegido["texto"]] + (["texto-reglas"] if elegido["texto"] == "texto" else [])
            for k in candidatos:
                ok, extra, nuevos = cabe([k], mod[k]["trabajo_mb"])
                if ok:
                    if k != elegido["texto"]:
                        decisiones.append(f"texto sin modelo (solo reglas): el modelo necesita "
                                          f"{costo(['texto'])[0] + mod['texto']['trabajo_mb']:.0f} MB")
                    elegido["texto"] = k
                    aceptar(pool, [k], mod[k]["trabajo_mb"], extra, nuevos)
                    break
            else:
                rechazados[pool] = "sin memoria ni para las reglas"
        elif pool == "imagen":
            if not deteccion:
                aceptar(pool, [], mod["adjuntos"]["trabajo_mb"], 0, set())
                continue
            k = ["yolo"]
            ok, extra, nuevos = cabe(k, mod["yolo"]["trabajo_mb"])
            if ok:
                elegido["yolo"] = "yolo"
                aceptar(pool, k, mod["yolo"]["trabajo_mb"], extra, nuevos)
            else:
                rechazados[pool] = f"YOLO necesita {extra + mod['yolo']['trabajo_mb']:.0f} MB"
        elif pool == "audio":
            if not deteccion:
                aceptar(pool, [], mod["adjuntos"]["trabajo_mb"], 0, set())
                continue
            # el audio también pasa su transcripción por el detector de texto
            tam = ORDEN_WHISPER[ORDEN_WHISPER.index(whisper_preferido):] if whisper_preferido in ORDEN_WHISPER else [whisper_preferido]
            probados = []
            for t in tam:
                clave = f"whisper-{t}"
                if clave not in mod:
                    continue
                probados.append(t)
                claves = [clave] + ([elegido["texto"]] if elegido["texto"] == "texto" else [])
                ok, extra, nuevos = cabe(claves, mod[clave]["trabajo_mb"])
                if ok:
                    if t != whisper_preferido:
                        decisiones.append(f"Whisper {t} en lugar de {whisper_preferido} (no cabe en el límite)")
                    elegido["whisper"] = t
                    aceptar(pool, claves, mod[clave]["trabajo_mb"], extra, nuevos)
                    break
            else:
                ultimo = f"whisper-{probados[-1]}" if probados else None
                rechazados[pool] = (f"Whisper {probados[-1]} + detector de texto necesitan "
                                    f"{costo([ultimo] + ([elegido['texto']] if elegido['texto'] == 'texto' else []))[0] + mod[ultimo]['trabajo_mb']:.0f} MB"
                                    if ultimo else "no hay huella medida de Whisper")

    max_trabajo = max(trabajo.values(), default=0)
    if admision and presupuesto is not None and max_trabajo:
        por_memoria = max(1, math.floor((presupuesto - persistente) / max_trabajo))
    else:
        por_memoria = concurrencia_max
    return {"presupuesto_mb": presupuesto, "base_mb": round(base_mb, 1), "persistente_mb": round(persistente, 1),
            "trabajo_mb": trabajo, "pools": aceptados, "rechazados": rechazados, "decisiones": decisiones,
            "texto_modelo": elegido["texto"] == "texto", "whisper": elegido["whisper"],
            "concurrencia": max(1, min(concurrencia_max, por_memoria)), "concurrencia_por_memoria": por_memoria,
            "admision": admision, "huellas_provisionales": bool(h.get("provisional"))}


# ============================================================ admisión en ejecución
class Admision:
    """Reserva la memoria de trabajo de cada sub-tarea antes de empezarla (semáforo por MB)."""

    def __init__(self, plan: dict):
        self.plan = plan
        self.activa = plan["admision"] and plan["presupuesto_mb"] is not None
        self.capacidad = (plan["presupuesto_mb"] - plan["persistente_mb"]) if self.activa else math.inf
        self.reservado = 0.0
        self.exclusiva = False
        self.esperas = 0                       # cuántas veces una sub-tarea tuvo que esperar memoria
        self.cond = threading.Condition()

    def entrar(self, pool: str, exclusiva: bool = False, cancelada=lambda: False) -> float:
        """Bloquea hasta que haya memoria. exclusiva=True: espera a que no corra nada más (tras un OOM)."""
        mb = self.plan["trabajo_mb"].get(pool, 0)
        esperó = False
        with self.cond:
            while True:
                if cancelada():
                    return 0.0
                otros = self.reservado > 0 or self.exclusiva
                if exclusiva and not otros:
                    break
                if not exclusiva and not self.exclusiva:
                    cabe = self.reservado + mb <= self.capacidad
                    # comprobación con el uso REAL del cgroup (las huellas son estimaciones)
                    real, pres = uso_mb(), self.plan["presupuesto_mb"]
                    if self.activa and real is not None and pres and self.reservado > 0 and real + mb > pres:
                        cabe = False
                    if cabe or self.reservado == 0:       # si no corre nada, siempre entra (evita bloqueo eterno)
                        break
                if not esperó:
                    esperó = True
                    self.esperas += 1
                self.cond.wait(timeout=1)
            self.reservado += mb
            self.exclusiva = self.exclusiva or exclusiva
        return mb

    def salir(self, mb: float, exclusiva: bool = False) -> None:
        with self.cond:
            self.reservado = max(0.0, self.reservado - mb)
            if exclusiva:
                self.exclusiva = False
            self.cond.notify_all()

    def estado(self) -> dict:
        with self.cond:
            return {"reservado_mb": round(self.reservado, 1),
                    "capacidad_mb": None if self.capacidad == math.inf else round(self.capacidad, 1),
                    "esperas": self.esperas, "exclusiva": self.exclusiva}


# ============================================================ registro para el supervisor (OOM)
def _escribir(ruta: Path, datos) -> None:
    tmp = ruta.with_suffix(".tmp")
    tmp.write_text(json.dumps(datos), encoding="utf-8")
    os.replace(tmp, ruta)                      # atómico: el supervisor nunca lee un archivo a medias


def _leer_json(ruta: Path, defecto):
    try:
        return json.loads(ruta.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return defecto


class Registro:
    """Archivos compartidos entre worker y supervisor (en el /tmp del contenedor):
    en_curso.json → sub-tareas que el worker está procesando; oom.json → OOM por sub-tarea."""

    def __init__(self, carpeta: Path):
        carpeta.mkdir(parents=True, exist_ok=True)
        self.en_curso_f, self.oom_f = carpeta / "en_curso.json", carpeta / "oom.json"
        self._en_curso: dict[str, dict] = {}
        self._candado = threading.Lock()

    def empezar(self, sub: dict) -> None:
        with self._candado:
            self._en_curso[sub["subtarea"]] = {"trabajo": sub["trabajo"], "subtarea": sub["subtarea"], "tipo": sub["tipo"],
                                              "desde": time.time()}
            _escribir(self.en_curso_f, list(self._en_curso.values()))

    def terminar(self, sub: dict) -> None:
        with self._candado:
            self._en_curso.pop(sub["subtarea"], None)
            _escribir(self.en_curso_f, list(self._en_curso.values()))

    def en_curso(self) -> list[dict]:
        return _leer_json(self.en_curso_f, [])

    def oom_de(self, subtarea: str) -> int:
        return _leer_json(self.oom_f, {}).get(subtarea, 0)

    def anotar_oom(self, subtareas: list[str]) -> dict[str, int]:
        cuenta = _leer_json(self.oom_f, {})
        for s in subtareas:
            cuenta[s] = cuenta.get(s, 0) + 1
        _escribir(self.oom_f, cuenta)
        _escribir(self.en_curso_f, [])
        return {s: cuenta[s] for s in subtareas}


def estado_cgroup() -> dict:
    return {"limite_mb": limite_mb(), "uso_mb": uso_mb(), "pico_mb": pico_mb(), "oom_kills": oom_kills()}


# ============================================================ herramienta
def _a_mb(texto: str) -> float:
    t = texto.strip().lower()
    for sufijo, f in (("g", 1024), ("m", 1), ("k", 1 / 1024)):
        if t.endswith(sufijo):
            return float(t[:-1]) * f
    return float(t)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Estado de memoria y plan de admisión (M1)")
    ap.add_argument("--nodo", help="memoria de un nodo (p. ej. 12g): cuántos workers caben")
    ap.add_argument("--pools", default="texto,imagen,audio,adjuntos")
    ap.add_argument("--whisper", default="small")
    ap.add_argument("--worker-mb", help="límite por worker (p. ej. 3g); por defecto, el mínimo que necesita uno completo")
    a = ap.parse_args()
    pools = [p for p in a.pools.split(",") if p]
    h = huellas()
    base = h.get("base_mb", 60)
    print("cgroup:", estado_cgroup())
    if not a.nodo:
        pres, origen = presupuesto_mb()
        print(f"presupuesto: {pres} MB ({origen})")
        print(json.dumps(planificar(pools, pres, base, texto_modelo=True, whisper_preferido=a.whisper,
                                    concurrencia_max=4), indent=2, ensure_ascii=False))
    else:
        nodo = _a_mb(a.nodo)
        completo = planificar(pools, None, base, texto_modelo=True, whisper_preferido=a.whisper, admision=False)
        necesita = completo["persistente_mb"] + max(completo["trabajo_mb"].values())
        por_worker = _a_mb(a.worker_mb) if a.worker_mb else math.ceil(necesita / (1 - margen()) / 128) * 128
        n = int(nodo * 0.9 // por_worker)        # 10 % del nodo para el sistema operativo
        print(f"Un worker completo ({', '.join(pools)}, Whisper {a.whisper}) necesita ≈ {necesita:.0f} MB "
              f"→ límite sugerido --memory={por_worker:.0f}m")
        print(f"En un nodo de {nodo:.0f} MB caben {n} worker(s) de {por_worker:.0f} MB "
              f"(se deja el 10 % para el sistema operativo).")
