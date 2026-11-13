"""M4: pruebas en directorios temporales, no tocan dataset_whatsapp."""
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'mensajeria'))
from boveda import importar_cifrado, mensajes_cifrados, archivo_descifrado, es_cifrado
from cifrado import leer_clave, ErrorCifrado


class TestBoveda(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.r = Path(tmp.name)
        self.dataset = self.r / 'dataset'
        self.entrada = self.r / 'chat.txt'
        self.entrada.write_text('12/11/26, 10:00 - Ana: hola\n12/11/26, 10:01 - Beto: mundo\n12/11/26, 10:02 - Ana: fin\n', encoding='utf-8')

    def _importar(self):
        return importar_cifrado(self.entrada, self.dataset, nombre='caso-ficticio')

    def test_importacion_solo_cifrada_y_lectura(self):
        caso = self._importar()
        self.assertTrue(es_cifrado(caso))
        self.assertFalse((caso / 'chat.txt').exists())
        self.assertTrue((caso / 'chat.txt.m4').exists())
        self.assertNotIn(b'Ana: hola', (caso / 'chat.txt.m4').read_bytes())
        ms = mensajes_cifrados(caso, self.dataset)
        self.assertEqual(len(ms), 3)
        self.assertEqual(ms[0].texto, 'hola')
        clave = leer_clave(self.dataset / '.seguridad' / 'almacen-aes256.key')
        with archivo_descifrado(caso, 'chat.txt', clave) as chat:
            self.assertEqual(chat.read_bytes(), self.entrada.read_bytes())
        self.assertFalse(chat.exists())

    def test_adjunto_cifrado_y_recuperado(self):
        self.entrada.write_text(
            '12/11/26, 10:00 - Ana: IMG-0001.jpg (archivo adjunto)\n'
            '12/11/26, 10:01 - Beto: mundo\n'
            '12/11/26, 10:02 - Ana: fin\n', encoding='utf-8')
        adjunto = self.r / 'IMG-0001.jpg'
        contenido = b'\xff\xd8\xff' + b'foto ficticia' * 20
        adjunto.write_bytes(contenido)
        caso = self._importar()
        self.assertFalse((caso / adjunto.name).exists())
        self.assertTrue((caso / (adjunto.name + '.m4')).exists())
        clave = leer_clave(self.dataset / '.seguridad' / 'almacen-aes256.key')
        with archivo_descifrado(caso, adjunto.name, clave) as recuperado:
            self.assertEqual(recuperado.read_bytes(), contenido)
        self.assertEqual(len(mensajes_cifrados(caso, self.dataset)), 3)

    def test_no_sobrescribe_existente(self):
        caso = self._importar()
        con = (caso / 'chat.txt.m4').read_bytes()
        with self.assertRaises(FileExistsError):
            self._importar()
        self.assertEqual(con, (caso / 'chat.txt.m4').read_bytes())

    def test_alteracion_rechazada(self):
        caso = self._importar()
        archivo = caso / 'chat.txt.m4'
        b = bytearray(archivo.read_bytes()); b[-19] ^= 1; archivo.write_bytes(b)
        with self.assertRaises(ErrorCifrado):
            mensajes_cifrados(caso, self.dataset)


if __name__ == '__main__':
    unittest.main()
