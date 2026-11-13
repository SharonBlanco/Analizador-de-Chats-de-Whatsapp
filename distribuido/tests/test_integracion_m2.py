
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
            "caso": "caso-prueba",
            "chat": "Chat de prueba",
            "m2": True,
            "prioridad": 5,
            "estado": "queued",
            "creado": time.time(),
            "iniciado": None,
            "terminado": None,
            "resultado": None,
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

    def test_cancelar_elimina_pendientes(self):
        """Cancelar un trabajo elimina sus subtareas de M2."""
        from unittest.mock import patch

        self.assertEqual(self.p.planificador.pendientes(), 2)

        with patch.object(self.p, "detalle", return_value={}):
            self.p.cancelar("tr-001")

        self.assertEqual(self.p.planificador.pendientes(), 0)
        self.assertEqual(self.tr["estado"], "cancelled")

        for st in self.tr["subtareas"].values():
            self.assertEqual(st["estado"], "cancelled")

    def test_reanudar_m2_no_publica_subtareas_directamente(self):
        """M2 debe reanudar mediante el planificador, no por publicación directa."""
        from unittest.mock import patch

        self.tr["estado"] = "cancelled"
        self.tr["iniciado"] = None
        self.tr["caso"] = "caso-prueba"
        self.tr["subtareas"]["st-1"]["estado"] = "cancelled"
        self.tr["subtareas"]["st-2"]["estado"] = "completed"

        self.p.planificador.cancelar("st-1")
        self.p.planificador.cancelar("st-2")
        self.p._registrar_pendientes = Mock()
        self.p._despachar = Mock(return_value=False)

        with patch.object(self.p, "detalle", return_value={}):
            self.p.reanudar("tr-001")

        self.assertEqual(
            self.tr["subtareas"]["st-1"]["estado"],
            "pendiente"
        )
        self.assertEqual(
            self.tr["subtareas"]["st-2"]["estado"],
            "completed"
        )
        self.p._registrar_pendientes.assert_called_once()
        self.p._despachar.assert_called_once()

        # Solo debe enviarse el mensaje de control "reanudar".
        self.assertEqual(self.p._publicar.call_count, 1)
        self.assertEqual(
            self.p._publicar.call_args.kwargs["exchange"],
            "wa.control"
        )

    def test_reintento_m2_vuelve_al_planificador(self):
        """Una subtarea fallida debe volver al planificador."""
        st = self.tr["subtareas"]["st-1"]
        st["estado"] = "retrying"
        st["despachada"] = True

        self.p.planificador.cancelar("st-1")
        self.p.planificador.cancelar("st-2")

        self.p._despachar = Mock(return_value=False)
        self.p._registrar_pendientes = Mock()

        self.p._reencolar("tr-001", "st-1")

        self.assertEqual(st["estado"], "pendiente")
        self.assertFalse(st["despachada"])
        self.p._registrar_pendientes.assert_called_once()
        self.p._despachar.assert_called_once()

    def test_recuperacion_reconstruye_pendientes_m2(self):
        """El reinicio debe recuperar las subtareas M2 pendientes."""
        self.p.planificador.cancelar("st-1")
        self.p.planificador.cancelar("st-2")

        self.p._registrar_pendientes = Mock()
        self.p._despachar = Mock(return_value=False)

        self.p._reanudar()

        self.p._registrar_pendientes.assert_called_once()
        self.p._despachar.assert_called_once()


if __name__ == "__main__":
    unittest.main()
