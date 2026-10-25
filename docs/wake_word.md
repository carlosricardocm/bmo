# Wake word personalizada con livekit-wakeword

Vimo usa [livekit-wakeword](https://github.com/livekit/livekit-wakeword) 0.3.0 (ONNX, CPU), el mismo
sistema con el que se entrenó el modelo. No hay motor sustituto: si el modelo falta o no carga, Vimo
lo indica y sigue funcionando con el botón **Hablar**. Nunca descarga un modelo genérico.

Hasta el 25-10-2026 se usaba openWakeWord 0.6.0 con el mismo modelo (ver «Por qué livekit»).

## Archivos

| Archivo | Uso |
|---|---|
| `models/hey_vimo_mis_audios.onnx` | modelo **en uso**: livekit-wakeword reentrenado con frases trampa como negativas y con las grabaciones del usuario; **umbral 0,3** |
| `models/hey_bmo_livekit.onnx` | versión anterior de livekit-wakeword (24-10-2026) |
| `models/hey_vimo_colab.onnx`, `models/hey_vimo.onnx` | modelos antiguos de openWakeWord (misma arquitectura de entrada, se pueden cargar) |
| `models/openwakeword/melspectrogram.onnx` | espectrograma mel (auxiliar; idéntico al que trae livekit-wakeword) |
| `models/openwakeword/embedding_model.onnx` | embeddings de voz (auxiliar; idéntico al de livekit-wakeword) |
| `models/openwakeword/silero_vad.onnx` | filtro de voz Silero (1,8 MB) |

La carpeta conserva el nombre `openwakeword/` porque los archivos vienen de su release v0.5.1
(`./setup.sh --download-models`). Si faltan el mel o los embeddings, se usan los del paquete
livekit-wakeword.

## Configuración (`config.json` → `wake_word`)

```json
{
  "enabled": true,
  "engine": "livekit",
  "phrase": "Hey Vimo",
  "model_path": "models/hey_vimo_mis_audios.onnx",
  "threshold": 0.3,
  "chunk_size": 1280,
  "cooldown_seconds": 2.0,
  "resume_delay_seconds": 0.6,
  "patience_frames": 1,
  "vad_threshold": 0.5,
  "duck_music_score": 0.15,
  "exact_gate": 0.1,
  "threads": 1
}
```

- `chunk_size` 1280 muestras = 80 ms a 16 kHz.
- `threshold` se ajusta en caliente desde **Ajustes** (resolución 0,01).
- `exact_gate`: puntuación aproximada a partir de la que se calcula la puntuación exacta (ver abajo).
  Se limita automáticamente a la mitad del umbral y a `duck_music_score`.
- `threads`: hilos de onnxruntime (1 = carga mínima; la Raspberry Pi se reinició una vez con mucha
  carga simultánea).

Antes de cargar el modelo se comprueba que su entrada es `[1, 16, 96]` (16 embeddings de 96
dimensiones). Un ONNX cualquiera se rechaza con un mensaje claro.

## Por qué livekit (25-10-2026)

openWakeWord y livekit-wakeword usan los mismos `melspectrogram.onnx` y `embedding_model.onnx`, pero
los alimentan distinto, y el modelo se entrenó con livekit:

| | livekit (entrenamiento) | openWakeWord |
|---|---|---|
| escala del audio que entra al mel | ±1 | ±32768 (int16 sin normalizar) |
| trozo sobre el que se calcula el mel | ventana de 2 s | ~0,1 s |

El grafo del mel contiene `ReduceMax` (normalización respecto al máximo de la entrada), así que con
openWakeWord las características no coincidían con las del entrenamiento. Mismo modelo, mismos datos
(`data/eval_wake/`), sin VAD:

| Umbral | 0,15 | 0,21 | 0,3 | 0,4 | 0,5 |
|---|---|---|---|---|---|
| voz del usuario (59 reservadas), openWakeWord | 97 % | 95 % | 88 % | 81 % | 75 % |
| voz del usuario, **livekit** | 100 % | 100 % | **98 %** | 95 % | 85 % |
| 30 frases trampa (voz Piper), openWakeWord | 11 | 10 | 8 | 7 | 6 |
| 30 frases trampa, **livekit** | 13 | 11 | **9** | 8 | 6 |
| música y noticias (23 min), openWakeWord | 0 | 0 | 0 | 0 | 0 |
| música y noticias, **livekit** | 6 | 1 | **0** | 0 | 0 |

Livekit con umbral 0,3 mejora en todo al sistema anterior (openWakeWord con 0,21).

## Cómo funciona en Vimo

1. `MicrophoneStream` captura el micrófono, lo convierte a mono 16 kHz y lo reparte en bloques de
   1280 muestras (80 ms).
2. `LiveKitModel.predict()` (`app/wake_word.py`) reproduce el pipeline de livekit-wakeword:
   - guarda los últimos 2 s de audio, normalizados a ±1;
   - en cada bloque calcula el mel de esos 2 s y **un** embedding nuevo, y puntúa con los 16 últimos
     (aproximación, ~11 ms por bloque);
   - si la aproximación llega a `exact_gate`, recalcula los 16 embeddings con el mel actual: es
     exactamente `livekit.wakeword.WakeWordModel.predict()` (~86 ms), y ocurre en <1 % de los bloques
     con ruido normal. Así el coste medio es ~11–14 ms por bloque en vez de 86 ms.
   - KleidiAI se desactiva en onnxruntime, como hace livekit.
3. `SileroVAD` (mismo modelo y criterio que el VAD de openWakeWord) descarta activaciones sin voz.
4. Al superar el umbral: enfriamiento, reinicio de los búferes y aviso al controlador, que solo
   inicia la escucha si Vimo está en `IDLE` (o `ERROR`).
5. Mientras Vimo reproduce audio, el detector está **suspendido** y se reactiva
   `resume_delay_seconds` después.

> Las secciones siguientes son el historial de ajustes. Las cifras anteriores al 25-10-2026 se
> midieron con openWakeWord; con livekit las puntuaciones son más altas.


## Evaluación sin micrófono

```bash
venv/bin/python tools/test_wake_word.py --model models/hey_vimo_mis_audios.onnx \
    --positive ~/bmo/muestras/positivas --negative sounds/ --limit 80
```

Resultados en esta Raspberry Pi (80 muestras positivas grabadas por el usuario y 26 audios
negativos, incluidos los sonidos del propio Vimo):

| Modelo | Detecciones (umbral 0,5) | Umbral 0,3 | Falsas activaciones |
|---|---|---|---|
| `hey_vimo_colab.onnx` | 38/80 | 47/80 | 0/26 |
| `hey_vimo.onnx` | 29/80 | 34/80 | 0/26 |
| `hey_vimo_old.onnx` (40 muestras) | 10/40 | — | 0/26 |
| `hey_bimo.onnx` (40 muestras) | 9/40 | — | 0/26 |

Conclusión: el modelo actual detecta aproximadamente la mitad de las muestras grabadas. Se puede
usar, pero conviene **reentrenarlo** con más datos. Estas muestras se usaron para entrenar, así que
con voces nuevas la tasa real puede ser más baja. 26 negativos son pocos para estimar falsas
activaciones: el umbral definitivo hay que ajustarlo en la habitación real.

## Ajustar con grabaciones reales (recomendado)

### 1. Grabar en tu habitación

Con Vimo **cerrado** (el micrófono es de uso exclusivo):

```bash
# Positivas: cuenta atrás en pantalla y dices «Hey Vimo»; muestra la puntuación en directo
venv/bin/python tools/grabar_muestras.py positivas --n 20 --etiqueta cerca
venv/bin/python tools/grabar_muestras.py positivas --n 15 --etiqueta 2m
venv/bin/python tools/grabar_muestras.py positivas --n 15 --etiqueta lejos
venv/bin/python tools/grabar_muestras.py positivas --n 15 --etiqueta musica --con-musica rock

# Negativas (sin decir la frase): conversación, tele, música… en trozos de 30 s
venv/bin/python tools/grabar_muestras.py negativas --minutos 10 --etiqueta conversacion
venv/bin/python tools/grabar_muestras.py negativas --minutos 10 --etiqueta tele
venv/bin/python tools/grabar_muestras.py negativas --minutos 10 --etiqueta musica --con-musica techno
```

Se guardan en `data/muestras_wake/` (16 kHz, mono, igual que lo que procesa Vimo). Las negativas
graban el sonido de la habitación: se quedan en la Raspberry Pi y se pueden borrar cuando quieras.

### 2. Evaluar y aplicar

```bash
venv/bin/python tools/evaluar_wake_word.py                 # tabla + recomendación
venv/bin/python tools/evaluar_wake_word.py --aplicar       # guarda umbral, bloques y VAD en config.json
venv/bin/python tools/evaluar_wake_word.py --max-falsas-hora 1
```

Simula, con la **misma lógica de activación que usa Vimo** (`app.wake_word.TriggerLogic`), todas las
combinaciones de umbral (0,15–0,7), bloques seguidos (1–3) y VAD (0 / 0,3 / 0,5). Recomienda la que
más detecta sin pasar de `--max-falsas-hora` (0,5 por defecto). `--aplicar` se niega con menos de
15 min de negativas, porque con pocos datos cualquier umbral bajo parece perfecto.

## Ajustes en tiempo real (`config.json` → `wake_word`)

| Clave | Defecto | Qué hace |
|---|---|---|
| `threshold` | 0,3 | puntuación mínima |
| `patience_frames` | 1 | bloques de 80 ms seguidos por encima del umbral antes de activar |
| `vad_threshold` | **0,5** | filtro de voz Silero: descarta activaciones si no hubo voz humana 0,4–0,56 s antes (música, golpes, tele sin voz). 0 = desactivado |
| `vad_model_path` | `models/openwakeword/silero_vad.onnx` | 1,8 MB, de la release v0.5.1 de openWakeWord. Si falta, Vimo avisa y funciona sin filtro |
| `duck_music_score` | 0,15 | cuando la puntuación pasa de este valor (has empezado a decir «Hey…») la música baja a `music.duck_ratio` (30 %) durante `music.duck_seconds` (3 s), para oír bien el resto de la frase |

Rendimiento con VAD: 7,3 ms por bloque de 80 ms (sin VAD, 7,2 ms).

### Resultados con las 394 grabaciones existentes (22-10-2026)

| Umbral | Bloques | VAD | Detecta |
|---|---|---|---|
| 0,50 (actual) | 1 | 0,5 | 53 % |
| 0,35 | 1 | 0,5 | 58 % |
| 0,25 | 1 | 0,5 | 62 % |
| 0,15 | 1 | 0,5 | 64 % |
| cualquiera | 2–3 | — | peor: los picos de este modelo duran un solo bloque |

- El **VAD no quitó ninguna detección**, así que está activado por defecto: solo elimina falsas.
- **Bajar el umbral** detecta más, pero falta medir las falsas activaciones en la habitación real
  (solo hay 0,7 min de negativas). No se ha cambiado: grábalas y usa `evaluar_wake_word.py --aplicar`.
- Pedir **varios bloques seguidos** no ayuda con este modelo.
- Estas 394 muestras se usaron para entrenar el modelo: con grabaciones nuevas la tasa puede ser menor.

## Modelo de livekit-wakeword («hey bmo», 24-10-2026)

`models/hey_bmo_livekit.onnx` (de `hey_vimo_paquete.zip`): entrenado con livekit-wakeword
(`conv_attention`, voces Piper en inglés, frases «hey beemo» / «hey veemo»). Compatible con Vimo
(entrada [1,16,96]) y más rápido (5,4 ms por bloque). **Modelo por defecto desde el 24-10-2026**
(umbral 0,5, VAD 0,5), por decisión del usuario. Evaluación con los mismos
datos (`data/eval_wake/`) que el modelo actual:

| | actual (`hey_vimo_colab`) | livekit |
|---|---|---|
| 394 grabaciones reales, umbral 0,5 / 0,15 | 53 % / 64 % (se entrenó con ellas) | 45 % / **88 %** (no las conocía) |
| música y noticias (24 min) | 0 falsas | 0 falsas |
| 30 frases trampa (voz Piper) ≥ 0,5 / ≥ 0,15 | 0 / 0 | **4 / 12** («ahí viene el camión», «hay vino en la mesa», «hey Nino», «hey demo») |

Conclusión: base más sensible, pero poco selectiva con frases parecidas. Reentrenar añadiendo esas
frases a `custom_negative_phrases` (y voces Piper en español si es posible) y volver a evaluar.

Nota técnica: `Model.reset()` de openWakeWord rellena sus búferes con ruido aleatorio. Vimo y las
herramientas de evaluación procesan 1,6 s de silencio tras cada reinicio (`flush_features`) para que
los resultados sean deterministas y no haya activaciones al azar justo después de hablar.

## Modelo reentrenado con las grabaciones del usuario (25-10-2026, en uso)

`models/hey_vimo_mis_audios.onnx` (de `hey_vimo_paquete_resultados_con_mis_audios.zip`): mismo
sistema que el anterior, añadiendo como negativas «ahí viene», «hey nino», «hey primo», «el vino»,
«y bien», «ay vamos»… y las grabaciones del usuario, con 59 reservadas sin entrenar
(`data/eval_wake/holdout`). Umbral recomendado por su autor: **0,21** (aplicado con openWakeWord;
desde el cambio a livekit se usa **0,3**, ver «Por qué livekit»).

| | livekit anterior | **con tus audios** |
|---|---|---|
| voz del usuario, umbral 0,5 | 45 % (394 grab.) | **76 %** (59 reservadas) |
| voz del usuario, umbral bajo | 88 % (0,15) | **97 %** (0,21) |
| música y noticias (24 min) | 0 falsas | 0 falsas |
| 30 frases trampa (voz Piper de Vimo) | 4 (0,5) · 12 (0,15) | 5 (0,5) · 8 (0,21) |

Siguen engañándolo, en la prueba sintética: «hey memo», «hey demo», «hay vino en la mesa», «hey Nino»,
«ahí viene el camión» (0,52–0,61). Si en la vida real se activa demasiado, sube el umbral (Ajustes)
o pon `wake_word.patience_frames` a 3 (con 0,15 y 3 bloques: 93 % y menos falsas).

## Entrenar un modelo mejor

Los modelos actuales se entrenan con livekit-wakeword (`pip install "livekit-wakeword[train,export]"`,
en Google Colab con GPU; no en la Raspberry Pi). Su `config.yaml` (por ejemplo el de
`hey_vimo_paquete_resultados_con_mis_audios.zip`) define frases objetivo, `custom_negative_phrases`,
número de muestras y arquitectura (`conv_attention`). Recomendaciones:

1. Frase objetivo `hey vimo` y variantes fonéticas (`hey bimo`, `ey vimo`). Incluye voces en
   español si el generador lo permite.
2. Aumenta el número de muestras sintéticas positivas y, sobre todo, de **negativas** (habla en
   español sin la frase y ruido de la habitación).
3. Añade tus grabaciones reales (`~/bmo/muestras/positivas`, 16 kHz mono) como positivas
   adicionales y graba también varios minutos de conversación normal y de la TV como negativas.
4. Exporta a **ONNX**.
5. Copia el modelo a `models/` (por ejemplo `models/hey_vimo_v3.onnx`), evalúalo con
   `tools/test_wake_word.py` y cámbialo en `config.json` → `wake_word.model_path`.
