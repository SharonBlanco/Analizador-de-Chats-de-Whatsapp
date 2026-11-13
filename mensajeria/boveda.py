"""M4: importación opcional a un caso cifrado, sin modificar casos históricos.

La importación original se hace en un directorio temporal, y solo se publica
el caso cifrado tras completar todos los archivos y metadatos. M3 queda intacto.
Este módulo no conecta todavía las lecturas de la interfaz web o los workers.
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path

from cifrado import clave_nueva, leer_clave, cifrar_archivo, descifrar_archivo
from whatsapp_formato import parsear_archivo
from importar import importar


class ErrorBoveda(Exception):
    pass


def _clave(dataset: Path) -> bytes:
    ruta = dataset / '.seguridad' / 'almacen-aes256.key'
    if not ruta.exists():
        try:
            clave_nueva(ruta)
        except FileExistsError:
            pass
    return leer_clave(ruta)


def importar_cifrado(entrada: str | Path, dataset: str | Path, *, fuente: str = 'generador',
                    declaro: bool = False, nombre: str | None = None,
                    prioridad: int = 5, nombre_entrada: str | None = None) -> Path:
    """Importa un caso nuevo como solo-cifrado. No sobrescribe casos existentes.

    Importa temporalmente utilizando el parser M2/M3 y cifra archivos en la carpeta
    final. NO copia el almacén temporal M3 al dataset real; la deduplicación global
    aún no se aplica a casos cifrados.
    """
    dataset = Path(dataset)
    dataset.mkdir(parents=True, exist_ok=True)
    clave = _clave(dataset)
    with tempfile.TemporaryDirectory(prefix='wa-m4-import-') as tmp:
        temporal = Path(tmp)
        etapa = importar(Path(entrada), temporal, fuente, declaro, nombre=nombre,
                         prioridad=prioridad, nombre_entrada=nombre_entrada)
        destino = dataset / etapa.name
        if destino.exists():
            raise FileExistsError(f'Ya existe el caso {destino}; no se sobrescribió')
        meta = json.loads((etapa / 'metadata.json').read_text(encoding='utf-8'))
        # Usamos un nombre temporal en el MISMO volumen para publicar atomícamente.
        trabajo = Path(tempfile.mkdtemp(prefix='.m4-pendiente-', dir=dataset))
        publicado = False
        try:
            archivos = meta.get('files', {})
            for nombre_archivo, datos in archivos.items():
                origen = etapa / nombre_archivo
                if not origen.is_file():
                    raise ErrorBoveda(f'Falta evidencia en la importación: {nombre_archivo}')
                # Nombre literal relativo controlado; nunca aceptar rutas fuera del caso.
                if Path(nombre_archivo).name != nombre_archivo:
                    raise ErrorBoveda(f'Nombre de adjunto inseguro: {nombre_archivo}')
                nombre_cifrado = nombre_archivo + '.m4'
                cifrar_archivo(origen, trabajo / nombre_cifrado, clave)
                datos['cifrado'] = 'AES-256-GCM'
                datos['archivo_cifrado'] = nombre_cifrado
                datos['almacen'] = False
            meta['m4_almacenamiento'] = {'version': 1, 'modo': 'cifrado',
                                         'deduplicacion': 'no_aplica_a_casos_cifrados'}
            meta['deduplicacion'] = {'nuevos': 0, 'repetidos': 0,
                                    'nota': 'M3 no se aplica a este caso cifrado'}
            (trabajo / 'metadata.json').write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
            # os.rename no reemplaza de forma segura un destino preexistente en Windows.
            if destino.exists():
                raise FileExistsError(f'Ya existe {destino}')
            os.rename(trabajo, destino)
            publicado = True
            return destino
        finally:
            if not publicado:
                shutil.rmtree(trabajo, ignore_errors=True)


def es_cifrado(caso: str | Path) -> bool:
    caso = Path(caso)
    meta = json.loads((caso / 'metadata.json').read_text(encoding='utf-8'))
    return meta.get('m4_almacenamiento', {}).get('modo') == 'cifrado'


@contextmanager
def archivo_descifrado(caso: str | Path, nombre: str, clave: bytes):
    """Entrega copia temporal autenticada; la elimina al terminar.

    No usar para publicar archivos a clientes sin control de acceso previo.
    """
    caso = Path(caso)
    meta = json.loads((caso / 'metadata.json').read_text(encoding='utf-8'))
    f = meta.get('files', {}).get(nombre)
    if not f or f.get('cifrado') != 'AES-256-GCM':
        raise ErrorBoveda('Archivo no registrado como evidencia cifrada')
    relativo = f['archivo_cifrado']
    if Path(relativo).name != relativo:
        raise ErrorBoveda('Ruta cifrada no válida')
    with tempfile.TemporaryDirectory(prefix='wa-m4-lectura-') as tmp:
        salida = Path(tmp) / Path(nombre).name
        descifrar_archivo(caso / relativo, salida, clave)
        yield salida


def mensajes_cifrados(caso: str | Path, dataset: str | Path):
    """Adaptador para el parser: descifra chat solo durante la operación."""
    with archivo_descifrado(caso, 'chat.txt', leer_clave(Path(dataset) / '.seguridad' / 'almacen-aes256.key')) as chat:
        return parsear_archivo(chat)
