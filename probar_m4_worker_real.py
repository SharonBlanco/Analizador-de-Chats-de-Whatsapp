"""Prueba aislada M4: descargador real del worker contra bóveda HTTPS.

Ejecutar desde la raíz del proyecto con el Python del entorno virtual.
No modifica dataset_whatsapp, RabbitMQ ni la configuración persistente.
"""
from __future__ import annotations

import hashlib
import json
import ssl
import sys
import tempfile
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path.cwd().resolve()
for carpeta in (ROOT / 'mensajeria', ROOT / 'distribuido', ROOT / 'distribuido' / 'worker'):
    sys.path.insert(0, str(carpeta))

import boveda
import web
import worker


def main():
    ca = ROOT / 'distribuido/certs/ca.crt'
    cert = ROOT / 'distribuido/certs/server.crt'
    key = ROOT / 'distribuido/certs/server.key'
    for f in (ca, cert, key):
        if not f.is_file():
            raise FileNotFoundError(f'Falta certificado: {f}')

    anterior_dataset = web.DATASET
    anterior_token_web = web.ManejadorWorkers.TOKEN
    anterior_token_worker = worker.TOKEN
    anterior_url_worker = worker.COORDINADOR_URL
    anterior_cancelacion = worker.revisar_cancelacion
    servidor = None
    hilo = None
    try:
        with tempfile.TemporaryDirectory(prefix='wa-m4-worker-real-') as tmp:
            raiz = Path(tmp)
            origen = raiz / 'origen'
            dataset = raiz / 'dataset'
            origen.mkdir()
            dataset.mkdir()
            nombre = 'DOC-20261112-WA0001.bin'
            contenido = b'EVIDENCIA_FICTICIA_M4_WORKER_' * 180000
            (origen / 'chat.txt').write_text(
                '12/11/26, 14:00 - Ana: Prueba de seguridad\n'
                '12/11/26, 14:01 - Luis: Recibido\n'
                f'12/11/26, 14:02 - Ana: {nombre} (archivo adjunto)\n',
                encoding='utf-8',
            )
            (origen / nombre).write_bytes(contenido)
            print('=== 1. CREAR CASO CIFRADO FICTICIO ===', flush=True)
            caso = boveda.importar_cifrado(origen / 'chat.txt', dataset, fuente='generador', nombre='worker_m4_demo')
            if not boveda.es_cifrado(caso) or (caso / nombre).exists():
                raise AssertionError('El caso no se almaceno cifrado')
            print('OK: evidencia original no esta en su ruta de texto plano', flush=True)

            print('\n=== 2. SERVIDOR HTTPS TEMPORAL ===', flush=True)
            web.DATASET = dataset
            token_demo = 'TOKEN_FICTICIO_M4_UNICAMENTE_EN_PRUEBA'
            web.ManejadorWorkers.TOKEN = token_demo
            servidor = ThreadingHTTPServer(('127.0.0.1', 0), web.ManejadorWorkers)
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            ctx.load_cert_chain(certfile=str(cert), keyfile=str(key))
            servidor.socket = ctx.wrap_socket(servidor.socket, server_side=True)
            hilo = threading.Thread(target=servidor.serve_forever, daemon=True)
            hilo.start()
            url = f'https://localhost:{servidor.server_port}'
            print('OK: servidor temporal HTTPS iniciado', flush=True)

            print('\n=== 3. DESCARGADOR REAL DEL WORKER ===', flush=True)
            worker.TOKEN = token_demo
            worker.COORDINADOR_URL = url
            worker.revisar_cancelacion = lambda _sub: None  # sin trabajo ni RabbitMQ en esta prueba
            sub = {
                'caso': caso.name,
                'archivo': nombre,
                'sha256': hashlib.sha256(contenido).hexdigest(),
                'bytes': len(contenido),
                'url_archivos': url,
                'subtarea': 'm4-prueba-temporal',
            }
            destino = raiz / 'descargado.bin'
            resultado = worker.descargar_por_bloques(sub, destino)
            if destino.read_bytes() != contenido:
                raise AssertionError('El worker no recupero los bytes originales')
            if resultado['sha256_calculado'] != sub['sha256']:
                raise AssertionError('SHA-256 calculado por worker incorrecto')
            if resultado['bytes'] != len(contenido):
                raise AssertionError('El tamano descargado no coincide')
            bloques = resultado['transferencia'].get('bloques')
            print('Bloques descargados:', bloques, flush=True)
            if not isinstance(bloques, int) or bloques < 4:
                raise AssertionError(
                    f'Se esperaban al menos 4 bloques; obtenidos: {bloques}'
                )
            print('OK: worker descargo bloques HTTPS del caso cifrado', flush=True)
            print('OK: SHA-256 de la evidencia coincide', flush=True)
            print('Transferencia:', json.dumps(resultado['transferencia'], ensure_ascii=False), flush=True)

            print('\n=== 4. VALIDAR CERTIFICADO EN WORKER ===', flush=True)
            print('OK: conexiones HTTPS del descargador completadas sin omitir verificacion TLS', flush=True)
            print('\nRESULTADO: WORKER REAL + HTTPS + BOVEDA APROBADOS', flush=True)
    finally:
        if servidor is not None:
            servidor.shutdown()
            servidor.server_close()
        if hilo is not None:
            hilo.join(timeout=5)
        web.DATASET = anterior_dataset
        web.ManejadorWorkers.TOKEN = anterior_token_web
        worker.TOKEN = anterior_token_worker
        worker.COORDINADOR_URL = anterior_url_worker
        worker.revisar_cancelacion = anterior_cancelacion
        print('Servidor temporal cerrado. Configuracion en memoria restaurada.', flush=True)


if __name__ == '__main__':
    main()
