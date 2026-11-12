"""
M1 · Supervisor del worker: detecta terminaciones por OOM y reinicia el worker.

Es el proceso principal del contenedor (PID 1 en Docker) y arranca worker.py como proceso
HIJO. Si el worker se pasa del límite de memoria del cgroup, el kernel mata al proceso que
más memoria usa —el worker, no este supervisor, que pesa unos 30 MB— con SIGKILL.

Cuando el hijo termina, el supervisor revisa:
  · código de salida −9 (SIGKILL), y
  · que el contador oom_kill de /sys/fs/cgroup/memory.events haya subido.
Las dos cosas juntas confirman que fue un OOM (y no, por ejemplo, un kill -9 manual).

Si fue OOM:
  1. Lee qué sub-tareas tenía en curso el worker (en_curso.json, que el worker mantiene) y suma
     un OOM a cada una en oom.json. El worker nuevo usa esa cuenta para aplicar la política de
     reencolado (ver memoria.py): 1.ª vez → la sub-tarea corre sola; 2.ª → la rechaza para que
     el coordinador la mande a otro nodo.
  2. Avisa al coordinador (evento "oom" en la cola de resultados) para el dashboard y las alertas.
  3. Reinicia el worker. Las sub-tareas no se pierden: el worker no alcanzó a confirmarlas (ACK),
     así que RabbitMQ las re-entrega solas.
Espera entre reinicios: 1, 2, 4… hasta 30 s (si el worker vuelve a caer enseguida, no se reinicia en bucle).

Uso: python distribuido/worker/supervisor.py   (mismas variables de entorno que worker.py)
"""

from __future__ import annotations

import json
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

AQUI = Path(__file__).resolve().parent
for ruta in (AQUI.parent, AQUI.parent.parent / "mensajeria", AQUI / "mensajeria"):
    if ruta.is_dir():
        sys.path.insert(0, str(ruta))

import socket  # noqa: E402

import memoria  # noqa: E402
from comun import COLA_RESULTADOS, PERSISTENTE, config, declarar, parametros_rabbit  # noqa: E402

WORKER_ID = config("WORKER_ID", f"worker-{socket.gethostname()}")
DIR_TRABAJO = Path(config("WORKER_DIR", tempfile.gettempdir())) / f"wa-{WORKER_ID}"
hijo: subprocess.Popen | None = None
detener = False


def avisar_coordinador(eventos: list[dict]) -> None:
    try:
        import pika
        conexion = pika.BlockingConnection(parametros_rabbit())
        try:
            canal = conexion.channel()
            declarar(canal)
            for ev in eventos:
                canal.basic_publish(exchange="", routing_key=COLA_RESULTADOS, body=json.dumps(ev).encode(),
                                    properties=pika.BasicProperties(delivery_mode=PERSISTENTE))
        finally:
            conexion.close()
    except Exception as e:  # noqa: BLE001 — sin aviso igual se reinicia; RabbitMQ re-entrega
        print(f"[supervisor] no se pudo avisar el OOM al coordinador: {e}")


def al_senal(num, _frame) -> None:
    """docker stop (SIGTERM) o Ctrl+C: se lo pasamos al worker y no lo reiniciamos."""
    global detener
    detener = True
    # Ctrl+C en la terminal ya le llega al worker (mismo grupo de procesos); docker stop (SIGTERM) no
    if num == signal.SIGTERM and hijo and hijo.poll() is None:
        hijo.send_signal(signal.SIGINT)


def main() -> None:
    global hijo
    signal.signal(signal.SIGTERM, al_senal)
    signal.signal(signal.SIGINT, al_senal)
    registro = memoria.Registro(DIR_TRABAJO)
    reinicios, espera = 0, 1.0
    lim = memoria.limite_mb()
    print(f"[supervisor] {WORKER_ID}: límite de memoria del cgroup = {f'{lim:.0f} MB' if lim else 'sin límite'}; "
          f"OOM previos = {memoria.oom_kills()}")
    while not detener:
        oom_antes = memoria.oom_kills()
        t0 = time.time()
        hijo = subprocess.Popen([sys.executable, str(AQUI / "worker.py")],
                                env={**__import__("os").environ, "WA_REINICIOS": str(reinicios), "WA_SUPERVISADO": "1"})
        codigo = hijo.wait()
        duro = time.time() - t0
        if detener or codigo == 0:
            break
        if codigo == 78:                     # EX_CONFIG: reiniciar no lo arregla
            print("[supervisor] error de configuración del worker: no se reinicia")
            sys.exit(78)
        oom_despues = memoria.oom_kills()
        es_oom = codigo == -signal.SIGKILL and oom_antes is not None and (oom_despues or 0) > oom_antes
        en_curso = registro.en_curso()
        if es_oom:
            cuentas = registro.anotar_oom([s["subtarea"] for s in en_curso])
            print(f"[supervisor] ✗ OOM: el kernel mató al worker (límite {memoria.limite_mb()} MB, "
                  f"pico {memoria.pico_mb()} MB) con {len(en_curso)} sub-tarea(s) en curso: "
                  + ", ".join(f"{s} ({n}.ª vez)" for s, n in cuentas.items()))
            avisar_coordinador([{"evento": "oom", "trabajo": s["trabajo"], "subtarea": s["subtarea"], "worker": WORKER_ID,
                                 "veces": cuentas[s["subtarea"]], "limite_mb": memoria.limite_mb(),
                                 "pico_mb": memoria.pico_mb(), "hora": time.time()} for s in en_curso]
                               or [{"evento": "oom", "worker": WORKER_ID, "limite_mb": memoria.limite_mb(), "hora": time.time()}])
        else:
            print(f"[supervisor] el worker terminó con código {codigo} tras {duro:.0f} s")
        reinicios += 1
        espera = 1.0 if duro > 60 else min(espera * 2, 30.0)
        print(f"[supervisor] reiniciando el worker en {espera:g} s (reinicio n.º {reinicios})")
        time.sleep(espera)
    print("[supervisor] detenido")


if __name__ == "__main__":
    main()
