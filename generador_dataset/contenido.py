"""
Contenido sintético de las conversaciones: nombres ficticios y plantillas de mensajes.

Todo lo de este archivo es inventado por el equipo (con apoyo de IA, declarado en
docs/bitacora_ia.md). Ningún nombre corresponde a una persona real.
"""

NOMBRES = """Ana Bruno Carla Diego Elena Fabián Gabriela Héctor Isabel Javier Karina Luis Mariana Néstor
Olga Pablo Quirina Rodrigo Sofía Tomás Valeria Walter Ximena Yolanda Zacarías Andrea Bernardo Camila Daniel
Esteban Fernanda Gustavo Hilda Ignacio Jimena Kevin Lucía Mauricio Natalia Óscar Paula Ricardo Silvia
Teresa Ulises Verónica Wilberth Yesenia Adrián Brenda César Daniela Emilio Florencia Gerardo""".split()
APELLIDOS = """Rojas Mora Jiménez Vargas Solano Chaves Araya Castro Quesada Salas Villalobos Méndez Ramírez
Herrera Alfaro Brenes Campos Cordero Fallas Guzmán Hidalgo Jara León Monge Navarro Ortiz Porras Rivera
Sandoval Ureña Valverde Zamora""".split()

GRUPOS = ["Proyecto SO", "Familia {apellido}", "Fútbol de los martes", "Compañeros de trabajo", "Viaje a la playa",
          "Grupo de estudio", "Vecinos del condominio", "Cumple de {nombre}", "Equipo de ventas", "Mejenga del jueves",
          "Tareas Redes", "Turno de noche", "Coro de la iglesia", "Club de lectura", "Alquiler compartido",
          "Graduación 2026", "Cuido de los chicos", "Gimnasio 6 a. m.", "Mudanza de {nombre}", "Taller de cerámica"]

LUGARES = ["la U", "el súper", "la soda", "el parque", "la biblioteca", "casa de {nombre}", "el centro",
           "la parada del bus", "el gym", "la oficina", "el mall", "la feria"]
MATERIAS = ["Sistemas Operativos", "Redes", "Bases de Datos", "Compiladores", "Cálculo", "Estadística",
            "Ingeniería de Software", "Arquitectura"]
DIAS = ["hoy", "mañana", "el lunes", "el martes", "el miércoles", "el jueves", "el viernes", "el sábado", "el domingo"]
HORAS = ["a las 8", "a las 10", "al mediodía", "a las 2", "a las 4", "a las 6", "a las 7:30", "después de clases"]
ENLACES = ["https://www.youtube.com/watch?v=dQ{n}xk", "https://maps.google.com/?q=9.93,-84.0{n}",
           "https://drive.google.com/file/d/1A{n}x/view", "https://docs.python.org/3/library/",
           "https://www.wikipedia.org/wiki/Planificaci%C3%B3n", "https://github.com/ejemplo/proyecto-{n}"]
EMOJIS = ["😂", "👍", "🙏", "😅", "❤️", "🔥", "👌", "😬", "🎉", "😴", "🤔", "💪", "😎", "🙌"]

PLANTILLAS = {
    "saludo": ["Buenas", "Hola a todos", "Buenos días", "Qué tal", "Hola {nombre}", "Buenas noches gente",
               "Diay, ¿cómo están?", "Pura vida, ¿todo bien?"],
    "plan": ["¿Nos vemos {dia} {hora} en {lugar}?", "Yo llego como {hora}", "¿Quién se apunta {dia}?",
             "Mejor {dia}, {hora} no puedo", "Los espero en {lugar}", "Voy saliendo para {lugar}",
             "Llego en 10", "Ya estoy en {lugar}", "¿Seguimos en pie para {dia}?", "Se cancela lo de {dia}, perdón"],
    "estudio": ["¿Alguien entendió lo de {materia}?", "Mañana es la entrega de {materia}",
                "Ya subí mi parte al drive", "¿El examen de {materia} es {dia}?", "Me falta la ficha de evidencia",
                "¿Quién hace las mediciones?", "El profe dijo que la defensa es individual",
                "Tengo dudas con el informe de {materia}", "Hagamos llamada {dia} {hora} para repasar",
                "Ya corrí las pruebas, todo bien", "Revisen el repo antes de las {n}"],
    "trabajo": ["El cliente pidió cambios otra vez", "Mañana hay reunión {hora}", "Ya mandé el reporte",
                "¿Quién cubre el turno {dia}?", "La impresora de nuevo no sirve", "Necesito la factura de ayer",
                "Les dejo la agenda de la semana", "Hoy salgo un poco antes"],
    "casa": ["¿Alguien compra pan?", "Ya pagué el agua", "Falta leche y huevos", "¿Quién saca la basura {dia}?",
             "Llegó el recibo de la luz", "Se fue el internet otra vez", "Les dejé comida en la refri",
             "Mi mamá manda saludos"],
    "deporte": ["¿Vieron el partido?", "Qué golazo el de anoche", "El {dia} jugamos {hora}",
                "Me lesioné el tobillo jaja", "Faltan dos para completar", "Llevo los conos y la bola"],
    "respuesta": ["Sí", "Dale", "Ok", "Listo", "De una", "Claro", "Va", "No puedo, perdón", "Tal vez",
                  "Jajaja", "Jaja sí", "Qué dicha", "Qué pereza", "Muchas gracias", "Con gusto", "Ya vi",
                  "Perfecto", "Ahí les aviso", "Ya voy", "Uy, se me olvidó"],
    "pregunta": ["¿Y {nombre}?", "¿Alguien sabe algo?", "¿A qué hora era?", "¿Dónde quedamos?",
                 "¿Me pasan el link?", "¿Ya comieron?", "¿Qué hay que llevar?", "¿Cuánto es por persona?"],
}
TEMAS_POR_GRUPO = {  # temas con más peso según el tipo de chat
    "estudio": ["Proyecto SO", "Grupo de estudio", "Tareas Redes", "Graduación 2026"],
    "trabajo": ["Compañeros de trabajo", "Equipo de ventas", "Turno de noche"],
    "casa": ["Familia", "Vecinos", "Alquiler compartido", "Cuido de los chicos", "Mudanza"],
    "deporte": ["Fútbol", "Mejenga", "Gimnasio"],
}
MULTILINEA = ["Lista para {dia}:\n- refrescos\n- hielo\n- vasos", "Recordatorio:\nentrega {dia}\nsin falta",
              "Ok, entonces queda así:\n1. {nombre} trae comida\n2. yo llevo la música"]
PIES_FOTO = ["Miren esto", "Así quedó", "Foto de hoy", "jaja", "Para que vean", "", "", "", ""]

DOCUMENTOS = [  # documentos adjuntos (PDF generado por el equipo)
    ("Acta_reunion.pdf", ["Acta de reunion", "Asistentes: {nombres}", "Acuerdos: revisar el avance del proyecto",
                          "Proxima reunion: {dia}"]),
    ("Horario_turnos.pdf", ["Horario de turnos", "Lunes a viernes: equipo A", "Fines de semana: equipo B",
                            "Contacto: {nombre}"]),
    ("Lista_compras.pdf", ["Lista de compras", "- arroz", "- frijoles", "- cafe", "Presupuesto aproximado: 25 000"]),
    ("Informe_avance.pdf", ["Informe de avance", "Modulos completados: 3 de 5", "Pendiente: pruebas de carga",
                            "Responsable: {nombre}"]),
]

# Respaldo SOLO si no se colocaron OffendES ni HatEval en fuentes_dataset/texto/.
# Frases agresivas ficticias, sin destinatarios reales, para que el detector tenga positivos.
POSITIVOS_SINTETICOS = [
    ("amenaza", "Te vas a arrepentir de esto, ya vas a ver"),
    ("amenaza", "Sé dónde vivís, mejor cuidate"),
    ("amenaza", "Si volvés a hablar de mí te va a ir muy mal"),
    ("amenaza", "Esto no se queda así, te lo juro"),
    ("amenaza", "Avisale a tu amigo que lo estamos buscando"),
    ("amenaza", "Más te vale pagar antes del viernes o vas a tener problemas"),
    ("amenaza", "La próxima vez no te la vas a acabar"),
    ("amenaza", "Ya sabemos a qué hora salís del trabajo"),
    ("amenaza", "No me provoqués porque no respondo"),
    ("amenaza", "Vas a terminar muy mal si seguís con eso"),
    ("ofensivo", "Sos un inútil, no servís para nada"),
    ("ofensivo", "Qué estúpido sos, de verdad"),
    ("ofensivo", "Cállate, idiota"),
    ("ofensivo", "Nadie te soporta, das asco"),
    ("ofensivo", "Sos la persona más tonta que conozco"),
    ("ofensivo", "Eres un fracasado y todos lo saben"),
    ("ofensivo", "Qué imbécil, otra vez la misma tontería"),
    ("ofensivo", "Andá a molestar a otro lado, payaso"),
    ("ofensivo", "Tu opinión no le importa a nadie, ignorante"),
    ("ofensivo", "Sos un mentiroso de lo peor"),
]
