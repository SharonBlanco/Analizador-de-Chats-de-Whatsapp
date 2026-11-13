"""Pruebas de cifrado M4 y custodia, aisladas en directorios temporales."""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "mensajeria"))
from cifrado import ErrorCifrado, cifrar_archivo, descifrar_archivo, clave_nueva, leer_clave
from custodia import ErrorCustodia, registrar, verificar


class TestCifrado(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.clave_ruta = clave_nueva(self.dir / "secretos" / "aes.key")
        self.clave = leer_clave(self.clave_ruta)

    def test_roundtrip_binario(self):
        dato = os.urandom(2 * 1024 * 1024 + 5)
        claro = self.dir / "chat.txt"
        claro.write_bytes(dato)
        cifrado = self.dir / "chat.enc"
        recuperado = self.dir / "recuperado.txt"
        cifrar_archivo(claro, cifrado, self.clave)
        self.assertEqual(claro.read_bytes(), dato)
        self.assertNotIn(dato[:32], cifrado.read_bytes())
        descifrar_archivo(cifrado, recuperado, self.clave)
        self.assertEqual(recuperado.read_bytes(), dato)

    def test_modificacion_detectada_sin_texto_parcial(self):
        origen = self.dir / "origen"
        origen.write_bytes(b"evidencia ficticia")
        cifrado = self.dir / "archivo.enc"
        cifrar_archivo(origen, cifrado, self.clave)
        b = bytearray(cifrado.read_bytes())
        b[-18] ^= 1
        cifrado.write_bytes(b)
        recuperado = self.dir / "salida"
        with self.assertRaises(ErrorCifrado):
            descifrar_archivo(cifrado, recuperado, self.clave)
        self.assertFalse(recuperado.exists())

    def test_clave_erronea(self):
        o, c, d = (self.dir / x for x in ("o", "c", "d"))
        o.write_bytes(b"hola")
        cifrar_archivo(o, c, self.clave)
        with self.assertRaises(ErrorCifrado):
            descifrar_archivo(c, d, os.urandom(32))
        self.assertFalse(d.exists())

    def test_clave_nunca_se_sobrescribe(self):
        with self.assertRaises(FileExistsError):
            clave_nueva(self.clave_ruta)


class TestCustodia(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        self.clave = os.urandom(32)
        self.bitacora = self.dir / "custodia.jsonl"

    def test_cadena_valida(self):
        f = self.dir / "evidencia"
        f.write_bytes(b"prueba")
        registrar(self.bitacora, self.clave, accion="importar", actor="admin", caso="demo", archivo=f)
        registrar(self.bitacora, self.clave, accion="consultar", actor="admin", caso="demo")
        estado = verificar(self.bitacora, self.clave)
        self.assertTrue(estado["ok"])
        self.assertEqual(estado["registros"], 2)

    def test_alteracion_detectada(self):
        registrar(self.bitacora, self.clave, accion="importar", actor="admin", caso="demo")
        self.bitacora.write_text(self.bitacora.read_text().replace("admin", "otro"))
        with self.assertRaises(ErrorCustodia):
            verificar(self.bitacora, self.clave)

    def test_sin_clave_no_se_puede_falsificar_sello(self):
        registrar(self.bitacora, self.clave, accion="importar", actor="admin", caso="demo")
        with self.assertRaises(ErrorCustodia):
            verificar(self.bitacora, os.urandom(32))


if __name__ == "__main__":
    unittest.main()
