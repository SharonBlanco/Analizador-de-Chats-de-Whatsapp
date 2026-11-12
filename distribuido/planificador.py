
"""Planificador de subtareas para el módulo M2."""

from dataclasses import dataclass, field
import itertools
import time

PRIORIDADES = {
    "baja": 1,
    "media": 2,
    "alta": 3,
    "critica": 4,
}

POLITICAS = ("fifo", "prioridad", "aging")


@dataclass
class Tarea:
    id: str
    prioridad: int
    datos: object = None
    llegada: float = field(default_factory=time.monotonic)
    orden: int = 0


class Planificador:
    def __init__(self, politica="aging", intervalo_aging=10.0):
        if politica not in POLITICAS:
            raise ValueError(f"Política inválida: {politica}")
        if intervalo_aging <= 0:
            raise ValueError("El intervalo de aging debe ser positivo")

        self.politica = politica
        self.intervalo_aging = intervalo_aging
        self.colas = {nivel: [] for nivel in range(1, 5)}
        self._contador = itertools.count()

    def agregar(self, tarea):
        if not 1 <= tarea.prioridad <= 4:
            raise ValueError("La prioridad debe estar entre 1 y 4")

        tarea.orden = next(self._contador)
        self.colas[tarea.prioridad].append(tarea)

    def devolver(self, tarea):
        """Reinserta una tarea conservando su orden y llegada originales."""
        if not 1 <= tarea.prioridad <= 4:
            raise ValueError("La prioridad debe estar entre 1 y 4")
        self.colas[tarea.prioridad].append(tarea)

    def prioridad_efectiva(self, tarea, ahora=None):
        if ahora is None:
            ahora = time.monotonic()

        if self.politica != "aging":
            return tarea.prioridad

        espera = max(0.0, ahora - tarea.llegada)
        aumentos = int(espera / self.intervalo_aging)

        return min(4, tarea.prioridad + aumentos)

    def siguiente(self, ahora=None):
        if ahora is None:
            ahora = time.monotonic()

        pendientes = [
            tarea
            for cola in self.colas.values()
            for tarea in cola
        ]

        if not pendientes:
            return None

        if self.politica == "fifo":
            elegida = min(pendientes, key=lambda t: t.orden)

        elif self.politica == "prioridad":
            elegida = min(
                pendientes,
                key=lambda t: (-t.prioridad, t.orden)
            )

        else:
            elegida = min(
                pendientes,
                key=lambda t: (
                    -self.prioridad_efectiva(t, ahora),
                    t.orden
                )
            )

        self.colas[elegida.prioridad].remove(elegida)
        return elegida

    def cancelar(self, tarea_id):
        for cola in self.colas.values():
            for tarea in cola:
                if tarea.id == tarea_id:
                    cola.remove(tarea)
                    return True
        return False

    def pendientes(self):
        return sum(len(cola) for cola in self.colas.values())


def convertir_prioridad(valor: int) -> int:
    """Convierte prioridades del sistema (1-10) a niveles M2 (1-4)."""
    prioridad = int(valor)

    if not 1 <= prioridad <= 10:
        raise ValueError("La prioridad debe estar entre 1 y 10")

    if prioridad <= 2:
        return 1       # Baja
    if prioridad <= 5:
        return 2       # Media
    if prioridad <= 8:
        return 3       # Alta
    return 4           # Crítica
