"""
Descarga OffendES (SINAI, Universidad de Jaén) desde Hugging Face a fuentes_dataset/texto/offendes/.

Requisitos (una vez):
  1. Cuenta en https://huggingface.co y acceso aceptado en https://huggingface.co/datasets/SINAI/OffendES
  2. Un token de lectura: https://huggingface.co/settings/tokens → Create new token → tipo "Read"

Uso:
  python generador_dataset/descargar_offendes.py
El token se pide sin mostrarlo en pantalla y no se guarda en ningún archivo.
"""

from getpass import getpass
from pathlib import Path

DESTINO = Path(__file__).resolve().parent.parent / "fuentes_dataset" / "texto" / "offendes"


def main():
    try:
        from datasets import load_dataset
    except ImportError:
        raise SystemExit("Falta la librería 'datasets'. Active el entorno (.venv) y corra: pip install -r requirements.txt")
    token = getpass("Token de Hugging Face (no se ve al pegarlo; Enter para continuar): ").strip()
    if not token.startswith("hf_"):
        raise SystemExit("Ese no parece un token de Hugging Face (empiezan con hf_)")
    try:
        ds = load_dataset("SINAI/OffendES", token=token)
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"No se pudo descargar: {e}\n¿Ya aceptó el acceso en https://huggingface.co/datasets/SINAI/OffendES ?")
    DESTINO.mkdir(parents=True, exist_ok=True)
    for parte, datos in ds.items():
        datos.to_csv(str(DESTINO / f"{parte}.csv"))
        print(f"  {parte}: {len(datos)} comentarios → {DESTINO / f'{parte}.csv'}")
    print("Listo. Ya puede borrar el token en https://huggingface.co/settings/tokens si no lo va a usar más.")


if __name__ == "__main__":
    main()
