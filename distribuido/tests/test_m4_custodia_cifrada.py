import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "mensajeria"))

import web
from boveda import es_cifrado
from custodia import verificar


class PruebaCustodiaCifrada(unittest.TestCase):

    def test_importacion_cifrada_registra_custodia(self):
        anterior = os.environ.get("WA_M4_CIFRAR_NUEVOS")

        try:
            with tempfile.TemporaryDirectory(prefix="m4-test-custodia-") as tmp:
                raiz = Path(tmp)
                dataset = raiz / "dataset"
                dataset.mkdir()

                chat = raiz / "chat.txt"
                chat.write_text(
                    "12/11/26, 14:00 - Ana: Prueba de seguridad\n"
                    "12/11/26, 14:01 - Luis: Mensaje ficticio\n"
                    "12/11/26, 14:02 - Ana: Confirmado\n",
                    encoding="utf-8"
                )

                os.environ["WA_M4_CIFRAR_NUEVOS"] = "1"

                caso = web.importar(
                    chat,
                    dataset,
                    fuente="generador",
                    declaro=False,
                    nombre="caso_custodia_m4"
                )

                self.assertTrue(es_cifrado(caso))
                self.assertFalse((caso / "chat.txt").exists())

                seguridad = dataset / ".seguridad"
                bitacora = seguridad / "custodia.jsonl"
                clave = seguridad / "custodia.key"

                self.assertTrue(bitacora.is_file())
                self.assertTrue(clave.is_file())

                entradas = [
                    json.loads(linea)
                    for linea in bitacora.read_text(
                        encoding="utf-8"
                    ).splitlines()
                    if linea.strip()
                ]

                self.assertEqual(len(entradas), 1)

                resultado = verificar(bitacora, clave.read_bytes())

                self.assertTrue(resultado["ok"])
                self.assertEqual(resultado["registros"], 1)

        finally:
            if anterior is None:
                os.environ.pop("WA_M4_CIFRAR_NUEVOS", None)
            else:
                os.environ["WA_M4_CIFRAR_NUEVOS"] = anterior


if __name__ == "__main__":
    unittest.main()
