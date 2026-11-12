
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


if __name__ == "__main__":
    unittest.main()
