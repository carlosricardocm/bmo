# Diagnóstico y solución de problemas

Primero, siempre:

```bash
venv/bin/python main.py --check     # componentes, sin audio ni cámara
tail -n 100 logs/vimo.log           # registro de Vimo (rotativo, 2 MB × 3)
venv/bin/python -I tools/verify_venv.py   # integridad del venv tras un corte de energía
```

En la GUI, **Texto** muestra al arrancar el estado de cada componente (✓/✗) con el motivo.

## La Raspberry Pi se apaga sola (alimentación)

Problema observado durante el desarrollo: cuatro apagados bruscos, sin registro de apagado del
sistema, coincidiendo con **picos de consumo**: carga de la voz de Piper, `pip install`, unos
segundos después de reproducir audio y cálculo continuo de la wake word. La temperatura era
normal (52–63 °C, ventilador activo).

Datos de `tools/power_logger.py` (21-10-2026):

| Hora | Entrada 5 V Pi | Corriente núcleo | Batería X120x |
|---|---|---|---|
| reposo | 5,08–5,11 V | 0,95 A | 4,21 V (98 %, *Full*) |
| 20:33:20 (inicio de carga de CPU) | 4,97 V | 2,93 A | **3,19 V** durante ~3 s |
| 20:37:39 (inicio de carga de CPU) | 5,08 V | 2,32 A | **3,19 V** durante ~3 s |
| mínimo en 90 s de carga | 4,887 V | pico 4,35 A | — |

Interpretación: en cada pico la fuente no da suficiente corriente, la SAI X120x tira de la batería,
y una celda llena cae ~1 V, casi hasta el corte de su convertidor (~3,0–3,2 V). Eso indica celdas
con resistencia interna alta (desgaste) o mala conexión. Los apagados ocurrieron con la batería
**cargando al 86 %**: menos margen y, además, el cargador consumiendo corriente de la fuente. Con
la batería llena el mismo pico no la apagó. Es la explicación más coherente con los datos, pero no
está demostrada: falta registrar un apagado con el registrador funcionando.

**Apagado registrado (25-10-2026, 12:20).** Con el registrador en marcha (1 lectura/s), al empezar
una prueba de Whisper con 4 hilos: en 4 s la corriente del núcleo pasó de 0,9 A a **7,1 A**, la
temperatura de 42 °C a 80 °C y la entrada de 5 V de 5,10 V a 4,91 V; la Pi se apagó en el segundo
siguiente, sin aviso de *throttling*. La batería no se pudo leer (`bat=?`), así que no se sabe si
cayó la celda. Con 2 hilos el pico es de ~4,9 A y no hubo problemas en varias pruebas seguidas.
Por eso Vimo usa ahora `stt.threads = 2` (con `stt.audio_ctx = 768` es más rápido que antes con 4).
`config.txt` tiene `usb_max_current_enable=1` (forzar corriente USB sin fuente de 5 A): con una
fuente más débil eso deja menos margen.

**Fuente de 5 V / 5 A a través de la X120x (27-10-2026).** Con la fuente nueva conectada al USB-C
de la X120x, la misma prueba (Whisper, 4 hilos, `-ac 768`) bajó la entrada de 5 V a **4,77 V** con
solo 5,35 A en el núcleo (el vigilante la cortó; en reposo 5,07 V). La caída es peor que con la
fuente anterior: la SAI averiada y sus contactos limitan, no la fuente. Hay que conectar la fuente
directamente al USB-C de la Raspberry Pi antes de volver a 4 hilos y 2,4 GHz.

**Fuente de 5 V / 5 A directa a la Pi + celda en la X120x (27-10-2026).** Con la fuente en el
USB-C de la Raspberry Pi: reposo 5,13–5,15 V; Whisper con 4 hilos → pico de 6,84 A en el núcleo,
5 V mínimo **5,09 V**, 69,7 °C, sin *throttling*. Se volvió a `stt.threads = 4`, yt-dlp en los 4
núcleos y se quitó `arm_freq=1800` (2,4 GHz). Ojo: así la X120x **no carga la celda** (solo carga
desde su propio USB-C); la batería quedó al 31 % («Discharging»).
Tras reiniciar a **2,4 GHz**: Whisper (4 hilos) 4,5 s por consulta; 22 transcripciones seguidas
(~100 s de carga continua) → pico de 9,0 A en el núcleo, 5 V mínimo 5,08 V, **78,5 °C**, sin
*throttling*. En uso normal la carga es a ráfagas y se queda bastante por debajo.

**X120x reasentada, con celda y la fuente en su USB-C (27-10-2026, 21:45).** Tras volver a montar
bien la X120x sobre la Pi, su chip responde, detecta la red (`x120x-ac online=1`) y **carga la
celda**. Reposo 5,14–5,18 V. A 2,4 GHz: Whisper 4 hilos sostenido → 8,6 A, 5 V ≥ 5,01 V, 68,6 °C;
Whisper + Piper a la vez → 9,0 A, 5 V ≥ 5,006 V, 74,7 °C; sin *throttling*. Antes del reasiento la
misma configuración caía a 4,81 V incluso con 2 hilos: el fallo era de **contacto** entre la X120x
y la Pi (tornillos/pines), no de la placa. Si vuelve a caer la tensión, revisar ese contacto.

**CPU a 2,0 GHz por temperatura (27-10-2026).** Con la caja en su configuración final, a 2,4 GHz la
CPU quedaba a ~70 °C casi en reposo (ventilador a 7.800 rpm) y pasaba de 82–85 °C tras unos
minutos de Whisper. Se dejó `arm_freq=2000` en `/boot/firmware/config.txt`. La alimentación no era
el problema (5 V ≥ 5,02 V en todas las pruebas, incluso con un cargador de 5 V/2 A).

Qué hacer:

1. **Fuente de 5 V / 5 A (27 W)**, la oficial de la Pi 5 o la que recomiende Suptronics para la
   X120x, con un cable corto y grueso.
2. Revisar las **celdas 18650**: misma marca y carga, sin golpes, bien asentadas en el portapilas.
   Probar con celdas nuevas de calidad.
3. Alimentar el **altavoz USB desde un hub con alimentación propia**.
4. Dejar el registrador en marcha y, tras un apagado, mirar las últimas líneas:
   ```bash
   nohup python3 tools/power_logger.py --interval 0.3 >/dev/null 2>&1 &
   tail -n 40 logs/power.log
   ```
5. Tras un corte, comprobar el venv (`tools/verify_venv.py`). Los archivos escritos justo antes del
   corte pueden quedar con 0 bytes (ext4); reinstala el paquete afectado con
   `venv/bin/pip install --force-reinstall --no-cache-dir <paquete>`.

Vimo reduce el riesgo: escribe `config.json` y la memoria de forma atómica (`fsync` + `rename`) y
graba el vídeo en MPEG-TS recuperable.

## Perfil de bajo consumo (24-10-2026)

Con la SAI X120x averiada (su chip no responde por I²C y no hay baterías), la Raspberry Pi se apaga
en los picos de carga. Reinicio del 24-10 a las 20:52: Whisper + yt-dlp + Piper + mpv a la vez
llevaron el núcleo a **7,9 A** y la entrada de 5 V a 4,87 V; segundos después, corte (`get_rsts=0x1000`).

Para reducir esos picos sin cambiar el hardware:

| Ajuste | Dónde | Efecto medido |
|---|---|---|
| CPU máx. **1,8 GHz** | `arm_freq=1800` en `/boot/firmware/config.txt` (copia: `config.txt.antes-vimo`) | −30 % de corriente por núcleo (1,2 → 0,84 A); más bajo no ahorra: la tensión del núcleo es fija (0,85 V) |
| Whisper **base**, 2 hilos | `stt.model`, `stt.threads` | ~3 veces menos cálculo que small |
| Piper, 2 hilos | `tts.threads` | antes usaba los 4 núcleos |
| yt-dlp en el núcleo 3, prioridad baja | `music.ytdlp_cpu_cores`, `music.ytdlp_nice` | no compite con el resto |

Mismo escenario (música + Piper + yt-dlp + cancelación de eco + Whisper) con estos ajustes:
**4,25 A** de pico (antes 7,9), 5 V ≥ 5,03 V, 59 °C (antes 77). Coste: transcripciones algo más
lentas y menos precisas (base). Si la alimentación se arregla, se puede volver a `small`, 4 hilos y
2,4 GHz (borrando la línea `arm_freq`).

> Actualización: el modelo base reconocía peor, así que se volvió a Whisper **small** con 4 hilos y
> Piper con 4 hilos. Se mantienen el límite de 1,8 GHz y yt-dlp en el núcleo 3.

La solución de fondo sigue siendo de hardware: alimentar la Pi directamente por su USB-C con la
fuente oficial de 27 W, o reparar/sustituir la X120x y ponerle baterías 18650 de calidad.

## Audio

**Puerto USB defectuoso (22-10-2026):** el puerto de la Raspberry Pi donde estaba el micrófono
(`usb 1-2` en `dmesg`) empezó a dar `device descriptor read/64, error -71` y
`unable to enumerate USB device` con dos dispositivos distintos. El mismo micrófono funciona en otro
puerto. No uses ese puerto. Para comprobarlo: `dmesg -T | grep -E "usb 1-2|error -71"`.

**Altavoz Bluetooth:** debe estar conectado con el perfil **A2DP** (`wpctl inspect @DEFAULT_AUDIO_SINK@`
→ `api.bluez5.profile = "a2dp-sink"`). Con el perfil manos libres (HFP) el sonido es de mala calidad.
Si se perdiera el principio de los sonidos al «despertar» el altavoz, se puede evitar que PipeWire
lo suspenda (no ha hecho falta: se oyen completos).

| Síntoma | Causa / solución |
|---|---|
| «No hay dispositivos de audio de entrada» | Micrófono desconectado. `arecord -l` debe listar una tarjeta de captura. El altavoz C-Media es solo de salida. |
| «No se encontró el dispositivo 'iTalk'» | Cambia `audio.input_device` (subcadena del nombre o índice; lista en `--check` o en Ajustes). |
| `Invalid sample rate` | Deja `input_sample_rate`/`output_sample_rate` en `null`: Vimo elige una frecuencia admitida (el iTalk-02 solo admite 44,1/48 kHz). |
| «Cola de audio llena» en el registro | CPU saturada; se descartan bloques antiguos. Usa un modelo de Whisper más ligero. |
| No me oye / corta antes de tiempo | Ajusta `listening.energy_threshold` y `listening.silence_seconds` (Ajustes). Vimo estima el ruido ambiente continuamente. |
| Se despierta solo | Sube `wake_word.threshold` (Ajustes). Mientras habla, la wake word está suspendida. |
| No se despierta | Baja el umbral (0,3–0,4) o reentrena el modelo (docs/wake_word.md). Usa **Hablar** mientras tanto. |
| Voz entrecortada | Salida `default` (PipeWire) recomendada; comprueba la carga de CPU. |

## Whisper.cpp

| Síntoma | Solución |
|---|---|
| «No se encuentra el modelo de Whisper» | `stt.model` es relativo a `stt.whisper_dir`. `./setup.sh --download-models`. |
| «El puerto 8178 ya está en uso» | Otro whisper-server en marcha (`pkill -f whisper-server`) o cambia `stt.server_port`. |
| Transcripción lenta | Cambia a `models/ggml-base-q5_1.bin` en Ajustes y reinicia. |
| «No he entendido nada» | Transcripción vacía o «alucinación» filtrada («Subtítulos por Amara.org»…). Habla más cerca. |

## Claude Code

| Mensaje | Solución |
|---|---|
| «no tiene una sesión iniciada» | Ejecuta `claude` en un terminal e inicia sesión (Vimo no gestiona credenciales). |
| «límite de uso» | Espera; Vimo no cambia a otro modelo. |
| «No puedo conectar» | Revisa la red. La GUI, la wake word y la cámara siguen funcionando. |
| «tardó demasiado» | Sube `claude.timeout_seconds`. |
| Funciona en terminal pero no en autoarranque | El `PATH` del lanzador gráfico no incluye `claude`; pon la ruta completa en `claude.executable`. |

## Búsqueda

«DuckDuckGo pidió una verificación anti-bots»: bloqueo temporal de la IP por exceso de consultas.
Vimo espera automáticamente (estado en `data/search_state.json`; bórralo para forzar un intento).
Ver [duckduckgo.md](duckduckgo.md).

## Cámara

| Síntoma | Solución |
|---|---|
| «No se detecta ninguna cámara» | `rpicam-hello --list-cameras`. Revisa el cable y `dtoverlay=imx519` en `/boot/firmware/config.txt`. |
| «¿la usa otro programa?» | Cierra `rpicam-*` u otra instancia de Vimo: libcamera admite un único propietario. |
| «Espacio insuficiente» | Libera espacio o baja `video.min_free_mb`. |
| Archivo `*_recuperado.mp4` | Grabación interrumpida que Vimo recuperó al arrancar. |
| `*.ts.part` que persiste | No se pudo recuperar. Prueba `ffmpeg -i x.ts.part -c copy x.mp4`. |

## GUI

| Síntoma | Solución |
|---|---|
| `cannot import name 'ImageTk'` | `sudo apt install python3-pil.imagetk` |
| «No hay sesión gráfica» | Arranca desde el escritorio o con el autoarranque (`docs/instalacion.md`). |
| Salir de pantalla completa | `Esc` o `F11`. |
