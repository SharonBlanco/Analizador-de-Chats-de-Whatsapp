# M2 · Planificación de subtareas

## Objetivo
Comparar tres políticas del planificador (`fifo`, `prioridad` y `aging`) y comprobar cómo afectan el tiempo de espera de una subtarea de prioridad baja cuando llegan muchas tareas críticas.

## Implementación
- **Planificador:** `distribuido/planificador.py`. Gestiona cuatro niveles (baja=1, media=2, alta=3, crítica=4).
- **FIFO:** atiende primero a la tarea que ingresó antes.
- **Prioridad:** atiende primero a la tarea con mayor prioridad; a igual nivel respeta el orden de llegada.
- **Prioridad con aging:** cada 10 segundos en espera incrementa un nivel de prioridad efectiva, hasta el nivel crítico. Ante empate se respeta el orden original.
- **Coordinador:** integra el planificador bajo la opción `WA_M2_ACTIVO`. La integración requiere validación adicional antes de habilitarla.

## Experimento reproducible
Archivo: `distribuido/experimentos/m2.py`.

Se usa el planificador, **sin RabbitMQ**, con una simulación de eventos discretos para aislar el comportamiento de las políticas. Se agrega una tarea baja en `t=0`, y 60 críticas, una cada segundo desde `t=0` hasta `t=59`. Cada tarea dura 2 segundos en un único servidor simulado. El experimento termina cuando todas han sido atendidas.

**Comando** desde la raíz del repositorio:

```powershell
python distribuido/experimentos/m2.py
```

El script imprime la tabla y genera `distribuido/experimentos/resultados_m2.csv`, que contiene tiempo de llegada, inicio, espera y finalización de cada subtarea. Los resultados son deterministas.

## Resultados de referencia de la simulación

| Política | Espera tarea baja (s) | Espera promedio de todas (s) | Espera máxima (s) | Tareas completadas |
|---|---:|---:|---:|---:|
| FIFO | 0.0 | 30.98 | 61.0 | 61 |
| Prioridad | 120.0 | 30.98 | 120.0 | 61 |
| Aging (10 s) | 30.0 | 30.98 | 61.0 | 61 |

**Interpretación:** En FIFO la tarea baja entra antes que todas las críticas, por lo que se procesa inmediatamente. Bajo prioridad estricta, las críticas la desplazan hasta el final y espera 120 segundos. Con aging, su prioridad efectiva alcanza el nivel crítico tras 30 segundos; como ingresó primero, gana el desempate. Este escenario muestra una reducción importante de la espera frente a prioridad estricta.

El promedio global es igual en estas tres ejecuciones porque el servidor no tiene períodos ociosos y todas las tareas duran exactamente lo mismo; la **distribución de la espera**, especialmente para la tarea baja, es lo que cambia.

## Alcance y limitaciones
- Los tiempos anteriores son **simulados**, no latencias reales de RabbitMQ, red o workers.
- La simulación muestra ausencia de inanición en este escenario acotado; no demuestra una garantía absoluta bajo cualquier patrón de llegada, fallas o capacidad de workers.
- Antes de declarar la integración completa, se requiere verificar el despacho desde `crear()` y al recibir resultados, reintentos, caídas de workers, recuperación de publicaciones inciertas y una prueba extremo a extremo con RabbitMQ.
- Conservar `WA_M2_ACTIVO=0` hasta completar esas pruebas.

## Evidencias para entrega
1. Código `distribuido/planificador.py` y pruebas unitarias.
2. Código de experimento `distribuido/experimentos/m2.py`.
3. CSV `distribuido/experimentos/resultados_m2.csv`.
4. Salida de la ejecución y pruebas (`python -m unittest discover -s distribuido/tests -p "test_*.py" -q`).
5. Pruebas con RabbitMQ reales (pendientes; adjuntar registros y resultados cuando se ejecuten).

## Conclusión
La política **aging** equilibra el tratamiento preferente de tareas urgentes con el tiempo acumulado de espera: en el caso simulado reduce la espera de la tarea baja de 120 a 30 segundos respecto a prioridad estricta. La elección de política depende de las necesidades del sistema, y aún debe validarse su funcionamiento integrado bajo fallos reales.
