
"""M4: sesiones HTTP con identificadores aleatorios y expiración."""

import secrets
import threading
import time


class Sesiones:
    def __init__(self, duracion=3600):
        self.duracion = duracion
        self._sesiones = {}
        self._lock = threading.RLock()

    def crear(self, usuario, rol):
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)

        with self._lock:
            self._sesiones[token] = {
                "usuario": usuario,
                "rol": rol,
                "csrf": csrf,
                "expira": time.time() + self.duracion,
            }

        return token

    def obtener(self, token):
        if not token:
            return None

        with self._lock:
            sesion = self._sesiones.get(token)

            if not sesion:
                return None

            if time.time() >= sesion["expira"]:
                self._sesiones.pop(token, None)
                return None

            return dict(sesion)

    def cerrar(self, token):
        with self._lock:
            return self._sesiones.pop(token, None) is not None

    def verificar_csrf(self, token, csrf):
        sesion = self.obtener(token)

        return bool(
            sesion
            and csrf
            and secrets.compare_digest(sesion["csrf"], csrf)
        )
