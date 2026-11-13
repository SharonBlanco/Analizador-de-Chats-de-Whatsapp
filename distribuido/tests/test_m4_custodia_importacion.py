"""Prueba extremo a extremo del registro de custodia de importaciones nuevas."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'mensajeria'))
from importar import importar
from cifrado import leer_clave
from custodia import verificar, ErrorCustodia


class TestCustodiaImportacion(unittest.TestCase):
    def test_importacion_ficticia_crea_bitacora_verificable(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            entrada = tmp / 'chat.txt'
            entrada.write_text('12/11/26, 10:00 - Ana: hola\n12/11/26, 10:01 - Luis: bien\n12/11/26, 10:02 - Ana: adios\n', encoding='utf-8')
            dataset = tmp / 'dataset'
            caso = importar(entrada, dataset, fuente='generador', declaro=False)
            self.assertTrue((caso / 'chat.txt').is_file())  # Compatibilidad M3 actual.
            clave = leer_clave(dataset / '.seguridad' / 'custodia.key')
            log = dataset / '.seguridad' / 'custodia.jsonl'
            estado = verificar(log, clave)
            self.assertEqual(estado['registros'], 1)
            with log.open('a', encoding='utf-8') as f:
                f.write('{"falso": true}\n')
            with self.assertRaises(ErrorCustodia):
                verificar(log, clave)


if __name__ == '__main__':
    unittest.main()
