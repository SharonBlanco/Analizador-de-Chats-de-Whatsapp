import sys
import tempfile
import unittest
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'mensajeria'))
from usuarios import Usuarios, autorizado


class TestUsuariosM4(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.archivo = Path(self.temp.name) / 'usuarios.json'
        self.u = Usuarios(self.archivo)

    def test_login(self):
        self.u.crear('ana', 'UnaClaveSegura_2026!', 'analista')
        self.assertEqual(self.u.autenticar('ana', 'UnaClaveSegura_2026!'), 'analista')
        self.assertIsNone(self.u.autenticar('ana', 'incorrecta'))
        self.assertIsNone(self.u.autenticar('nadie', 'incorrecta'))

    def test_no_guarda_password_plano(self):
        self.u.crear('ana', 'UnaClaveSegura_2026!', 'analista')
        self.assertNotIn('UnaClaveSegura_2026!', self.archivo.read_text(encoding='utf-8'))

    def test_roles(self):
        self.assertTrue(autorizado('administrador', 'reiniciar'))
        self.assertFalse(autorizado('analista', 'reiniciar'))
        self.assertFalse(autorizado('desconocido', 'consultar'))

    def test_duplicado(self):
        self.u.crear('ana', 'UnaClaveSegura_2026!', 'analista')
        with self.assertRaises(ValueError):
            self.u.crear('ana', 'OtraClaveSegura_2026!', 'administrador')

if __name__ == '__main__':
    unittest.main()
