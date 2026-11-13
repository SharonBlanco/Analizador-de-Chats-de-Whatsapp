"""M4: bitácora de custodia encadenada y autenticada con HMAC-SHA256.

Una secuencia SHA-256 sola puede ser reescrita por quien modifique el registro.
Este diseño usa una clave HMAC separada, mantenida fuera del repositorio.
No es un registro externo inmutable: un atacante con la clave puede reescribirlo.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

_CANDADO = threading.RLock()
ZERO = "0" * 64


class ErrorCustodia(Exception):
    pass


def sha256_archivo(ruta: str | Path) -> str:
    h = hashlib.sha256()
    with Path(ruta).open("rb") as f:
        while b := f.read(1024 * 1024):
            h.update(b)
    return h.hexdigest()


def _serializar(x: dict) -> bytes:
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sello(entrada: dict, clave: bytes) -> str:
    return hmac.new(clave, _serializar(entrada), hashlib.sha256).hexdigest()


def verificar(ruta: str | Path, clave: bytes) -> dict:
    if len(clave) != 32:
        raise ErrorCustodia("Clave HMAC inválida")
    ruta = Path(ruta)
    if not ruta.exists():
        return {"ok": True, "registros": 0, "ultimo": ZERO}
    anterior, numero = ZERO, 0
    try:
        with ruta.open("r", encoding="utf-8") as f:
            for linea in f:
                numero += 1
                registro = json.loads(linea)
                sello = registro.pop("sello")
                if registro.get("numero") != numero or registro.get("anterior") != anterior:
                    raise ErrorCustodia(f"Cadena alterada en registro {numero}")
                if not hmac.compare_digest(_sello(registro, clave), sello):
                    raise ErrorCustodia(f"Firma HMAC inválida en registro {numero}")
                anterior = sello
    except (ValueError, KeyError, TypeError) as exc:
        raise ErrorCustodia(f"Registro de custodia inválido cerca de línea {numero}") from exc
    return {"ok": True, "registros": numero, "ultimo": anterior}


def registrar(ruta: str | Path, clave: bytes, *, accion: str, actor: str,
             caso: str, archivo: str | Path | None = None) -> dict:
    """Verifica el historial antes de anexar una entrada firmada.

    El registro HMAC encadena acciones y opcionalmente el SHA-256 de un archivo.
    No cambia ni borra el archivo evidencial.
    """
    if not accion or not actor or not caso:
        raise ValueError("Se requieren acción, actor y caso")
    with _CANDADO:
        ruta = Path(ruta)
        estado = verificar(ruta, clave)
        registro = {
            "numero": estado["registros"] + 1,
            "anterior": estado["ultimo"],
            "fecha_utc": datetime.now(timezone.utc).isoformat(),
            "accion": accion,
            "actor": actor,
            "caso": caso,
            "archivo": Path(archivo).name if archivo is not None else None,
            "sha256_archivo": sha256_archivo(archivo) if archivo is not None else None,
        }
        registro["sello"] = _sello(registro, clave)
        ruta.parent.mkdir(parents=True, exist_ok=True)
        with ruta.open("a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(registro, ensure_ascii=False, sort_keys=True) + "\n")
            f.flush()
            os.fsync(f.fileno())
        return registro
