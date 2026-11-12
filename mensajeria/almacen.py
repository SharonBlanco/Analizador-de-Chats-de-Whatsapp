"""
M3 · Almacenamiento direccionado por contenido (SHA-256) con deduplicación.

Cada archivo adjunto se guarda UNA sola vez en <dataset>/.almacen/, con su SHA-256 como nombre:
    .almacen/ab/abcdef0123…   (las dos primeras letras como subcarpeta, para no tener miles de
                                archivos en un mismo directorio)
La carpeta de cada caso no tiene copias: tiene ENLACES DUROS (hard links) a esos archivos.
Un enlace duro es otro nombre para el mismo inodo: no ocupa espacio y la aplicación sigue
leyendo <caso>/IMG-….jpg como siempre.

  · Deduplicación: si el SHA-256 ya está en el almacén, no se vuelve a guardar.
  · Recolección de basura: el kernel cuenta cuántos nombres tiene cada inodo (st_nlink).
    Un archivo del almacén con st_nlink == 1 ya no lo usa ningún caso → se puede borrar.
  · Integridad: el nombre ES el hash; `verificar()` recalcula y detecta cualquier alteración.

Herramienta:
  python mensajeria/almacen.py --dataset dataset_whatsapp --migrar   pasa los casos ya importados al almacén
  python mensajeria/almacen.py --dataset dataset_whatsapp            estadísticas de deduplicación
  python mensajeria/almacen.py --dataset dataset_whatsapp --verificar
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
from pathlib import Path

BLOQUE_LECTURA = 1024 * 1024


def sha256_de(ruta: Path) -> str:
    h = hashlib.sha256()
    with open(ruta, "rb") as f:
        while bloque := f.read(BLOQUE_LECTURA):
            h.update(bloque)
    return h.hexdigest()


class Almacen:
    def __init__(self, dataset: Path):
        self.dataset = Path(dataset)
        self.raiz = self.dataset / ".almacen"
        self._candado = threading.Lock()

    def ruta(self, sha: str) -> Path:
        return self.raiz / sha[:2] / sha

    def guardar(self, origen: Path, sha: str | None = None) -> tuple[str, bool]:
        """Guarda el contenido de `origen` en el almacén → (sha256, era_nuevo).
        Si ya existía (mismo contenido), no se copia nada: deduplicación."""
        sha = sha or sha256_de(origen)
        destino = self.ruta(sha)
        with self._candado:
            if destino.exists():
                return sha, False
            destino.parent.mkdir(parents=True, exist_ok=True)
            # copiar a un temporal y renombrar: os.replace es atómico, nunca queda un blob a medias
            fd, tmp = tempfile.mkstemp(dir=destino.parent, prefix=".tmp-")
            os.close(fd)
            shutil.copyfile(origen, tmp)
            os.chmod(tmp, 0o444)                 # solo lectura: una evidencia no se modifica en su lugar
            os.replace(tmp, destino)
        return sha, True

    def enlazar(self, sha: str, destino: Path) -> None:
        """Crea <destino> como enlace duro al archivo del almacén (sin copiar datos)."""
        destino.unlink(missing_ok=True)
        try:
            os.link(self.ruta(sha), destino)
        except OSError:                          # otro sistema de archivos: no queda otra que copiar
            shutil.copyfile(self.ruta(sha), destino)

    def agregar(self, origen: Path, destino: Path) -> tuple[str, bool]:
        """guardar + enlazar: lo que usa el importador por cada adjunto."""
        sha, nuevo = self.guardar(origen)
        self.enlazar(sha, destino)
        return sha, nuevo

    def recolectar(self) -> dict:
        """Borra del almacén los archivos que ya no usa ningún caso (st_nlink == 1)."""
        borrados = liberados = 0
        if not self.raiz.exists():
            return {"borrados": 0, "bytes_liberados": 0}
        with self._candado:
            for f in self.raiz.glob("??/*"):
                st = f.stat()
                if st.st_nlink == 1 and not f.name.startswith(".tmp-"):
                    f.unlink()
                    borrados += 1
                    liberados += st.st_size
        return {"borrados": borrados, "bytes_liberados": liberados}

    def estadisticas(self) -> dict:
        """Bytes lógicos (lo que ocuparía sin deduplicar) frente a bytes físicos (lo que ocupa)."""
        logicos = referencias = 0
        for meta in self.dataset.glob("*/metadata.json"):
            try:
                files = json.loads(meta.read_text(encoding="utf-8")).get("files", {})
            except (OSError, ValueError):
                continue
            for nombre, f in files.items():
                if nombre != "chat.txt" and f.get("bytes"):
                    logicos += f["bytes"]
                    referencias += 1
        blobs = list(self.raiz.glob("??/*")) if self.raiz.exists() else []
        fisicos = sum(b.stat().st_size for b in blobs)
        return {"archivos_referenciados": referencias, "archivos_unicos": len(blobs),
                "bytes_logicos": logicos, "bytes_fisicos": fisicos, "bytes_ahorrados": max(0, logicos - fisicos),
                "tasa_deduplicacion": round(1 - fisicos / logicos, 4) if logicos else 0.0}

    def verificar(self) -> list[str]:
        """Recalcula el SHA-256 de cada archivo del almacén; devuelve los que no coinciden con su nombre."""
        return [str(b) for b in (self.raiz.glob("??/*") if self.raiz.exists() else [])
                if not b.name.startswith(".tmp-") and sha256_de(b) != b.name]

    def migrar(self) -> dict:
        """Pasa al almacén los adjuntos de los casos importados antes de que existiera (copias → enlaces)."""
        nuevos = repetidos = 0
        for meta in self.dataset.glob("*/metadata.json"):
            caso = meta.parent
            files = json.loads(meta.read_text(encoding="utf-8")).get("files", {})
            for nombre, f in files.items():
                p = caso / nombre
                if nombre == "chat.txt" or not p.is_file():
                    continue
                sha = f.get("sha256") or sha256_de(p)
                if p.stat().st_nlink > 1 and self.ruta(sha).exists() and os.path.samefile(p, self.ruta(sha)):
                    continue                        # ya está en el almacén
                _, nuevo = self.guardar(p, sha)
                self.enlazar(sha, p)
                nuevos += nuevo
                repetidos += not nuevo
        return {"guardados": nuevos, "deduplicados": repetidos}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Almacén por contenido (M3)")
    ap.add_argument("--dataset", type=Path, default=Path(__file__).resolve().parent.parent / "dataset_whatsapp")
    ap.add_argument("--migrar", action="store_true")
    ap.add_argument("--verificar", action="store_true")
    ap.add_argument("--recolectar", action="store_true")
    a = ap.parse_args()
    al = Almacen(a.dataset)
    if a.migrar:
        print("migración:", al.migrar())
    if a.recolectar:
        print("recolección:", al.recolectar())
    if a.verificar:
        malos = al.verificar()
        print("verificación:", "todo coincide" if not malos else f"{len(malos)} alterados: {malos[:5]}")
    e = al.estadisticas()
    print(f"{e['archivos_referenciados']} adjuntos referenciados · {e['archivos_unicos']} archivos únicos en el almacén")
    print(f"{e['bytes_logicos'] / 2**20:.1f} MB sin deduplicar → {e['bytes_fisicos'] / 2**20:.1f} MB en disco "
          f"(ahorro {e['bytes_ahorrados'] / 2**20:.1f} MB, tasa de deduplicación {e['tasa_deduplicacion']:.1%})")
