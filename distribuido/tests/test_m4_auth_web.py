"""Pruebas HTTP M4: login, 401/403, CSRF y logout. No requieren RabbitMQ."""
import http.cookiejar
import json
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import HTTPCookieProcessor, Request, build_opener

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'mensajeria'))
import web
from sesiones import Sesiones
from usuarios import Usuarios


class TestM4HTTP(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temporal = tempfile.TemporaryDirectory()
        cls.usuarios = Usuarios(Path(cls.temporal.name) / 'usuarios.json')
        cls.usuarios.crear('ana', 'PruebaM4_ClaveSegura2026!', 'analista')
        cls.usuarios.crear('admin', 'PruebaM4_AdminSegura2026!', 'administrador')
        web.CUENTAS_M4 = cls.usuarios
        web.SESIONES_M4 = Sesiones()
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), web.Manejador)
        cls.hilo = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.hilo.start()
        cls.url = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.hilo.join(timeout=2)
        cls.temporal.cleanup()

    def setUp(self):
        self.opener = build_opener(HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def pedir(self, path, metodo='GET', datos=None, csrf=None):
        encabezados = {}
        if datos is not None:
            encabezados['Content-Type'] = 'application/json'
        if csrf:
            encabezados['X-CSRF-Token'] = csrf
        solicitud = Request(self.url + path,
            data=json.dumps(datos).encode('utf-8') if datos is not None else (b'' if metodo=='POST' else None),
            headers=encabezados, method=metodo)
        try:
            with self.opener.open(solicitud,timeout=5) as r:
                return r.status, json.loads(r.read()) if 'json' in r.headers.get('Content-Type','') else {}
        except HTTPError as e:
            return e.code, json.loads(e.read())

    def login(self, nombre, clave):
        return self.pedir('/api/auth/login','POST',{'usuario':nombre,'contrasena':clave})

    def test_anonimo_401(self):
        estado, _ = self.pedir('/api/casos')
        self.assertEqual(estado,401)
        estado, _ = self.pedir('/api/plataforma/reiniciar','POST',{})
        self.assertEqual(estado,401)

    def test_login_incorrecto_401(self):
        self.assertEqual(self.login('ana','incorrecta')[0],401)

    def test_analista_denegado_403(self):
        self.assertEqual(self.login('ana','PruebaM4_ClaveSegura2026!')[0],200)
        self.assertEqual(self.pedir('/api/plataforma/reiniciar','POST',{'confirmar':'REINICIAR'})[0],403)

    def test_csrf_y_logout(self):
        self.assertEqual(self.login('admin','PruebaM4_AdminSegura2026!')[0],200)
        self.assertEqual(self.pedir('/api/plataforma/reiniciar','POST',{'confirmar':'REINICIAR'})[0],403)
        estado, perfil = self.pedir('/api/auth/me')
        self.assertEqual(estado,200)
        self.assertEqual(perfil['rol'],'administrador')
        self.assertEqual(self.pedir('/api/auth/logout','POST',csrf=perfil['csrf'])[0],200)
        self.assertEqual(self.pedir('/api/auth/me')[0],401)
