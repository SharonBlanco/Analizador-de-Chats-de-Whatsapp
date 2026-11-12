"""M1 · Memoria de trabajo del detector de texto según el tamaño del bloque (mensajes por sub-tarea).
Un proceso por tamaño; se mide el pico de la 2.ª inferencia (la 1.ª incluye cargar los pesos)."""
import json, subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))
from medir_modelos import TEXTOS, fase, mem  # noqa: E402

if len(sys.argv) > 1 and sys.argv[1] == "--uno":
    n = int(sys.argv[2])
    from pysentimiento import create_analyzer
    an = create_analyzer(task="hate_speech", lang="es")
    textos = (TEXTOS * (n // len(TEXTOS) + 1))[:n]
    r = {}
    fase("inferencia", lambda: an.predict(textos), r)
    fase("inferencia2", lambda: an.predict(textos), r)
    x = r["inferencia2"]
    print("RESULTADO " + json.dumps({"mensajes": n, "trabajo_mb": round(x["despues"]["hwm"] - x["antes"]["rss"], 1),
                                     "residente_mb": x["despues"]["rss"], "segundos": x["segundos"]}))
else:
    filas = []
    for n in (32, 64, 256, 1024):
        p = subprocess.run([sys.executable, __file__, "--uno", str(n)], capture_output=True, text=True)
        linea = next((l for l in p.stdout.splitlines() if l.startswith("RESULTADO ")), None)
        filas.append(json.loads(linea[10:]) if linea else {"mensajes": n, "error": f"código {p.returncode}"})
        print(filas[-1], flush=True)
    Path("/salida/texto_por_bloque.json").write_text(json.dumps(filas, indent=2))
