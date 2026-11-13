# M?dulo 4 ? Seguridad

## 1. Introducci?n

El M?dulo 4 incorpora controles de seguridad al Analizador de Chats de WhatsApp para proteger el acceso a la plataforma, la informaci?n almacenada, la integridad de las evidencias y las comunicaciones entre los componentes distribuidos.

La implementaci?n se realiz? sobre la arquitectura existente de Marea, sin reemplazar sus funcionalidades de an?lisis ni su infraestructura de workers.

## 2. Objetivo

Fortalecer la confidencialidad, integridad y el control de acceso a la informaci?n procesada por el sistema, manteniendo la compatibilidad con el procesamiento distribuido.

## 3. Controles de seguridad implementados

### 3.1. Autenticaci?n y autorizaci?n

Se implement? un sistema de usuarios con contrase?as protegidas mediante `hashlib.scrypt`.

El sistema contempla los roles:

- Analista.
- Supervisor.
- Administrador.

Se incorporaron sesiones de usuario, cierre de sesi?n, restricciones de acceso seg?n el rol y controles CSRF para operaciones protegidas.

Las pruebas verificaron respuestas HTTP 401 para accesos no autenticados y HTTP 403 para operaciones sin permisos suficientes.

Archivos relevantes:

- `mensajeria/usuarios.py`
- `mensajeria/web.py`
- `distribuido/tests/test_m4_auth_web.py`

### 3.2. Cifrado de informaci?n almacenada

Se implement? cifrado AES-256-GCM para proteger los archivos de evidencia almacenados en la b?veda.

La soluci?n permite trabajar con casos cifrados sin exigir que sus archivos originales permanezcan disponibles en texto plano dentro de la carpeta del caso.

El cifrado autom?tico de nuevas importaciones realizadas desde Marea puede activarse mediante:

```powershell
$env:WA_M4_CIFRAR_NUEVOS = "1"
```

La opci?n se encuentra desactivada por defecto cuando no se establece esa variable.

Archivos relevantes:

- `mensajeria/cifrado.py`
- `mensajeria/boveda.py`
- `mensajeria/web.py`

### 3.3. Integridad y cadena de custodia

Se incorpor? una bit?cora de custodia con protecci?n de integridad mediante HMAC-SHA256 y encadenamiento de registros.

Las nuevas importaciones cifradas realizadas a trav?s del flujo de Marea generan un registro en la bit?cora del dataset correspondiente.

En una prueba con informaci?n ficticia se verific?:

- Importaci?n de un caso cifrado.
- Ausencia de `chat.txt` en texto plano dentro de la carpeta final del caso.
- Creaci?n de `custodia.jsonl`.
- Creaci?n de la clave correspondiente.
- Verificaci?n correcta de un registro de custodia.

Resultado obtenido:

```text
{'ok': True, 'registros': 1, ...}
```

Archivos relevantes:

- `mensajeria/custodia.py`
- `mensajeria/importar.py`
- `mensajeria/web.py`
- `distribuido/tests/test_m4_custodia_cifrada.py`

### 3.4. Comunicaciones HTTPS

Se incorpor? HTTPS en la interfaz Marea y en el servidor utilizado para obtener evidencias desde los workers.

Se utilizaron certificados locales para las pruebas de TLS. Tambi?n se verificaron solicitudes HTTPS para consultar manifiestos y transferir adjuntos.

### 3.5. Comunicaciones AMQPS

RabbitMQ utiliza el puerto `5671` para conexiones AMQP protegidas mediante TLS.

Se deshabilit? el listener AMQP sin cifrar en el puerto `5672` y se elimin? su publicaci?n en Docker Compose.

Configuraci?n aplicada:

```ini
listeners.tcp = none
listeners.ssl.default = 5671
```

Los diagn?sticos posteriores confirmaron:

- Cuatro conexiones activas utilizando TLS.
- Cero conexiones AMQP sin TLS.
- El puerto `5672` no disponible desde el equipo.
- El listener interno `5671` activo.
- Siete colas con consumidores conectados tras el reinicio.

Archivos relevantes:

- `distribuido/docker-compose.yml`
- `distribuido/rabbitmq.conf`
- `distribuido/worker/worker.py`

## 4. Pruebas realizadas

### 4.1. Pruebas automatizadas

Comando de ejecuci?n:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s distribuido/tests -p "test_m4_*.py" -q
```

Resultado:

```text
Ran 17 tests in 1.059s

OK
```

Las pruebas abarcaron autenticaci?n, autorizaci?n, cifrado, b?veda, custodia e importaciones cifradas.

### 4.2. Procesamiento distribuido

Se proces? el caso cifrado ficticio `piloto_auto_m4` mediante RabbitMQ y los workers.

Resultados observados:

- Tres mensajes procesados.
- Dos subtareas completadas.
- Cero reintentos.
- Resultado id?ntico al procesamiento local.

Esta prueba fue realizada antes de deshabilitar definitivamente el puerto AMQP sin TLS.

### 4.3. Transferencia HTTPS multibloque

Se utiliz? el descargador real del worker y un servidor HTTPS temporal con un adjunto ficticio almacenado mediante la b?veda cifrada.

Resultados:

| M?trica | Valor |
|---|---|
| Bloques descargados | 5 |
| Tama?o por bloque | 1 MiB |
| Descargas paralelas | 4 |
| Reintentos | 0 |
| Duraci?n | 2,197 segundos |
| Velocidad reportada | 2,27 MB/s |
| Verificaci?n SHA-256 | Correcta |
| Verificaci?n TLS | Correcta |

Resultado de la ejecuci?n:

```text
RESULTADO: WORKER REAL + HTTPS + BOVEDA APROBADOS
```

Archivo de prueba:

- `probar_m4_worker_real.py`

## 5. Evidencias recomendadas

Adjuntar capturas del proyecto que demuestren:

1. Inicio de sesi?n de Marea.
2. Restricciones de acceso seg?n el rol.
3. Importaci?n de conversaciones y casos cifrados.
4. Workers conectados y procesamiento distribuido completado.
5. Resultado de las 17 pruebas automatizadas.
6. Verificaci?n de puertos: `5671` activo y `5672` deshabilitado.
7. Resultado de transferencia HTTPS multibloque.

No incluir contrase?as, tokens, claves privadas ni informaci?n personal de conversaciones reales.

## 6. Limitaciones y verificaciones pendientes

- La integraci?n autom?tica de custodia fue probada mediante el flujo de importaci?n de Marea. El uso directo de `boveda.importar_cifrado()` requiere revisi?n independiente de su integraci?n con custodia.
- Debe verificarse la recuperaci?n ante un fallo de registro de custodia posterior a una importaci?n cifrada, para evitar casos confirmados sin registro persistente.
- El caso `piloto_auto_m4` fue importado antes de la correcci?n de custodia; no debe considerarse retrospectivamente registrado.
- RabbitMQ cifra las comunicaciones mediante TLS, pero su configuraci?n actual `ssl_options.verify = verify_none` no requiere certificados de cliente. La autenticaci?n AMQP sigue dependiendo de credenciales.
- Falta documentar una ejecuci?n completa de procesamiento distribuido del caso cifrado posterior al cierre de `5672`.

## 7. Conclusi?n

El M?dulo 4 fortaleci? la seguridad del Analizador de Chats de WhatsApp mediante autenticaci?n, permisos por roles, cifrado AES-256-GCM, registros de integridad, HTTPS y AMQPS.

Las 17 pruebas automatizadas se ejecutaron satisfactoriamente, y se verific? una transferencia HTTPS multibloque con integridad SHA-256 correcta.

La soluci?n conserva la arquitectura distribuida existente y proporciona una base para ampliar las pruebas de seguridad, recuperaci?n y auditor?a.
