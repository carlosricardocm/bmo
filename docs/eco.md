# Cancelación de eco (oír «Hey Vimo» con música)

## El problema (medido el 24-10-2026)

La música sale del altavoz y vuelve a entrar por el micrófono. Con el reproductor y el altavoz
Bluetooth al 100 %, el micrófono la capta **casi saturado** (nivel 0,49–0,67) mientras la voz llega
a ~0,075: la voz queda 16–19 dB por debajo. Mezclando las 59 grabaciones reservadas del usuario con
la música grabada por el micrófono, la wake word detectaba: 95 % sin música · **7 %** a volumen 30 ·
0 % a 50 y 100. Bajar el umbral no lo arreglaba.

## La solución

Vimo arranca al abrirse su **propio** proceso `pipewire -c data/aec/vimo_aec.conf` con el módulo
`libpipewire-module-echo-cancel` (motor WebRTC) y lo cierra al salir. **No cambia la configuración
de audio del sistema** ni la salida predeterminada.

- `vimo_aec_sink`: altavoz virtual. La voz de Vimo, los efectos y la música (mpv) salen por aquí y
  de ahí al altavoz real que era el predeterminado al arrancar Vimo (ahora el BT-017).
- `vimo_aec_source`: el micrófono real (el que contiene `audio.input_device`, «iTalk») con la
  señal del altavoz restada. De aquí leen la wake word y Whisper.
- `~/.asoundrc` define los PCM ALSA `vimo_aec_out` / `vimo_aec_in` para que sounddevice pueda
  abrirlos (Vimo los añade si faltan, entre las marcas «Vimo»).
- Se desactivan la supresión de ruido y el control de ganancia de WebRTC para no alterar la voz que
  analiza la wake word.

Resultado: con música a volumen 100 la música en el micrófono baja de 0,33 a **0,0105** (≈ 30 dB,
el nivel del ruido de la habitación). En Vimo real, el nivel que llega a la wake word con música
(0,0125) es igual que sin música (0,0131). Estimación con las grabaciones del usuario mezcladas con
el residuo: **64 %** de detección a la primera con la música a tope (antes 0 %); y en ese mismo
64–68 % de los casos la puntuación pasa de 0,15 y Vimo **baja la música**, así que si no reconoce a
la primera, al repetir «Hey Vimo» la música ya está baja.

## Calibración con voz y música a la vez (24-10-2026, «El Triste» de José José en vivo)

La prueba anterior mezclaba la voz por software. Esta reproduce 20 grabaciones de «Hey Vimo» por el
altavoz **sin pasar por el cancelador** (para él son voz de la habitación) mientras la canción suena
por `vimo_aec_sink`, y pasa la wake word real por lo que sale de `vimo_aec_source`.
(`data/eval_wake/aec_josejose/resultados.jsonl`)

Nota: la voz que sale de un altavoz y vuelve al micrófono se reconoce peor que la voz en directo;
sin música esta prueba solo llega a 4–5 de 20. Sirve para comparar ajustes, no como cifra absoluta.

| Volumen de la música | Detecta (de 20) | Baja la música (de 20) |
|---|---|---|
| sin música (techo de la prueba) | 4–5 | 8 |
| 100 | 1 | 3 |
| 80 | 3 | 7 |
| 70 | 7 (9 con supresión de transitorios) | 10 |
| 60 | 7 | 14 |
| 50 | 6 (8 con supresión de transitorios) | 8–12 |

El cancelador quita la canción casi entera (≈ 50 dB), pero a volumen 100 la música llega al
micrófono más fuerte que la voz y lo satura: WebRTC suprime **también la voz** (le quita ~18 dB) y
amplificar después no la recupera. Hasta 70, la voz pasa igual que sin música.

Cambios aplicados: `music.max_volume = 70` (tope; «sube el volumen» no pasa de ahí; **desde el
25-10-2026 es 90 por decisión del usuario**: entre 70 y 90 «Hey Vimo» se oye peor con música) y
`webrtc.transient_suppression = true` (mejor en las tres comparaciones). Sin efecto o peor:
supresión de ruido, control de ganancia, `buffer.play_delay` 100/200 ms, `buffer.max_size` 64, y
bajar la ganancia del micrófono. Si quieres más volumen, súbelo en el propio altavoz sabiendo que
«Hey Vimo» se oirá peor con la música.

## Configuración (`config.json` → `audio.echo_cancel`)

| Clave | Defecto | |
|---|---|---|
| `enabled` | `true` | `false` vuelve al micrófono y altavoz normales |
| `capture_target` | `null` | nodo de micrófono de PipeWire; `null` = el que contiene `audio.input_device` |
| `playback_target` | `null` | altavoz real; `null` = el predeterminado al arrancar Vimo |
| `aec_args` | NS y AGC desactivados, filtro de graves activado | opciones de `libspa-aec-webrtc` |

Si la cancelación no puede arrancar, Vimo lo indica en «Estado de los componentes» y sigue con los
dispositivos normales. Registro del proceso: `logs/aec.log`.

## Consejos

- Enciende el altavoz Bluetooth **antes** de abrir Vimo (la salida se elige al arrancar).
- Un volumen de música algo menor (60–80) deja aún menos residuo y la voz destaca más.
- Para quitarlo del todo: `audio.echo_cancel.enabled = false` y borra el bloque «Vimo» de `~/.asoundrc`.
