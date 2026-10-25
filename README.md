# Vimo

Agente conversacional por voz para **Raspberry Pi 5** con **Raspberry Pi OS (Debian 13 trixie, ARM64)**.
Tiene cara animada, palabra de activación propia («Hey Vimo»), usa **Claude Code** como motor de
razonamiento, busca en **DuckDuckGo** cuando hace falta información actual, responde con voz y
hace fotos y vídeos con la cámara **Sony IMX519** cuando se le pide. También funciona como
**reproductor de música**: pone canciones de YouTube y sigue con canciones similares hasta que le
digas que pare.

Toma como referencia visual y funcional [Be More Agent](https://github.com/brenpoly/be-more-agent)
de brenpoly (licencia MIT) y reutiliza sus **caras** (`faces/`) y **sonidos** (`sounds/`) originales.
Ver [Créditos y licencias](#créditos-y-licencias).

```
 micrófono ──► OpenWakeWord ──► Whisper.cpp ──► Claude Code ──► Piper ──► altavoz
   (USB)       «Hey Vimo»       (local, es)     (claude -p)     (local)    (USB)
                                                    │
                                    servidor MCP de Vimo: web_search (DuckDuckGo)
   GUI Tkinter (caras originales) · cámara IMX519 por Picamera2 (fotos y vídeo bajo demanda)
```

## Qué es local y qué no

| Componente | Dónde se ejecuta | Qué sale del dispositivo |
|---|---|---|
| Wake word (OpenWakeWord, ONNX) | Raspberry Pi | nada |
| Reconocimiento de voz (Whisper.cpp) | Raspberry Pi | nada |
| Síntesis de voz (Piper) | Raspberry Pi | nada |
| Razonamiento (Claude Code) | servicio de Anthropic, con la sesión ya iniciada en `claude` | el **texto** transcrito de cada consulta, el historial de la sesión y los resultados de búsqueda |
| Búsqueda web | DuckDuckGo | solo el **texto de la búsqueda** que decide Claude |
| Música | YouTube (yt-dlp) + mpv local | el **nombre de la canción** pedida y las peticiones de descarga del audio |
| Cámara | Raspberry Pi | nada (las fotos y vídeos se quedan en `photos/` y `recordings/`) |

No se envía audio ni imágenes a ningún servicio. No se graba audio ni vídeo de forma permanente
salvo que el usuario lo pida explícitamente.

## Puesta en marcha rápida

```bash
cd ~/bmoclaude
./setup.sh                 # idempotente: instala lo que falte y ejecuta el diagnóstico
./start_agent.sh           # arranca la GUI (pantalla completa; Esc alterna ventana)
                           # o doble clic en el icono «Vimo» del escritorio
./start_agent.sh --windowed
venv/bin/python main.py --check   # diagnóstico sin GUI, sin sonido y sin encender la cámara
```

Requisitos previos: Claude Code instalado **y con sesión iniciada** (`claude auth status`),
whisper.cpp compilado en `~/whisper.cpp` (o `./setup.sh --build-whisper`), micrófono y altavoz USB,
y la cámara IMX519 funcionando con `rpicam-hello`. Detalles en [docs/instalacion.md](docs/instalacion.md).

## Uso

- Di **«Hey Vimo»** y, tras el sonido de confirmación, haz la pregunta. Vimo deja de escuchar tras
  ~1 s de silencio (configurable).
- **Hablar**: pulsar para hablar (no necesita la wake word). Mientras Vimo habla, lo interrumpe y
  vuelve a escuchar.
- **Parar**: interrumpe la respuesta hablada o cancela la petición en curso; la conversación se conserva.
- **Escucha ✓/✗**: activa o desactiva la wake word.
- **Foto** / **Grabar** (Detener): cámara. Durante la grabación se ve `● REC mm:ss`.
- **Texto**: muestra la transcripción, la respuesta completa (con Markdown, código…), las **fuentes**
  de las búsquedas y un campo para escribir consultas.
- **Nueva**: empieza una conversación nueva. **Ajustes**: umbral, velocidad de voz, dispositivos, modelo
  Whisper, perfil de vídeo, historial. **Salir**: cierre ordenado (detiene la grabación y libera la cámara).

**En reposo** Vimo hace de vez en cuando un gesto breve (parpadear, mirar a los lados, sonreír,
respirar, bostezar, adormilarse con «z z z») y vuelve a su cara de reposo. Tras **2 minutos** sin
actividad (sin música ni grabación) la pantalla se pone negra y se apaga la retroiluminación. Se
despierta al instante con «Hey Vimo», al tocar la pantalla, mover el ratón o pulsar una tecla; el
primer toque solo despierta y no pulsa ningún botón. Ajustes: `gui.sleep_after_seconds` (0 = nunca),
`gui.sleep_backlight`, `faces.idle_gesture_min_seconds` / `max_seconds`, `faces.idle_animations`.
Las caras `faces/idle_*` se generan con `tools/generar_caras_reposo.py` a partir de las originales.

Órdenes de voz que Vimo ejecuta **localmente** (no pasan por Claude Code):

| Orden | Acción |
|---|---|
| «Toma una foto», «sácame una foto» | Foto en `photos/foto_AAAAMMDD-HHMMSS.jpg` |
| «Graba un vídeo», «empieza a grabar» | Inicia la grabación |
| «Detén la grabación», «deja de grabar» | Detiene, genera y verifica el MP4 |
| «Nueva conversación», «olvida todo» | Reinicia el contexto |
| «Pon *De música ligera*», «ponme algo de Zoé» | Música de YouTube y después canciones similares ([docs/musica.md](docs/musica.md)) |
| «Reproduce rock», «reproduce techno», «pon house», «pon rock de los 80» | Música por **género**, sin parar hasta que digas «para» |
| «Siguiente canción», «sube / baja el volumen», «pausa», «continúa» | Control de la música |
| «Para la música», «para», «basta» | Detiene la música |

Las preguntas («¿cómo se graba un vídeo?») no activan la cámara: se responden como cualquier consulta.

## Estructura

```
bmoclaude/
├── main.py                  # punto de entrada (GUI o --check)
├── config.json              # configuración (se crea/completa automáticamente)
├── requirements.txt         # dependencias Python verificadas
├── setup.sh / start_agent.sh
├── app/
│   ├── controller.py        # orquestador: turnos, estados, órdenes locales
│   ├── gui.py               # interfaz Tkinter (solo presenta y delega)
│   ├── state_manager.py     # máquina de estados explícita
│   ├── animation_manager.py # carga y animación de las caras
│   ├── audio_manager.py     # micrófono, reproducción interrumpible, sonidos
│   ├── wake_word.py         # livekit-wakeword (+ VAD Silero)
│   ├── speech_to_text.py    # fin de intervención (VAD) + Whisper.cpp
│   ├── claude_code_client.py# adaptador de `claude -p`
│   ├── mcp_server.py        # herramienta web_search para Claude Code (MCP)
│   ├── web_search.py        # cliente de DuckDuckGo
│   ├── text_to_speech.py    # Piper + adaptación de texto para voz
│   ├── intents.py           # órdenes locales explícitas
│   ├── music_player.py      # música: YouTube (yt-dlp) + mpv, cola de similares
│   ├── camera_manager.py    # Picamera2: propietario único de la cámara, fotos
│   ├── video_recorder.py    # grabación, MP4 verificado, recuperación
│   ├── conversation_memory.py
│   └── config_manager.py
├── faces/  sounds/          # recursos originales de Be More Agent (sin modificar)
├── models/                  # wake word + modelos auxiliares de openWakeWord
├── voices/                  # voz de Piper (.onnx + .onnx.json)
├── recordings/  photos/     # capturas del usuario
├── data/                    # memoria, estado de búsqueda, espacio de trabajo de Claude Code
├── logs/                    # vimo.log, whisper-server.log, power.log
├── deploy/                  # autoarranque (.desktop) y servicio systemd opcional
├── tools/                   # diagnóstico: verify_venv, power_logger, test_wake_word
├── tests/                   # pruebas automatizadas (unittest)
└── docs/                    # guías
```

## Documentación

| Guía | Contenido |
|---|---|
| [docs/instalacion.md](docs/instalacion.md) | Instalación, arranque manual y automático |
| [docs/claude_code.md](docs/claude_code.md) | Integración con Claude Code, sesiones, permisos |
| [docs/wake_word.md](docs/wake_word.md) | Wake word con livekit-wakeword: modelo, ajustes, evaluación |
| [docs/whisper.md](docs/whisper.md) | Whisper.cpp: compilación, modelos, idioma, rendimiento |
| [docs/piper.md](docs/piper.md) | Piper TTS: voces, parámetros |
| [docs/duckduckgo.md](docs/duckduckgo.md) | Búsqueda web y servidor MCP |
| [docs/camara_video.md](docs/camara_video.md) | Fotos y grabación de vídeo |
| [docs/musica.md](docs/musica.md) | Reproductor de música (YouTube, canciones similares, volumen) |
| [docs/eco.md](docs/eco.md) | Cancelación de eco: oír «Hey Vimo» con música sonando |
| [docs/dependencias.md](docs/dependencias.md) | Tabla de dependencias y versiones |
| [docs/diagnostico.md](docs/diagnostico.md) | Solución de problemas (incluye los apagados por alimentación) |
| [docs/pruebas.md](docs/pruebas.md) | Registro de pruebas y limitaciones conocidas |

## Pruebas

```bash
venv/bin/python -m unittest discover -s tests -v
```

Las pruebas automatizadas usan simulaciones (un `claude` falso, audio sintético, modelos simulados)
y **no** validan el hardware. Lo verificado en la Raspberry Pi real y lo pendiente está en
[docs/pruebas.md](docs/pruebas.md).

## Seguridad y privacidad

- Claude Code se ejecuta **sin herramientas integradas** (`--tools ""`: ni Bash, ni edición, ni
  lectura de archivos), con `--permission-mode dontAsk` y solo la herramienta `mcp__vimo__web_search`
  permitida. Nunca se usa `--dangerously-skip-permissions` (config.json lo rechaza).
- La cámara no está expuesta a Claude: solo responde a órdenes explícitas del usuario o a los botones.
- Los resultados web se marcan como datos no confiables; el sistema indica a Claude que ignore
  instrucciones incrustadas en ellos y que no invente fuentes.
- El texto del usuario se pasa por stdin, sin shell. No se registran tokens ni credenciales; la
  autenticación es la de Claude Code y Vimo no la lee ni la modifica.
- Las sesiones de Vimo usan su propio directorio (`data/claude_workspace`) y no tocan las demás
  sesiones ni la configuración de Claude Code del usuario.

## Créditos y licencias

- **Caras** (`faces/`) y **sonidos originales** (`sounds/sonidos en Ingles/`) de
  [Be More Agent](https://github.com/brenpoly/be-more-agent) © 2026 brenpoly, licencia MIT
  (copia en [LICENSE-be-more-agent](LICENSE-be-more-agent)). Los sonidos de primer nivel de `sounds/`
  son versiones con los mismos nombres proporcionadas por el usuario. Las caras de música
  `faces/music_*` son obras derivadas de esas caras originales (recombinación de sus ojos y bocas
  con `tools/generar_caras_musica.py`), bajo la misma licencia MIT.
- «BMO» y «Adventure Time» son marcas de Cartoon Network (Warner Bros. Discovery). Este proyecto es
  una creación de aficionado sin relación con ellas, como indica el aviso legal de Be More Agent.
- [openWakeWord](https://github.com/dscripka/openWakeWord): código Apache-2.0; según su README, los
  modelos preentrenados que distribuye tienen licencia CC BY-NC-SA 4.0 (uso no comercial). Los
  modelos auxiliares `models/openwakeword/*.onnx` proceden de esa distribución; el modelo de la
  wake word «Hey Vimo» lo entrenó el usuario.
- Software de terceros:
  [whisper.cpp](https://github.com/ggml-org/whisper.cpp) (MIT),
  [Piper](https://github.com/OHF-Voice/piper1-gpl) (GPL-3.0),
  [ddgs](https://github.com/deedy5/ddgs) (MIT), [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) (MIT),
  [Picamera2](https://github.com/raspberrypi/picamera2) (BSD-2-Clause).
  Piper es GPL-3.0: si distribuyes Vimo junto con piper-tts, respeta los términos de la GPL.
- [yt-dlp](https://github.com/yt-dlp/yt-dlp) (Unlicense; yt-dlp-ejs: Unlicense/MIT/ISC) y
  [mpv](https://mpv.io) (LGPL-2.1+) para la música. El contenido de YouTube está sujeto a sus
  condiciones de uso.
