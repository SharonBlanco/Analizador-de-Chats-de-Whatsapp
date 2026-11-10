# Guía para el equipo: instalar y correr el proyecto

Pasos para dejar el proyecto funcionando en otra computadora. Los comandos se escriben en la **terminal**. Cuando Linux y Windows difieren, aparecen las dos versiones.

> **Antes de empezar:** pídele a Sharon que confirme que ya subió a GitHub la última versión (carpetas `generador_dataset/`, `distribuido/` y `docs/`, y el archivo `requirements.txt`).

---

## 1. Programas necesarios (una sola vez)

| Programa | Para qué | Cómo comprobar que está |
|---|---|---|
| **Python 3.11 o superior** | Todo el proyecto | `python3 --version` (en Windows, `python --version`) |
| **Git** | Bajar el repositorio | `git --version` |
| **FFmpeg** | Notas de voz del dataset y metadatos en los workers | `ffmpeg -version` |
| Docker (opcional) | Solo si RabbitMQ va a correr en **tu** computadora | `docker --version` |

Para instalar lo que falte:
- **Ubuntu/Debian:** `sudo apt install python3 python3-venv git ffmpeg`
- **Windows:** `winget install Python.Python.3.12 Git.Git Gyan.FFmpeg` (después, cierra y abre la terminal)

## 2. Bajar el proyecto

```bash
git clone https://github.com/SharonBlanco/Analizador-de-Chats-de-Whatsapp.git
cd Analizador-de-Chats-de-Whatsapp
```

## 3. Crear el entorno de Python (una sola vez)

El entorno virtual (`.venv`) guarda las librerías del proyecto. **Créalo siempre en tu propia carpeta; no lo copies de otro proyecto.** Un `.venv` copiado sigue apuntando a la carpeta vieja e instala todo allá.

**Linux:**
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**Windows (PowerShell):**
```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```
> Si Windows dice que no se pueden ejecutar scripts, corre una vez `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned` y vuelve a activar el entorno.

**Cada vez que abras una terminal nueva**, activa el entorno: `source .venv/bin/activate` (Linux) o `.venv\Scripts\Activate.ps1` (Windows). Sabes que está activo cuando la línea empieza con `(.venv)`.

Con el entorno activo, el comando es `python` tanto en Linux como en Windows.

## 4. Armar el dataset

El dataset **no está en GitHub**: pesa varios GB y una parte no se puede redistribuir. Cada quien lo genera en su computadora y, con la misma semilla, sale idéntico. Los detalles están en `docs/dataset.md`.

### 4.1 Imágenes, audios y videos (automático)
```bash
python generador_dataset/descargar_fuentes.py
```
Baja unos 3 GB: 300 imágenes, 908 audios y 4 videos. Si se corta, vuelve a correr el mismo comando y continúa donde quedó. Si por ahora no necesitas los videos, agrega `--sin-videos`.

### 4.2 Comentarios ofensivos (OffendES, requiere cuenta)
1. Crea una cuenta en https://huggingface.co.
2. Entra a https://huggingface.co/datasets/SINAI/OffendES y acepta las condiciones de acceso (el recuadro de arriba).
3. Crea un token en https://huggingface.co/settings/tokens: **Create new token** → tipo **Read** → copiarlo (empieza con `hf_`).
4. Corre el script y pega el token cuando lo pida. **No se ve al pegarlo; es normal.** En la terminal de Linux se pega con `Ctrl+Shift+V`.
   ```bash
   python generador_dataset/descargar_offendes.py
   ```
5. Al terminar, borra el token en esa misma página.

> **Nunca** pegues el token en un comando, en un archivo del proyecto ni en un chat.

### 4.3 Grabaciones (opcional, pero recomendado)
Son notas de voz "malas", para probar que el worker de audio detecta amenazas. Si Sharon ya las tiene, pídeselas en lugar de grabarlas de nuevo.
1. Graba con el celular las 15 frases de `generador_dataset/guion_grabaciones.csv`.
2. Nombra cada audio como su fila: `grabacion_01`, `grabacion_02`… La extensión da igual (`.m4a`, `.opus`, `.ogg`).
3. Ponlas en `fuentes_dataset/grabaciones/`, junto con una copia del guion llamada `grabaciones.csv`.

### 4.4 Generar y cargar
```bash
python generador_dataset/generar.py --sobrescribir --importar
```
Al final deben aparecer **todas las líneas con ✓**. Los 40 chats quedan cargados en la app, en la carpeta **"Dataset sintético"**.

## 5. Abrir la aplicación

```bash
python mensajeria/web.py
```
Abre **http://localhost:8100** en el navegador. Se detiene con `Ctrl+C`.

## 6. Procesamiento en paralelo (workers)

Los workers se coordinan por **RabbitMQ**. Hay dos formas de trabajar:

### Opción A: todo en tu computadora (para probar o desarrollar)
1. Crea tu configuración a partir del ejemplo:
   - Linux: `cp distribuido/config.env.ejemplo distribuido/config.env`
   - Windows: `copy distribuido\config.env.ejemplo distribuido\config.env`
2. Abre `distribuido/config.env` y completa:
   - `RABBIT_USER` y `RABBIT_PASS`: inventa un usuario y una contraseña.
   - `WORKER_TOKEN`: genéralo con `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
3. Levanta RabbitMQ (necesita Docker):
   ```bash
   cd distribuido
   docker compose --env-file config.env up -d
   cd ..
   ```
4. Abre la app (paso 5). En otra terminal, con el entorno activo, enciende un worker:
   ```bash
   python distribuido/worker/worker.py
   ```
5. En la app: abre un chat → **Procesamiento paralelo** → **Dividir entre workers**.

### Opción B: tu computadora como worker de la de Sharon (para la demo con 3 nodos)
Sharon corre la app y RabbitMQ; tú solo aportas un worker.
1. Pídele a Sharon **por un canal privado** (nunca por GitHub) los valores de `RABBIT_USER`, `RABBIT_PASS` y `WORKER_TOKEN`, y la **IP** de su computadora.
2. Crea `distribuido/config.env` (como en la opción A, paso 1) con esos valores, y cambia:
   ```
   RABBIT_HOST=<IP de Sharon>
   COORDINADOR_URL=http://<IP de Sharon>:8101
   ```
3. Enciende tu worker con un nombre propio:
   - Linux: `WORKER_ID=worker-tunombre python distribuido/worker/worker.py`
   - Windows: `$env:WORKER_ID="worker-tunombre"; python distribuido/worker/worker.py`
4. En la app de Sharon, la sección **Workers** debe mostrarte como **Activo**.

Para que esto funcione, las dos computadoras deben estar en la **misma red**, y la de Sharon debe aceptar conexiones en los puertos **5672** (RabbitMQ) y **8101** (adjuntos). Si están en redes distintas (por ejemplo, la del TEC bloquea esas conexiones), se puede usar **Tailscale**, igual que en el I Proyecto.

## 7. Reglas del repositorio

- **Nunca subir** datos ni credenciales: `fuentes_dataset/`, `dataset_generado/`, `dataset_whatsapp/` y `distribuido/config.env` ya están en el `.gitignore`. Antes de cada commit, revisa `git status`.
- **Nunca usar chats reales** de otras personas, ni siquiera en pruebas locales que vayan al informe (enunciado B.3).
- **Commits frecuentes y propios**, en los componentes de los que eres responsable. El enunciado revisa que el historial coincida con la matriz de responsabilidades y penaliza los commits masivos al final.
- Si usas IA, regístralo en `docs/bitacora_ia.md`.

## 8. Problemas frecuentes

| Síntoma | Causa y solución |
|---|---|
| `ModuleNotFoundError` (por ejemplo `datasets` o `pika`) | El entorno no está activo, o se copió de otro proyecto. Actívalo; si sigue igual, bórralo (`rm -rf .venv`) y repite el paso 3. |
| `pip install` termina bien pero el módulo no aparece | El `.venv` fue copiado y apunta a otra carpeta. Bórralo y créalo de nuevo (paso 3). |
| El token "no se pega" | Sí se pega, solo que no se ve. Usa `Ctrl+Shift+V` y luego Enter. |
| `Falta ffmpeg` | Instálalo (paso 1) y abre una terminal nueva. |
| La app dice "Procesamiento en paralelo sin configurar" | Falta `distribuido/config.env` (paso 6). |
| La app dice "Sin conexión con RabbitMQ" | RabbitMQ no está corriendo (`docker ps`), o `RABBIT_HOST` o las credenciales están mal. |
| El worker no aparece en la app de Sharon | Las computadoras no están en la misma red, o el firewall bloquea el puerto 5672. Prueben primero con la opción A. |
| `Ya existe dataset_generado` | Agrega `--sobrescribir` al comando de `generar.py`. |
