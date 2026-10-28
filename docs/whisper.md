# Reconocimiento de voz con Whisper.cpp

[whisper.cpp](https://github.com/ggml-org/whisper.cpp) transcribe localmente lo que el usuario
dice **después** de la wake word. OpenWakeWord solo activa; Whisper solo transcribe.

## Instalación

Instalación existente: `~/whisper.cpp` (commit 79218f51, 16-03-2026), compilada en
`build/bin/` con `whisper-cli` y `whisper-server`. Para instalar desde cero:

```bash
./setup.sh --build-whisper
# equivale a:
git clone --depth 1 https://github.com/ggml-org/whisper.cpp ~/whisper.cpp
cmake -S ~/whisper.cpp -B ~/whisper.cpp/build -DCMAKE_BUILD_TYPE=Release
cmake --build ~/whisper.cpp/build -j4 --target whisper-cli whisper-server
```

## Modelos

Multilingües (sin `.en`) y cuantizados, para el español:

| Modelo | Tamaño | Notas |
|---|---|---|
| `models/ggml-base-q5_1.bin` | 57 MB | rápido; peor con nombres propios |
| `models/ggml-small-q5_1.bin` | 182 MB | **por defecto**; mejor calidad en español |
| `models/ggml-medium-q5_0.bin` | 514 MB | demasiado lento para conversación en la Pi 5 |

Descarga: `https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-<modelo>.bin` en
`~/whisper.cpp/models/` (o `./setup.sh --download-models`, que muestra tamaño y destino y pide
confirmación). Cambiar de modelo: **Ajustes → Modelo Whisper** y reiniciar Vimo.

## Cómo lo usa Vimo

- **whisper-server** (`stt.use_server: true`): se arranca una vez en `127.0.0.1:8178` con el modelo
  cargado en memoria, y cada consulta se envía por HTTP local (`/inference`, multipart, idioma por
  petición). Así el modelo no se recarga en cada consulta. Su salida va a `logs/whisper-server.log`.
- Si el servidor no arranca o falla, cada consulta usa **whisper-cli** (carga el modelo cada vez).
- Al cerrar Vimo se detiene el servidor.

### Fin de la intervención (VAD)

`app/speech_to_text.py` (`EndpointDetector`) usa un detector de energía con umbral adaptativo:
`máx(listening.energy_threshold, ruido_ambiente × listening.noise_multiplier)`. El ruido ambiente
se estima continuamente mientras Vimo espera.

| Clave (`listening`) | Defecto | |
|---|---|---|
| `silence_seconds` | 1,0 | silencio que da por terminada la intervención |
| `max_seconds` | 12 | duración máxima |
| `no_speech_timeout_seconds` | 5 | si no se empieza a hablar, se cancela |
| `min_speech_seconds` | 0,35 | menos voz que esto no se envía a Whisper |
| `energy_threshold` / `noise_multiplier` | 0,012 / 2,5 | sensibilidad |

Se conservan 0,3 s de audio previos al inicio de la voz. Los clics aislados no cuentan como voz
(hacen falta 160 ms seguidos). Se filtran las transcripciones vacías y las «alucinaciones» típicas
de Whisper con silencio («Subtítulos realizados por la comunidad de Amara.org», «[Música]»…).

### Idioma

`stt.language`: `es` por defecto; también `en`, `fr`, `pt`… o `auto` (detección; algo más lento).

## Rendimiento

Medido el 25-10-2026 con `ggml-small-q5_1` y `whisper-server` (11 frases con la voz Piper de Vimo,
limpias y con música grabada por el micrófono; 2–6 s de audio):

| Configuración | Tiempo por frase | Texto |
|---|---|---|
| 4 hilos, contexto completo (la anterior) | ~13 s (logs) | — |
| 2 hilos, contexto completo | 22,5 s | igual |
| **2 hilos, `-ac 768` (la actual)** | **10,5 s** | **idéntico** en todas las frases |

`-ac` (`stt.audio_ctx`) limita la ventana que procesa el codificador de Whisper: 1500 = 30 s,
768 ≈ 15 s. Las consultas duran como máximo `listening.max_seconds` (12 s), así que no se pierde
nada y el codificador tarda menos de la mitad. Vimo avisa y usa el contexto completo si
`listening.max_seconds` supera lo que cubre `audio_ctx`.

`stt.threads` es 2 porque con 4 la Raspberry Pi se apagó (pico de consumo, ver
[diagnostico.md](diagnostico.md#la-raspberry-pi-se-apaga-sola-alimentación)). Con una alimentación
estable, 4 hilos y `-ac 768` deberían bajar a ~5–6 s por frase.

El banco de pruebas está en `~/bmoclaude_backups/stt_bench/stt_bench.py` (con un vigilante que
detiene Whisper si sube la temperatura o la corriente, o si baja la tensión).

## Vocabulario (`stt.prompt`) y evaluación con tu voz (28-10-2026)

`tools/grabar_frases_stt.py` graba 33 frases reales (luces, música, volumen, cámara y preguntas)
por el mismo camino de audio que Vimo; `tools/evaluar_whisper.py` las transcribe con varias
configuraciones y mide si Vimo haría lo correcto con cada transcripción. Resultados (Pi 5 a 2,0 GHz;
tiempos estimados con 4 hilos):

| Configuración | Órdenes bien | WER | Tiempo |
|---|---|---|---|
| small (antes) | 19/33 | 36 % | ~4,9 s |
| **small + vocabulario (actual)** | 22/33 → 31/33 con las variantes añadidas | **24 %** | ~5,8 s |
| small + búsqueda en haz (`beam_size 5`) | 19/33 | 35 % | ~5,2 s |
| medium + vocabulario | 29/33 | 12 % | ~16,6 s (demasiado lento) |

El fallo típico era la primera palabra («pon» → «hola», «con», «pondra»). El vocabulario se envía
en cada petición a whisper-server (`stt.prompt`; vacío lo desactiva). Las transcripciones erróneas
que siguen apareciendo se aceptan como variantes en `app/intents.py` y `app/smart_lights.py`.
Para repetir la evaluación: cierra Vimo y ejecuta `venv/bin/python tools/evaluar_whisper.py --hilos 2`.
