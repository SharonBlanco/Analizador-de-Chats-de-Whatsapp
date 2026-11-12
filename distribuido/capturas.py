"""
Capturas de pantalla de la interfaz con Google Chrome sin ventana (headless), para el README y el informe.
Solo se capturan vistas del dataset sintético: nunca vistas que muestren chats reales.

  python distribuido/capturas.py                         capturas por defecto → docs/img/
  python distribuido/capturas.py workers "#/workers"     una captura con nombre y ruta
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

R = Path(__file__).resolve().parents[1]
DESTINO = R / "docs" / "img"
APP = "http://localhost:8100/"
POR_DEFECTO = {
    "conversaciones": "#/conversaciones/1d5c10e0",               # carpeta "Dataset sintético"
    "caso_resumen": "#/caso/027_vecinos_del_condominio/resumen",
    "caso_hallazgos": "#/caso/027_vecinos_del_condominio/hallazgos",
    "caso_estadisticas": "#/caso/027_vecinos_del_condominio/estadisticas",
    "caso_paralelo": "#/caso/027_vecinos_del_condominio/paralelo",
    "workers": "#/workers",
}


def chrome() -> list[str]:
    """Chrome local, o el de Flatpak (si VS Code corre como Flatpak, a través del host)."""
    for b in ("google-chrome", "chromium", "chromium-browser"):
        if shutil.which(b):
            return [b]
    pre = ["flatpak-spawn", "--host"] if shutil.which("flatpak-spawn") else []
    return pre + ["flatpak", "run", "com.google.Chrome"]


def capturar(nombre: str, ruta: str, ancho: int = 1440, alto: int = 1000) -> Path:
    # Chrome en Flatpak solo puede escribir en Descargas: se captura ahí y se mueve a docs/img/
    descargas = Path(subprocess.run((["flatpak-spawn", "--host"] if shutil.which("flatpak-spawn") else []) +
                                    ["xdg-user-dir", "DOWNLOAD"], capture_output=True, text=True).stdout.strip() or Path.home())
    tmp = descargas / f"_captura_{nombre}.png"
    subprocess.run(chrome() + ["--headless=new", "--disable-gpu", "--hide-scrollbars", f"--window-size={ancho},{alto}",
                               "--virtual-time-budget=8000", f"--screenshot={tmp}", APP + ruta],
                   capture_output=True, timeout=120)
    DESTINO.mkdir(parents=True, exist_ok=True)
    final = DESTINO / f"{nombre}.png"
    shutil.move(tmp, final)
    print("→", final)
    return final


if __name__ == "__main__":
    if len(sys.argv) == 3:
        capturar(sys.argv[1], sys.argv[2])
    else:
        for n, r in POR_DEFECTO.items():
            capturar(n, r)
