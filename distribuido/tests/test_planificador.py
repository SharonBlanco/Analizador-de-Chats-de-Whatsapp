
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from planificador import Planificador, Tarea, convertir_prioridad


class TestPlanificador(unittest.TestCase):

    def test_fifo(self):
        p = Planificador("fifo")
        p.agregar(Tarea("baja", 1))
        p.agregar(Tarea("critica", 4))

        self.assertEqual(p.siguiente().id, "baja")
        self.assertEqual(p.siguiente().id, "critica")

    def test_prioridad(self):
        p = Planificador("prioridad")
        p.agregar(Tarea("baja", 1))
        p.agregar(Tarea("critica", 4))

        self.assertEqual(p.siguiente().id, "critica")

    def test_aging(self):
        p = Planificador("aging", intervalo_aging=10)
        p.agregar(Tarea("antigua", 1, llegada=0))
        p.agregar(Tarea("critica", 4, llegada=35))

        self.assertEqual(p.siguiente(ahora=35).id, "antigua")

    def test_conversion(self):
        esperado = [1, 1, 2, 2, 2, 3, 3, 3, 4, 4]

        self.assertEqual(
            [convertir_prioridad(i) for i in range(1, 11)],
            esperado
        )

    def test_devolver_conserva_orden(self):
        p = Planificador("fifo")
        p.agregar(Tarea("primera", 1))
        p.agregar(Tarea("segunda", 4))

        tarea = p.siguiente()
        p.devolver(tarea)

        self.assertEqual(p.siguiente().id, "primera")

    def test_filtrar_por_pool(self):
        p = Planificador("prioridad")
        p.agregar(Tarea("audio", 4, ("cola.audio", {}, 4)))
        p.agregar(Tarea("texto", 2, ("cola.texto", {}, 2)))

        self.assertEqual(
            p.siguiente(pools=["cola.texto"]).id,
            "texto"
        )
        self.assertEqual(p.pendientes(), 1)
    
    def test_aging_evitar_inanicion(self):
        """Una tarea antigua debe adelantarse a críticas recientes."""
        p = Planificador("aging", intervalo_aging=10)
        p.agregar(Tarea("baja_antigua", 1, llegada=0))
        for i in range(20):
            p.agregar(
                Tarea(
                    f"critica_{i}",
                    4,
                    llegada=31 + i
                )
            )
        seleccionada = p.siguiente(ahora=60)
        self.assertEqual(seleccionada.id, "baja_antigua")

    def test_worker_multipool_no_duplica_capacidad(self):
        """Una plaza compartida no debe contarse dos veces."""
        import time
        from pathlib import Path
        sys.path.insert(
            0, str(Path(__file__).resolve().parents[2] / "mensajeria")
        )
        from coordinador import Plataforma
        plataforma = Plataforma(Path("."), lambda caso: ([], {}))
        plataforma.workers["worker-1"] = {
            "worker": "worker-1",
            "pools": ["texto", "imagen"],
            "concurrencia": 1,
            "en_curso": 0,
            "visto": time.time(),
        }
        disponibles = plataforma._workers_disponibles()
        self.assertEqual(len(disponibles), 1)
        self.assertEqual(disponibles[0]["libres"], 1)
        self.assertEqual(
            set(disponibles[0]["pools"]),
            {"texto", "imagen"}
        )

    def test_reserva_reduce_capacidad(self):
        """Una reserva consume una plaza compartida."""
        import time
        from pathlib import Path

        sys.path.insert(
            0, str(Path(__file__).resolve().parents[2] / "mensajeria")
        )
        from coordinador import Plataforma
        plataforma = Plataforma(Path("."), lambda caso: ([], {}))

        plataforma.workers["worker-1"] = {
            "worker": "worker-1",
            "pools": ["texto", "imagen"],
            "concurrencia": 1,
            "en_curso": 0,
            "visto": time.time(),
        }

        # Antes de reservar, hay una plaza disponible.
        disponibles = plataforma._workers_con_reservas()
        self.assertEqual(len(disponibles), 1)
        self.assertEqual(disponibles[0]["libres"], 1)
        # Reservamos la plaza para una subtarea.
        plataforma.reservas["st-001"] = "worker-1"
        # La plaza ya no debe estar disponible.
        disponibles = plataforma._workers_con_reservas()
        self.assertEqual(disponibles, [])
        # Al liberar la reserva, vuelve a estar disponible.
        del plataforma.reservas["st-001"]
        disponibles = plataforma._workers_con_reservas()
        self.assertEqual(len(disponibles), 1)
        self.assertEqual(disponibles[0]["libres"], 1)

    def test_reserva_no_duplica_tarea_en_ejecucion(self):
        """Una tarea iniciada no debe descontar capacidad dos veces."""
        import time
        from pathlib import Path
        sys.path.insert(
            0, str(Path(__file__).resolve().parents[2] / "mensajeria")
        )
        from coordinador import Plataforma
        plataforma = Plataforma(Path("."), lambda caso: ([], {}))
        plataforma.workers["worker-1"] = {
            "worker": "worker-1",
            "pools": ["texto", "imagen"],
            "concurrencia": 2,
            "en_curso": 1,
            "visto": time.time(),
        }
        plataforma.trabajos["tr-001"] = {
            "id": "tr-001",
            "subtareas": {
                "st-001": {
                    "id": "st-001",
                    "estado": "procesando",
                    "worker": "worker-1",
                }
            },
        }
        plataforma.reservas["st-001"] = "worker-1"

        disponibles = plataforma._workers_con_reservas()

        self.assertEqual(len(disponibles), 1)
        self.assertEqual(disponibles[0]["libres"], 1)

    def test_reserva_con_latido_atrasado(self):
        """No liberar una plaza si el latido aún no refleja la tarea."""
        import time
        from pathlib import Path

        sys.path.insert(
            0, str(Path(__file__).resolve().parents[2] / "mensajeria")
        )
        from coordinador import Plataforma
        plataforma = Plataforma(Path("."), lambda caso: ([], {}))

        plataforma.workers["worker-1"] = {
            "worker": "worker-1",
            "pools": ["texto", "imagen"],
            "concurrencia": 1,
            "en_curso": 0,
            "visto": time.time(),
        }

        plataforma.trabajos["tr-001"] = {
            "id": "tr-001",
            "subtareas": {
                "st-001": {
                    "id": "st-001",
                    "estado": "procesando",
                    "worker": "worker-1",
                }
            },
        }

        plataforma.reservas["st-001"] = "worker-1"

        disponibles = plataforma._workers_con_reservas()

        self.assertEqual(disponibles, [])

    def test_registrar_pendientes_sin_duplicados(self):
        """Registrar una subtarea dos veces no debe duplicarla."""
        from pathlib import Path
        import sys

        sys.path.insert(
            0, str(Path(__file__).resolve().parents[2] / "mensajeria")
        )
        from coordinador import Plataforma

        plataforma = Plataforma(Path("."), lambda caso: ([], {}))

        trabajo = {
            "id": "tr-001",
            "prioridad": 5,
            "subtareas": {
                "st-001": {
                    "id": "st-001",
                    "tipo": "texto",
                    "estado": "pendiente",
                    "despachada": False,
                    "ini": 0,
                    "fin": 0,
                }
            },
        }

        agregadas = plataforma._registrar_pendientes(
            trabajo, [], {}
        )

        self.assertEqual(agregadas, 1)
        self.assertEqual(plataforma.planificador.pendientes(), 1)

        agregadas = plataforma._registrar_pendientes(
            trabajo, [], {}
        )

        self.assertEqual(agregadas, 0)
        self.assertEqual(plataforma.planificador.pendientes(), 1)


if __name__ == "__main__":
    unittest.main()
