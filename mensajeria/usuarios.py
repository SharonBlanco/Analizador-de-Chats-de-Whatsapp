"""M4: cuentas locales con hashes scrypt y permisos basados en roles."""
import getpass
import hashlib
import hmac
import json
import os
import secrets
import tempfile
from pathlib import Path

PERMISOS = {
    'analista': frozenset({'consultar', 'importar', 'analizar'}),
    'supervisor': frozenset({'consultar', 'importar', 'analizar', 'cancelar', 'reanudar'}),
    'administrador': frozenset({'consultar', 'importar', 'analizar', 'cancelar', 'reanudar',
                                'eliminar', 'reiniciar', 'usuarios', 'configurar'}),
}


def autorizado(rol: str, accion: str) -> bool:
    return accion in PERMISOS.get(rol, ())


class Usuarios:
    def __init__(self, ruta):
        self.ruta = Path(ruta)

    def _leer(self):
        if not self.ruta.exists():
            return {'version': 1, 'usuarios': {}}
        datos = json.loads(self.ruta.read_text(encoding='utf-8'))
        if datos.get('version') != 1 or not isinstance(datos.get('usuarios'), dict):
            raise ValueError('Archivo de usuarios inválido')
        return datos

    def _guardar(self, datos):
        self.ruta.parent.mkdir(parents=True, exist_ok=True)
        fd, temporal = tempfile.mkstemp(dir=self.ruta.parent, prefix='.usuarios-', suffix='.tmp')
        try:
            if os.name != 'nt':
                os.fchmod(fd, 0o600)
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(datos, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporal, self.ruta)
        finally:
            if os.path.exists(temporal):
                os.unlink(temporal)

    def crear(self, nombre: str, clave: str, rol: str) -> None:
        nombre = nombre.strip()
        if not nombre or len(nombre) > 64 or any(c.isspace() for c in nombre):
            raise ValueError('Nombre de usuario inválido')
        if rol not in PERMISOS:
            raise ValueError('Rol inválido')
        if len(clave) < 12:
            raise ValueError('La contraseña debe tener al menos 12 caracteres')
        datos = self._leer()
        if nombre in datos['usuarios']:
            raise ValueError('El usuario ya existe')
        sal = secrets.token_bytes(16)
        digest = hashlib.scrypt(clave.encode('utf-8'), salt=sal, n=2**14, r=8, p=1, dklen=32)
        datos['usuarios'][nombre] = {'rol': rol, 'salt': sal.hex(), 'hash': digest.hex(), 'kdf': 'scrypt'}
        self._guardar(datos)

    def autenticar(self, nombre: str, clave: str):
        cuenta = self._leer()['usuarios'].get(nombre)
        if not cuenta:
            hashlib.scrypt(clave.encode('utf-8'), salt=b'0' * 16, n=2**14, r=8, p=1, dklen=32)
            return None
        if cuenta.get('kdf') != 'scrypt':
            return None
        try:
            sal = bytes.fromhex(cuenta['salt'])
            esperado = bytes.fromhex(cuenta['hash'])
            obtenido = hashlib.scrypt(clave.encode('utf-8'), salt=sal, n=2**14, r=8, p=1, dklen=32)
        except (ValueError, KeyError, TypeError):
            return None
        return cuenta['rol'] if hmac.compare_digest(obtenido, esperado) else None


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument('--crear', required=True)
    ap.add_argument('--rol', required=True, choices=sorted(PERMISOS))
    ap.add_argument('--archivo', default='dataset_whatsapp/.seguridad/usuarios.json')
    args = ap.parse_args()
    clave = getpass.getpass('Contraseña (mínimo 12 caracteres): ')
    confirmacion = getpass.getpass('Repetir contraseña: ')
    if clave != confirmacion:
        ap.error('Las contraseñas no coinciden')
    try:
        Usuarios(args.archivo).crear(args.crear, clave, args.rol)
    except ValueError as e:
        ap.error(str(e))
    print('Cuenta creada correctamente.')
