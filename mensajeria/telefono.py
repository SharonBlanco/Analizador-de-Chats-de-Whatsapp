"""
Importar chats desde un teléfono Android conectado por USB, desde la interfaz web.

Usa ADB (el que trae la librería adbutils, no hace falta instalarlo aparte) y uiautomator2
para tocar los mismos botones que tocaría una persona en WhatsApp: abrir el chat →
Más opciones → Más → Exportar chat → Incluir archivos → Guardar en Descargas.
La lógica de los toques está en exportar_whatsapp.py (raíz del proyecto).

Requisitos en el teléfono (una vez):
  Ajustes → Acerca del teléfono → tocar 7 veces "Número de compilación" → Opciones de
  desarrollador → activar "Depuración USB". Al conectar el cable, aceptar "¿Permitir depuración USB?".
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

RAIZ = Path(__file__).resolve().parent.parent
if str(RAIZ) not in sys.path:
    sys.path.insert(0, str(RAIZ))

AYUDA_ESTADO = {
    "device": "Listo para importar.",
    "unauthorized": "Desbloquee el teléfono y toque «Permitir» en el aviso «¿Permitir depuración USB?». "
                    "Si no aparece, desconecte y vuelva a conectar el cable.",
    "offline": "El teléfono no responde: desconecte y vuelva a conectar el cable.",
    "no permissions": "Linux no da permiso para usar el USB del teléfono. Instale las reglas para Android "
                      "(sudo apt install android-sdk-platform-tools-common), desconecte y vuelva a conectar. "
                      "Si usa VS Code instalado como Flatpak, abra la app desde una terminal normal.",
}
SIN_TELEFONO = ("No se detecta ningún teléfono. Revise: (1) cable USB de datos, no solo de carga; "
                "(2) «Depuración USB» activada en Opciones de desarrollador; "
                "(3) en la notificación USB del teléfono, modo «Transferencia de archivos».")


def _interfaces_adb() -> int:
    """Cuántos dispositivos USB ofrecen la interfaz de depuración de Android (clase ff, subclase 42,
    protocolo 01). Solo Linux; en otros sistemas devuelve 0."""
    n = 0
    for clase in Path("/sys/bus/usb/devices").glob("*/*:*/bInterfaceClass"):
        d = clase.parent
        try:
            if (clase.read_text().strip(), (d / "bInterfaceSubClass").read_text().strip(),
                    (d / "bInterfaceProtocol").read_text().strip()) == ("ff", "42", "01"):
                n += 1
        except OSError:
            pass
    return n


_ultimo_reinicio = [0.0]


def _reiniciar_adb() -> None:
    """El servidor de ADB solo ve los teléfonos conectados ANTES de arrancar cuando no recibe los avisos
    de conexión del sistema (pasa dentro de VS Code instalado como Flatpak). Reiniciarlo lo obliga a buscar."""
    import adbutils
    _ultimo_reinicio[0] = time.time()
    for orden in ("kill-server", "start-server"):
        subprocess.run([adbutils.adb_path(), orden], capture_output=True, timeout=20)
    time.sleep(1.5)


def dispositivos() -> dict:
    """Teléfonos conectados por ADB, con su estado y qué hacer si no están listos."""
    try:
        import adbutils
    except ImportError:
        return {"disponible": False, "dispositivos": [],
                "error": "Falta la librería uiautomator2 (pip install -r requirements.txt)"}
    try:
        lista = adbutils.adb.list()
        # Hay un teléfono con depuración USB enchufado pero ADB no lo ve: reiniciar ADB (como mucho cada 10 s)
        if len(lista) < _interfaces_adb() and time.time() - _ultimo_reinicio[0] > 10:
            _reiniciar_adb()
            lista = adbutils.adb.list()
    except Exception as e:  # noqa: BLE001 — el servidor ADB no arrancó
        return {"disponible": False, "dispositivos": [], "error": f"No se pudo iniciar ADB: {e}"}
    salida = []
    for info in lista:
        estado = info.state if info.state in AYUDA_ESTADO else ("no permissions" if "permission" in info.state else info.state)
        d = {"serial": info.serial, "estado": estado, "ayuda": AYUDA_ESTADO.get(estado, f"Estado: {info.state}")}
        if estado == "device":
            try:
                dev = adbutils.adb.device(info.serial)
                d["modelo"] = f'{dev.prop.get("ro.product.manufacturer") or ""} {dev.prop.model or ""}'.strip()
                d["android"] = dev.prop.get("ro.build.version.release")
                d["whatsapp"] = bool(dev.shell("pm list packages com.whatsapp").strip())
                if not d["whatsapp"]:
                    d["ayuda"] = "El teléfono está listo, pero no tiene WhatsApp instalado."
            except Exception as e:  # noqa: BLE001
                d["ayuda"] = f"Conectado, pero no se pudo leer la información: {e}"
        salida.append(d)
    return {"disponible": True, "dispositivos": salida, "ayuda": None if salida else SIN_TELEFONO}


def _conectar(serial: str):
    import uiautomator2 as u2
    # La primera vez uiautomator2 instala en el teléfono un pequeño servicio de automatización;
    # algunos teléfonos (Xiaomi, Oppo…) piden aceptar «Instalar vía USB».
    return u2.connect(serial)


_candado_tel = threading.Lock()          # el teléfono no puede atender dos operaciones a la vez
_ocupado = {"id": None}                  # importación en curso (se reserva ANTES de arrancar el hilo)
_candado_reserva = threading.Lock()


def chats_recientes(serial: str) -> list[str]:
    from exportar_whatsapp import chats_recientes as leer
    with _candado_tel:
        return leer(_conectar(serial))


# ============================================================ exportación en segundo plano
PASOS = ["Conectar con el teléfono", "Exportar el chat en WhatsApp", "Copiar a la computadora", "Importar a la plataforma"]
_trabajos: dict[str, dict] = {}


def iniciar(serial: str, chat: str, con_archivos: bool, importar: Callable[[Path], str]) -> str:
    """Arranca la exportación en un hilo. importar(ruta_zip) → nombre del caso importado."""
    if not chat.strip():
        raise ValueError("Indique el nombre del chat tal como aparece en WhatsApp")
    id_ = uuid.uuid4().hex[:8]
    with _candado_reserva:
        if _ocupado["id"]:
            raise ValueError("Ya hay una importación en curso desde el teléfono")
        _ocupado["id"] = id_
    t = {"id": id_, "chat": chat, "estado": "en curso", "paso": 0, "pasos": PASOS, "mensajes": [],
         "caso": None, "error": None, "inicio": time.time()}
    _trabajos[id_] = t

    def avisar(texto: str) -> None:
        t["mensajes"].append({"hora": time.strftime("%H:%M:%S"), "texto": texto})
        if "copiando" in texto.lower():
            t["paso"] = 2

    def correr() -> None:
        carpeta = Path(tempfile.mkdtemp(prefix="wa_telefono_"))
        final, error = "error", None
        try:
            with _candado_tel:
                avisar("Conectando con el teléfono… (la primera vez puede pedir aceptar una instalación)")
                d = _conectar(serial)
                avisar(f"Conectado a {d.info.get('productName', 'teléfono')}")
                t["paso"] = 1
                from exportar_whatsapp import exportar
                archivo = exportar(d, chat, con_archivos=con_archivos, avisar=avisar, destino_pc=carpeta)
            t["paso"] = 3
            avisar("Importando: análisis del formato, adjuntos y SHA-256…")
            t["caso"] = importar(archivo)
            t["paso"] = 4
            final = "listo"
        except Exception as e:  # noqa: BLE001 — se muestra en la interfaz
            error = f"{e}"
        finally:
            # Primero se borra la copia temporal del chat (ya se importó, o falló) y recién después se
            # anuncia el final: quien lea "listo" tiene la garantía de que no quedó una copia suelta.
            shutil.rmtree(carpeta, ignore_errors=True)
            if final == "listo":
                avisar(f"✔ Importado como {t['caso']}. La copia del chat sigue en Descargas del teléfono.")
            else:
                avisar(f"✗ {error}")
            t["error"], t["estado"] = error, final
            with _candado_reserva:
                _ocupado["id"] = None

    threading.Thread(target=correr, daemon=True, name=f"telefono-{id_}").start()
    return id_


def estado(id_: str) -> dict | None:
    t = _trabajos.get(id_)
    return dict(t, segundos=round(time.time() - t["inicio"], 1)) if t else None
