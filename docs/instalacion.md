# Instalación y arranque

Probado en: Raspberry Pi 5 (16 GB) · Raspberry Pi OS basado en Debian 13 «trixie» · aarch64 ·
Python 3.13.5 · escritorio labwc (Wayland) · pantalla DSI 800×480.

## 1. Requisitos previos

| Requisito | Comprobación |
|---|---|
| Claude Code instalado y con sesión | `claude --version` y `claude auth status` → `"loggedIn": true` |
| Micrófono USB | `arecord -l` debe listar un dispositivo de **captura** |
| Altavoz USB | `aplay -l` |
| Cámara IMX519 | `rpicam-hello --list-cameras` muestra `imx519` |
| whisper.cpp | `~/whisper.cpp/build/bin/whisper-cli` (o `./setup.sh --build-whisper`) |

> En esta instalación la pila libcamera/rpicam incluye paquetes específicos para la IMX519.
> `setup.sh` **no** actualiza paquetes ya instalados (`apt-get install --no-upgrade`) para no
> romperla.

## 2. setup.sh

```bash
cd ~/bmoclaude
./setup.sh                    # idempotente; se puede repetir
./setup.sh --build-whisper    # clona y compila whisper.cpp si no existe
./setup.sh --download-models  # descarga modelos que falten (muestra tamaño y destino, pide confirmación)
```

Pasos que hace:

1. Comprueba sistema, arquitectura y Python.
2. Instala por APT solo los paquetes que falten: `python3-venv python3-tk python3-pil
   python3-pil.imagetk python3-numpy python3-picamera2 python3-libcamera python3-av
   python3-simplejpeg libportaudio2 ffmpeg git curl cmake build-essential`.
   (`python3-pil.imagetk` es imprescindible: en Debian `PIL.ImageTk` va en un paquete aparte.)
3. Crea `venv/` con `--system-site-packages` (para usar Picamera2/libcamera/Tk de APT).
4. Instala `requirements.txt` y `openwakeword==0.6.0 --no-deps` (ver
   [dependencias](dependencias.md)) y verifica la integridad del venv con `tools/verify_venv.py`.
5. Crea `models/ voices/ recordings/ photos/ logs/ data/`.
6. Cuenta las caras y sonidos originales.
7. Comprueba Claude Code **sin reinstalarlo ni tocar su sesión**.
8. Comprueba los modelos (wake word, auxiliares de openWakeWord, Whisper, voz de Piper). Nunca
   descarga un modelo de wake word genérico.
9. Crea `config.json` solo si no existe.
10. Ejecuta `python main.py --check`.

Nunca usa `sudo pip`.

## 3. Arranque manual

```bash
./start_agent.sh              # pantalla completa (Esc o F11 alternan)
./start_agent.sh --windowed
```

`start_agent.sh` comprueba que existe `venv/` y una sesión gráfica, limpia las variables de una
sesión de Claude Code (si se lanza desde un terminal de Claude Code) y ejecuta `main.py`.

## 4. Icono en el escritorio

Ya está instalado en `~/Desktop/Vimo.desktop`, con el icono `deploy/vimo-icon.png` (la cara
original de Vimo). Doble clic para abrir Vimo a pantalla completa. Para reinstalarlo:

```bash
cp deploy/vimo.desktop ~/Desktop/Vimo.desktop && chmod +x ~/Desktop/Vimo.desktop
cp deploy/vimo.desktop ~/.local/share/applications/      # opcional: también en el menú
```

Al abrirlo desde el icono no hay terminal:
- Si Vimo ya está abierto, sale el aviso «Vimo ya está abierto» y no se lanza otra copia (bloqueo
  `flock` sobre `data/vimo.lock`, liberado automáticamente al cerrar, incluso tras un corte de luz).
- Si algo falla al arrancar, aparece una ventana con el error y las últimas líneas de
  `logs/arranque.log`.

## 5. Arranque automático

### Opción recomendada: autoarranque del escritorio (labwc)

labwc ejecuta los archivos de `~/.config/autostart/` al iniciar la sesión gráfica, con el mismo
usuario, entorno gráfico, PipeWire y credenciales de Claude Code que un terminal.

```bash
cp deploy/vimo-autostart.desktop ~/.config/autostart/vimo.desktop
# (ajusta Exec/Path si el proyecto no está en /home/bmo/bmoclaude)
```

Para desactivarlo: `rm ~/.config/autostart/vimo.desktop`. Lanzador de menú opcional:
`cp deploy/vimo.desktop ~/.local/share/applications/`.

### Alternativa: servicio systemd de usuario

`deploy/vimo.service`. **Atención**: en Raspberry Pi OS con labwc `graphical-session.target` está
inactivo y los servicios de usuario no reciben `WAYLAND_DISPLAY`/`DISPLAY`. Un servicio systemd no
tiene automáticamente la misma sesión gráfica ni el mismo entorno que un terminal. Hay que
exportarlo desde la sesión (instrucciones dentro del archivo). No uses un servicio de sistema
(root): Claude Code usa la sesión del usuario en `~/.claude`.

## 6. Desinstalar

Borra la carpeta del proyecto, `~/Desktop/Vimo.desktop` y, si lo instalaste, `~/.config/autostart/vimo.desktop`. Las sesiones
de Claude Code creadas por Vimo están en `~/.claude/projects/` bajo el nombre del directorio
`data/claude_workspace`; puedes borrarlas desde Claude Code si quieres.
