"""M4: cifrado autenticado AES-256-GCM de archivos en bloques.

API de bajo nivel para nuevas evidencias. NO cifra ni borra casos existentes
por sí sola. La integración con importador, almacén y lectores es otra etapa.

La clave de 32 bytes debe guardarse fuera del repositorio y respaldarse
por separado: su pérdida hace irrecuperables los archivos cifrados.
"""
from __future__ import annotations

import os
import secrets
import tempfile
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

MAGIC = b"M4ENC001"  # 8 bytes: versión de formato
NONCE_BYTES = 12
TAG_BYTES = 16
BLOQUE = 1024 * 1024


class ErrorCifrado(Exception):
    """Clave incorrecta, evidencia alterada o formato no válido."""


def clave_nueva(ruta: str | Path) -> Path:
    """Crea una clave nueva solo si no existe; nunca la sobrescribe."""
    ruta = Path(ruta)
    ruta.parent.mkdir(parents=True, exist_ok=True)
    # Apertura exclusiva: protege claves existentes y arranques concurrentes.
    try:
        fd = os.open(str(ruta), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise FileExistsError(f"La clave ya existe: {ruta}") from None
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(secrets.token_bytes(32))
            f.flush()
            os.fsync(f.fileno())
    except Exception:
        ruta.unlink(missing_ok=True)
        raise
    return ruta


def leer_clave(ruta: str | Path) -> bytes:
    clave = Path(ruta).read_bytes()
    if len(clave) != 32:
        raise ErrorCifrado("Clave AES-256 inválida (se requieren 32 bytes)")
    return clave


def _temporal(destino: Path):
    destino.parent.mkdir(parents=True, exist_ok=True)
    fd, nombre = tempfile.mkstemp(prefix=".m4-", suffix=".tmp", dir=destino.parent)
    if os.name != "nt":
        os.fchmod(fd, 0o600)
    return fd, Path(nombre)


def _validar_rutas(origen: Path, destino: Path):
    if origen.resolve() == destino.resolve():
        raise ValueError("El origen y el destino deben ser distintos")
    if not origen.is_file():
        raise FileNotFoundError(origen)
    if destino.exists():
        raise FileExistsError(f"Destino ya existe: {destino}")


def cifrar_archivo(origen: str | Path, destino: str | Path, clave: bytes) -> Path:
    """Cifra en streaming y escribe un archivo nuevo (sin borrar el original).

    Formato: MAGIC(8) + nonce(12) + ciphertext(N) + tag(16).
    MAGIC es AAD para autenticar también la versión de formato.
    """
    if len(clave) != 32:
        raise ErrorCifrado("Clave AES-256 inválida")
    origen, destino = Path(origen), Path(destino)
    _validar_rutas(origen, destino)
    nonce = secrets.token_bytes(NONCE_BYTES)
    cifrador = Cipher(algorithms.AES(clave), modes.GCM(nonce)).encryptor()
    cifrador.authenticate_additional_data(MAGIC)
    fd, temporal = _temporal(destino)
    try:
        with origen.open("rb") as entrada, os.fdopen(fd, "wb") as salida:
            salida.write(MAGIC + nonce)
            while bloque := entrada.read(BLOQUE):
                salida.write(cifrador.update(bloque))
            salida.write(cifrador.finalize())
            salida.write(cifrador.tag)
            salida.flush()
            os.fsync(salida.fileno())
        if destino.exists():
            raise FileExistsError(destino)
        os.replace(temporal, destino)
        return destino
    finally:
        temporal.unlink(missing_ok=True)


def descifrar_archivo(origen: str | Path, destino: str | Path, clave: bytes) -> Path:
    """Descifra verificando autenticidad antes de publicar el archivo destino.

    Si falla el tag, elimina el temporal para no exponer texto parcial.
    """
    if len(clave) != 32:
        raise ErrorCifrado("Clave AES-256 inválida")
    origen, destino = Path(origen), Path(destino)
    _validar_rutas(origen, destino)
    tamano = origen.stat().st_size
    if tamano < len(MAGIC) + NONCE_BYTES + TAG_BYTES:
        raise ErrorCifrado("Archivo cifrado incompleto")
    with origen.open("rb") as entrada:
        cabecera = entrada.read(len(MAGIC))
        if cabecera != MAGIC:
            raise ErrorCifrado("Formato de archivo cifrado desconocido")
        nonce = entrada.read(NONCE_BYTES)
        entrada.seek(-TAG_BYTES, os.SEEK_END)
        tag = entrada.read(TAG_BYTES)
        entrada.seek(len(MAGIC) + NONCE_BYTES)
        restante = tamano - len(MAGIC) - NONCE_BYTES - TAG_BYTES
        descifrador = Cipher(algorithms.AES(clave), modes.GCM(nonce, tag)).decryptor()
        descifrador.authenticate_additional_data(MAGIC)
        fd, temporal = _temporal(destino)
        try:
            with os.fdopen(fd, "wb") as salida:
                while restante:
                    bloque = entrada.read(min(BLOQUE, restante))
                    if not bloque:
                        raise ErrorCifrado("Archivo cifrado truncado")
                    salida.write(descifrador.update(bloque))
                    restante -= len(bloque)
                try:
                    salida.write(descifrador.finalize())
                except InvalidTag as exc:
                    raise ErrorCifrado("Autenticación fallida: clave incorrecta o archivo alterado") from exc
                salida.flush()
                os.fsync(salida.fileno())
            if destino.exists():
                raise FileExistsError(destino)
            os.replace(temporal, destino)
            return destino
        finally:
            temporal.unlink(missing_ok=True)
