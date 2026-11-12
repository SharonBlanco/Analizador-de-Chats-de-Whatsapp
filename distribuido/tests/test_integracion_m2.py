
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

RAIZ = Path(__file__).resolve().parents[2]
sys.path[:0] = [
    str(RAIZ / "distribuido"),
    str(RAIZ / "mensajeria"),
]

from coordinador import Plataforma
from planificador import Tarea


class TestIntegracionM2(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

        self.p = Plataforma(
            Path(self.tmp.name),
            lambda caso: ([], {})
        )
        self.p.dir.mkdir(parents=True, exist_ok=True)
        self.p.m2_activo = True
        self.p.conectado = True
        self.p._publicar = Mock()

        self.p.workers["worker-1"] = {
            "worker": "worker-1",
            "pools": ["texto"],
            "concurrencia": 1,
            "en_curso": 0,
            "visto": time.time(),
        }

        subtareas = {}
        for i in (1, 2):
            id_st = f"st-{i}"
            subtareas[id_st] = {
                "id": id_st,
                "estado": "pendiente",
                "despachada": False,
            }

        self.tr = {
            "id": "tr-001",
            "m2": True,
            "estado": "queued",
            "subtareas": subtareas,
        }
        self.p.trabajos["tr-001"] = self.tr

        for i in (1, 2):
            id_st = f"st-{i}"
            self.p.planificador.agregar(
                Tarea(
                    id_st,
                    2,
                    ("wa.tareas.texto", {
                        "trabajo": "tr-001",
                        "subtarea": id_st
                    }, 5)
                )
            )

    def test_publica_solo_una(self):
        self.assertTrue(self.p._despachar())
        self.assertEqual(self.p._publicar.call_count, 1)
        self.assertFalse(self.p._despachar())
        self.assertEqual(self.p.planificador.pendientes(), 1)

    def test_continua_tras_finalizar(self):
        self.assertTrue(self.p._despachar())

        self.tr["subtareas"]["st-1"]["estado"] = "completed"

        self.assertTrue(self.p._despachar())
        self.assertEqual(self.p._publicar.call_count, 2)
        self.assertEqual(self.p.planificador.pendientes(), 0)

    def test_publicacion_incierta_bloquea(self):
        self.tr["subtareas"]["st-1"]["publicacion_incierta"] = True

        self.assertFalse(self.p._despachar())
        self.p._publicar.assert_not_called()


if __name__ == "__main__":
    unittest.main()
