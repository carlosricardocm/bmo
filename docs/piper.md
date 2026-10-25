# Síntesis de voz con Piper

Se usa **piper-tts 1.8.0** del proyecto mantenido [OHF-Voice/piper1-gpl](https://github.com/OHF-Voice/piper1-gpl)
(el repositorio original rhasspy/piper está archivado). Se instala con pip en el venv: hay wheel
para aarch64 que incluye espeak-ng, sin binarios externos. Licencia GPL-3.0.

> Las instalaciones previas en `~/piper` (código fuente de rhasspy/piper) y `~/bmo/piper` (binario
> de 2023) no se usan ni se modifican.

## Voz

| Clave (`tts`) | Defecto | |
|---|---|---|
| `voice_model` | `voices/voz_bmo_final_v2.onnx` | modelo ONNX de la voz |
| `voice_config` | `null` → `<voice_model>.json` | configuración (frecuencia, fonemas) |
| `length_scale` | 1,0 | velocidad: >1 más lento, <1 más rápido (Ajustes) |
| `noise_scale`, `noise_w_scale` | `null` | variación; `null` = valores del modelo |
| `speaker_id` | `null` | para voces con varios hablantes |
| `max_spoken_chars` | 900 | lo que exceda se resume con «el resto está en la pantalla» |

Voz actual: `voz_bmo_final_v2` (voz personalizada del usuario, **es-419**, 22 050 Hz, mono,
calidad alta, 61 MB). Otras voces en español: <https://huggingface.co/rhasspy/piper-voices/tree/main/es>
(copia `.onnx` y `.onnx.json` a `voices/` y cambia `tts.voice_model`).

## Flujo

1. `prepare_for_speech()` adapta la respuesta original: quita Markdown, viñetas, tablas, emojis y
   etiquetas; no lee bloques de código («te muestro el código en la pantalla»); cambia URL por su
   dominio y rutas largas por el nombre del archivo. **La respuesta original se conserva** en el
   panel **Texto**, con las fuentes.
2. `split_sentences()` divide en frases, en orden.
3. Un hilo sintetiza por adelantado como mucho 2 frases mientras se reproduce la anterior (cola
   FIFO con un solo productor, así que el orden se mantiene). Eso reduce la espera hasta la primera
   palabra.
4. `AudioPlayer` remuestrea de 22 050 Hz a la frecuencia de salida (48 kHz en PipeWire), duplica a
   estéreo si hace falta y escribe en bloques de 50 ms. **Parar** detiene en ≤50 ms y cierra el
   sintetizador.
5. Mientras suena, la wake word está suspendida, y se reactiva `wake_word.resume_delay_seconds`
   después.

La voz se carga **una vez** al arrancar, no en cada respuesta. Si falta el modelo, Vimo lo indica
(«Funcionando con limitaciones: voz») y muestra las respuestas en pantalla.

## Rendimiento

**Pendiente de medir en el dispositivo** (carga del modelo, tiempo hasta la primera frase y factor
de tiempo real). La carga de la voz coincidió con uno de los apagados por alimentación (ver
[diagnostico.md](diagnostico.md)). Las cifras aparecerán en `logs/vimo.log`
(`Voz Piper … cargada en X s`, `tts_first_segment` en «Tiempos del turno»).
