| Modelo | Dispositivo | Latencia por elemento | Throughput | RAM del proceso | VRAM | Aceleración GPU/CPU |
|---|---|---|---|---|---|---|
| Texto (RoBERTuito) | CPU | 52.81 ms | 18.93 mensajes/s | 1565 MB | — | — |
| Texto (RoBERTuito) | CUDA | 10.22 ms | 97.87 mensajes/s | 2287 MB | 688 MB | 5.2× |
| Fotos (YOLOv8-l) | CPU | 449.98 ms | 2.22 fotos/s | 1446 MB | — | — |
| Fotos (YOLOv8-l) | CUDA | 39.96 ms | 25.03 fotos/s | 1924 MB | 368 MB | 11.3× |
| Audio (Whisper small, transcripción) | CPU | 1625.32 ms | 0.62 audios/s (6.9× tiempo real) | 1391 MB | — | — |
| Audio (Whisper small, transcripción) | CUDA | 241.2 ms | 4.15 audios/s (46.8× tiempo real) | 1515 MB | 370 MB | 6.7× |
