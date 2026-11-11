# Medición: tamaño del modelo YOLO para detectar armas

Sirve como evidencia para **M1** (memoria por modelo) y **M5** (rendimiento según la configuración), y justifica el modelo por defecto del worker de imágenes.

- **Fecha:** 10/11/2026
- **Equipo:** laptop ASUS TUF F15, Intel i7-12700H, NVIDIA RTX 3050 Laptop (4 GB), CUDA 13.2, PyTorch 2.15, Ultralytics 8.4
- **Datos:** las 300 imágenes del dataset (`dataset_generado/verdad/imagenes.csv`), 60 con armas según Open Images V7 (split *validation*, que no se usó para entrenar estos modelos)
- **Positivo:** al menos una caja de alguna de estas clases: Handgun, Rifle, Shotgun, Weapon, Knife, Kitchen knife, Dagger, Sword
- **Método:** `YOLO(modelo).predict(imagen, conf=0.01, device=0)` sobre cada imagen; luego se varía el umbral sobre la confianza máxima de arma. El tiempo se mide de forma secuencial, una imagen a la vez, después de una inferencia de calentamiento. La memoria es el pico de `torch.cuda.max_memory_allocated()`.

| Modelo | Umbral | Precisión | Exhaustividad | F1 | VP | FP | FN | ms/imagen | VRAM pico |
|---|---|---|---|---|---|---|---|---|---|
| yolov8n-oiv7 | 0.25 | 1.00 | 0.22 | 0.36 | 13 | 0 | 47 | 12.1 | 88 MB |
| yolov8n-oiv7 | 0.15 | 1.00 | 0.27 | 0.42 | 16 | 0 | 44 | 10.8 | 88 MB |
| yolov8s-oiv7 | 0.25 | 1.00 | 0.30 | 0.46 | 18 | 0 | 42 | 15.8 | 136 MB |
| yolov8s-oiv7 | 0.15 | 1.00 | 0.37 | 0.54 | 22 | 0 | 38 | 15.2 | 136 MB |
| yolov8m-oiv7 | 0.25 | 1.00 | 0.33 | 0.50 | 20 | 0 | 40 | 24.9 | 187 MB |
| yolov8m-oiv7 | 0.10 | 0.96 | 0.43 | 0.60 | 26 | 1 | 34 | 24.9 | 187 MB |
| yolov8m-oiv7 | 0.05 | 0.97 | 0.48 | 0.64 | 29 | 1 | 31 | 24.9 | 187 MB |
| **yolov8l-oiv7** | **0.10** | **1.00** | **0.53** | **0.70** | 32 | 0 | 28 | 39.6 | 365 MB |
| yolov8l-oiv7 | 0.05 | 1.00 | 0.57 | 0.72 | 34 | 0 | 26 | 39.6 | 365 MB |
| yolov8x-oiv7 | 0.10 | 1.00 | 0.50 | 0.67 | 30 | 0 | 30 | 58.2 | 479 MB |

## Interpretación
- **Ninguno da falsas alarmas** con umbrales de 0.10 o más. El problema es lo que se escapa: las armas de Open Images suelen ser pequeñas, parciales o estar en escenas complejas.
- **Más grande no siempre es mejor:** `x` tarda 1,5 veces lo que `l` y encuentra menos. Entre `n` y `l`, el tiempo se multiplica por 3,3 y la memoria por 4,1, mientras la exhaustividad pasa de 0,22 a 0,53.
- **Elección:** `yolov8l-oiv7` con umbral 0.10. Es el mejor F1 sin falsas alarmas y entra con holgura en una GPU de 4 GB. El umbral 0.05 sube un poco el F1 en este conjunto, pero se deja en 0.10 para no generar alertas de severidad alta con evidencia débil en datos nuevos.
- Se configura con `WA_YOLO_MODELO` y `WA_UMBRAL_IMAGEN`.
