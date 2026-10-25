# Dependencias, versiones y requisitos

Verificadas en la Raspberry Pi 5 de destino el 21-10-2026 (Debian 13.x trixie, kernel
6.12.75+rpt-rpi-2712, aarch64, Python 3.13.5).

## Sistema (APT)

| Paquete | Versión instalada | Uso |
|---|---|---|
| python3 / python3-venv | 3.13.5-1 | intérprete y entorno virtual |
| python3-tk | 3.13.5-1 | GUI (Tk 8.6) |
| python3-pil | 11.1.0-5+deb13u4 | imágenes |
| **python3-pil.imagetk** | 11.1.0-5+deb13u4 | `PIL.ImageTk` (no venía instalado; sin él la GUI no arranca) |
| python3-numpy | 2.2.4 | señal de audio |
| python3-picamera2 | 0.3.34-1 | cámara |
| python3-libcamera | 0.7.0+rpt20260205-1 | pila de cámara (versión específica instalada para la IMX519; no se actualiza) |
| python3-av (PyAV) | 14.2.0-1 | contenedor de vídeo en Picamera2 |
| python3-simplejpeg | 1.8.1 | JPEG desde YUV420 (fotos durante la grabación) |
| libportaudio2 | 19.6.0 | audio para sounddevice |
| ffmpeg / ffprobe | 7.1.3 | MP4 final y verificación de vídeos |
| mpv | 0.40.0 | reproducción de música (IPC) |
| nodejs | 22.23.4 | motor JavaScript para yt-dlp/YouTube (ya instalado; lo usa también Claude Code) |
| cmake, build-essential, git, curl | — | compilar whisper.cpp, descargas |
| PipeWire / WirePlumber | 1.4.2 | servidor de audio del escritorio |

## Python (venv con `--system-site-packages`)

| Paquete | Versión | Notas |
|---|---|---|
| openwakeword | 0.6.0 | instalado con `--no-deps`: su metadato exige `tflite-runtime`, que no tiene wheel para Python 3.13. Sin esto, pip retrocede a 0.4.0. Vimo usa ONNX. |
| onnxruntime | 1.31.0 | CPU (`CPUExecutionProvider`); no hay CUDA en la Pi 5. Puede avisar «GPU device discovery failed», un aviso inofensivo. |
| scipy | 1.18.1 | remuestreo polifásico (y dependencia de openwakeword) |
| scikit-learn, tqdm, requests | 1.9.1, 4.67.1, 2.32.3 | dependencias reales de openwakeword |
| sounddevice | 0.5.6 | captura/reproducción (PortAudio) |
| piper-tts | 1.8.0 | OHF-Voice/piper1-gpl, wheel aarch64 con espeak-ng incluido (GPL-3.0) |
| pathvalidate | 3.3.1 | dependencia de piper-tts |
| ddgs | 9.16.0 | cliente alternativo de DuckDuckGo |
| mcp | 2.3.0 | servidor MCP (API 2.x: `MCPServer`, antes `FastMCP`) |
| yt-dlp[default] | 2026.10.21 (+ yt-dlp-ejs 0.8.0) | música de YouTube. Se instala con `--upgrade` porque el venv ve el `python3-yt-dlp` 2025.04.30 de APT y pip lo daba por bueno |

## Herramientas externas

| Herramienta | Versión | Ruta |
|---|---|---|
| Claude Code | 2.1.307 | `/usr/bin/claude` → `/usr/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe` |
| whisper.cpp | commit 79218f51 (16-03-2026) | `~/whisper.cpp/build/bin/{whisper-cli,whisper-server}` |

## Modelos

| Modelo | Tamaño | Ruta | Origen |
|---|---|---|---|
| Wake word «Hey Vimo» | 200 KB | `models/hey_vimo_colab.onnx` | entrenado por el usuario |
| melspectrogram / embedding | 1,1 MB + 1,3 MB | `models/openwakeword/` | openWakeWord v0.5.1 |
| Whisper small (q5_1) | 182 MB | `~/whisper.cpp/models/ggml-small-q5_1.bin` | ggerganov/whisper.cpp (HF) |
| Whisper base (q5_1) | 57 MB | `~/whisper.cpp/models/ggml-base-q5_1.bin` | alternativa ligera |
| Voz Piper «voz_bmo_final_v2» | 61 MB | `voices/voz_bmo_final_v2.onnx` (+ `.json`) | voz del usuario, es-419, 22 050 Hz |

## Hardware

| Dispositivo | Detectado como | Formatos |
|---|---|---|
| Micrófono USB | `iTalk-02: USB Audio` (el número `hw:N,0` cambia según el puerto; Vimo lo busca por nombre) | captura mono S16, **44,1/48 kHz** (no 16 kHz) |
| Altavoz | **Bluetooth BT-017** (desde el 22-10-2026), perfil A2DP, códec SBC, salida predeterminada de PipeWire. Antes: USB C-Media 0d8c:0012 | Vimo usa la salida `default` (PipeWire) a 48 kHz; probado: efectos y voz completos, sin cortes |
| Cámara | `imx519` en `/base/axi/pcie@1000120000/rp1/i2c@80000/imx519@1a` | modos 1280×720@80 … 4656×3496@9 |
| Pantalla | DSI-1 800×480 táctil | — |
| SAI | Suptronics X120x (driver `x120x`) | batería y estado de red en `/sys/class/power_supply/x120x-*` |

## Incompatibilidades encontradas y soluciones

| Problema | Solución |
|---|---|
| `tflite-runtime` no existe para Python 3.13 / aarch64 | backend ONNX + `openwakeword --no-deps` |
| `PIL.ImageTk` ausente | `apt install python3-pil.imagetk` |
| El micrófono no admite 16 kHz | captura a 48 kHz y remuestreo ×1/3 |
| `mcp` 2.x renombró `FastMCP` | se usa `mcp.server.mcpserver.MCPServer` |
| ddgs recibe HTTP 202 (anti-bots) | cliente HTML propio, ddgs como alternativa |
| La Pi 5 no tiene codificador H.264 por hardware | `H264Encoder` = `LibavH264Encoder` (software) |
| yt-dlp de APT (2025) obsoleto para YouTube | yt-dlp actual en el venv con `--upgrade` |
| HTTP 403 al abrir con mpv la URL del stream de YouTube | descarga del audio con yt-dlp a una caché temporal |
