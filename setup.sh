#!/usr/bin/env bash
# Instalación idempotente de Vimo en Raspberry Pi 5 / Raspberry Pi OS (Debian 13 trixie, aarch64).
#
#   ./setup.sh                      instala lo que falte y ejecuta el diagnóstico
#   ./setup.sh --build-whisper      además clona y compila whisper.cpp si no existe
#   ./setup.sh --download-models    además descarga los modelos que falten (pide confirmación)
#   ./setup.sh --yes                no pregunta (para usar con --download-models)
#
# No reinstala Claude Code, no toca su configuración ni su sesión, no actualiza paquetes ya
# instalados (libcamera/rpicam pueden ser versiones específicas para la IMX519) y nunca usa
# `sudo pip`. Las dependencias Python van en ./venv.
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"
ROOT="$PWD"
BUILD_WHISPER=0
DOWNLOAD=0
YES=0
for arg in "$@"; do
  case "$arg" in
    --build-whisper) BUILD_WHISPER=1 ;;
    --download-models) DOWNLOAD=1 ;;
    --yes|-y) YES=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "Opción desconocida: $arg"; exit 2 ;;
  esac
done

G='\033[0;32m'; Y='\033[1;33m'; R='\033[0;31m'; N='\033[0m'
step() { echo -e "\n${G}==> $*${N}"; }
warn() { echo -e "${Y}AVISO:${N} $*"; }
fail() { echo -e "${R}ERROR:${N} $*"; }
confirm() {  # confirm "pregunta"  -> 0 si sí
  [ "$YES" = 1 ] && return 0
  read -r -p "$1 [s/N] " ans; [[ "$ans" =~ ^[sSyY]$ ]]
}

WHISPER_DIR="${WHISPER_DIR:-$HOME/whisper.cpp}"

# ---------------------------------------------------------------------------------------
step "1/10 Sistema"
. /etc/os-release
echo "Sistema: $PRETTY_NAME · arquitectura $(uname -m) · $(python3 --version)"
[ "${VERSION_CODENAME:-}" = "trixie" ] || warn "Probado en Debian 13 (trixie); este sistema es ${VERSION_CODENAME:-desconocido}."
[ "$(uname -m)" = "aarch64" ] || warn "Probado en aarch64 (ARM64)."
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' || { fail "Se necesita Python 3.11 o superior"; exit 1; }

# ---------------------------------------------------------------------------------------
step "2/10 Paquetes del sistema (APT)"
APT_PKGS=(python3-venv python3-tk python3-pil python3-pil.imagetk python3-numpy
          python3-picamera2 python3-libcamera python3-av python3-simplejpeg
          libportaudio2 ffmpeg mpv git curl cmake build-essential)
MISSING=()
for p in "${APT_PKGS[@]}"; do
  dpkg-query -W -f='${Status}' "$p" 2>/dev/null | grep -q "install ok installed" || MISSING+=("$p")
done
if [ ${#MISSING[@]} -eq 0 ]; then
  echo "Todos los paquetes necesarios están instalados."
else
  echo "Faltan: ${MISSING[*]}"
  sudo apt-get update
  # --no-upgrade: no se actualiza nada que ya esté instalado.
  sudo apt-get install -y --no-upgrade "${MISSING[@]}"
fi

# ---------------------------------------------------------------------------------------
step "3/10 Entorno virtual"
if [ ! -x venv/bin/python ]; then
  python3 -m venv --system-site-packages venv
  echo "Creado venv/ (con acceso a Picamera2, Tkinter y Pillow del sistema)."
else
  echo "venv/ ya existe."
fi

# ---------------------------------------------------------------------------------------
step "4/10 Paquetes Python"
venv/bin/python -m pip install --upgrade --quiet pip
venv/bin/python -m pip install --quiet -r requirements.txt
# yt-dlp debe ir actualizado (YouTube cambia a menudo); --upgrade para no quedarse con el de APT.
venv/bin/python -m pip install --quiet --upgrade "yt-dlp[default]"
sync
venv/bin/python -I tools/verify_venv.py || { fail "Hay paquetes dañados en venv (¿corte de energía?). Reinstala con: venv/bin/pip install --force-reinstall --no-cache-dir <paquete>"; exit 1; }

# ---------------------------------------------------------------------------------------
step "5/10 Directorios"
mkdir -p models/openwakeword voices recordings photos logs data/claude_workspace
echo "OK"

# ---------------------------------------------------------------------------------------
step "6/10 Recursos originales (faces/ y sounds/)"
for d in idle listening thinking speaking error warmup capturing; do
  n=$(find "faces/$d" -maxdepth 1 -name '*.png' 2>/dev/null | wc -l)
  printf "  faces/%-10s %s imágenes\n" "$d" "$n"
done
for d in greeting_sounds ack_sounds thinking_sounds; do
  n=$(find "sounds/$d" -maxdepth 1 -name '*.wav' 2>/dev/null | wc -l)
  printf "  sounds/%-16s %s sonidos\n" "$d" "$n"
done
[ -d faces/idle ] || warn "No hay faces/idle: copia las caras originales de https://github.com/brenpoly/be-more-agent"

# ---------------------------------------------------------------------------------------
step "7/10 Claude Code (instalación existente, no se modifica)"
if command -v claude >/dev/null; then
  echo "Ejecutable: $(readlink -f "$(command -v claude)")"
  echo "Versión:    $(env -u CLAUDECODE claude --version 2>/dev/null)"
  if env -u CLAUDECODE claude auth status 2>/dev/null | grep -q '"loggedIn": true'; then
    echo "Sesión:     iniciada"
  else
    warn "Claude Code no tiene sesión iniciada. Ejecuta 'claude' en un terminal e inicia sesión."
  fi
else
  fail "No se encuentra 'claude' en el PATH. Instálalo según https://code.claude.com/docs (Vimo no lo instala)."
fi

# ---------------------------------------------------------------------------------------
step "8/10 Modelos"
download() {  # download URL DESTINO DESCRIPCIÓN
  local url="$1" dest="$2" desc="$3"
  if [ "$DOWNLOAD" = 1 ] && confirm "¿Descargar $desc en $dest?"; then
    curl -L --fail -o "$dest.part" "$url" && mv "$dest.part" "$dest" && sync
  else
    warn "Falta $dest ($desc). Descárgalo con: curl -L -o '$dest' '$url'  (o ./setup.sh --download-models)"
  fi
}
# Los auxiliares de la wake word son los mismos que trae livekit-wakeword (mismo md5); se publican
# en la release v0.5.1 de openWakeWord, de donde se descarga también el filtro de voz Silero.
OWW_REL="https://github.com/dscripka/openWakeWord/releases/download/v0.5.1"
[ -s models/openwakeword/melspectrogram.onnx ] || download "$OWW_REL/melspectrogram.onnx" models/openwakeword/melspectrogram.onnx "espectrograma mel de la wake word (≈1,1 MB; si falta se usa el de livekit-wakeword)"
[ -s models/openwakeword/embedding_model.onnx ] || download "$OWW_REL/embedding_model.onnx" models/openwakeword/embedding_model.onnx "embeddings de la wake word (≈1,3 MB; si falta se usa el de livekit-wakeword)"
[ -s models/openwakeword/silero_vad.onnx ] || download "$OWW_REL/silero_vad.onnx" models/openwakeword/silero_vad.onnx "filtro de voz Silero VAD (≈1,8 MB)"
WAKE_MODEL=$(venv/bin/python -c 'from app.config_manager import ConfigManager; print(ConfigManager().path_for("wake_word.model_path"))' 2>/dev/null || echo models/hey_vimo.onnx)
if [ -s "$WAKE_MODEL" ]; then
  echo "Wake word personalizada: $WAKE_MODEL"
else
  warn "No existe el modelo de wake word $WAKE_MODEL. Vimo no descarga uno genérico: entrena el tuyo (docs/wake_word.md). Mientras tanto usa el botón Hablar."
fi

if [ ! -x "$WHISPER_DIR/build/bin/whisper-cli" ]; then
  if [ "$BUILD_WHISPER" = 1 ]; then
    [ -d "$WHISPER_DIR" ] || git clone --depth 1 https://github.com/ggml-org/whisper.cpp "$WHISPER_DIR"
    cmake -S "$WHISPER_DIR" -B "$WHISPER_DIR/build" -DCMAKE_BUILD_TYPE=Release
    cmake --build "$WHISPER_DIR/build" -j"$(nproc)" --config Release --target whisper-cli whisper-server
  else
    warn "No se encuentra whisper.cpp compilado en $WHISPER_DIR (usa ./setup.sh --build-whisper)."
  fi
else
  echo "whisper.cpp: $WHISPER_DIR/build/bin (whisper-cli$( [ -x "$WHISPER_DIR/build/bin/whisper-server" ] && echo ', whisper-server'))"
fi
WMODEL=$(venv/bin/python -c 'from app.config_manager import ConfigManager; print(ConfigManager().get("stt.model"))' 2>/dev/null || echo models/ggml-small-q5_1.bin)
if [ -s "$WHISPER_DIR/$WMODEL" ]; then
  echo "Modelo Whisper: $WHISPER_DIR/$WMODEL ($(du -h "$WHISPER_DIR/$WMODEL" | cut -f1))"
else
  name=$(basename "$WMODEL" .bin); name=${name#ggml-}
  download "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-$name.bin" "$WHISPER_DIR/$WMODEL" "modelo Whisper $name (small-q5_1 ≈ 190 MB, base-q5_1 ≈ 60 MB)"
fi
VOICE=$(venv/bin/python -c 'from app.config_manager import ConfigManager; print(ConfigManager().path_for("tts.voice_model"))' 2>/dev/null || echo voices/voz.onnx)
if [ -s "$VOICE" ] && [ -s "$VOICE.json" ]; then
  echo "Voz Piper: $VOICE ($(du -h "$VOICE" | cut -f1))"
else
  warn "Falta la voz de Piper $VOICE (+ .json). Ver docs/piper.md para descargar una voz en español."
fi

# ---------------------------------------------------------------------------------------
command -v node >/dev/null && echo "Node.js $(node --version) (motor JavaScript para yt-dlp/YouTube)" \
  || warn "No hay Node.js: yt-dlp puede fallar con YouTube. Instala nodejs o cambia music.js_runtime."

# ---------------------------------------------------------------------------------------
venv/bin/python -c 'from app.echo_cancel import ensure_alsa_pcms; ensure_alsa_pcms()' \
  && echo "Dispositivos ALSA de cancelación de eco listos en ~/.asoundrc"

# ---------------------------------------------------------------------------------------
step "9/10 Configuración"
if [ -f config.json ]; then
  echo "config.json existente: se conserva (solo se completan claves que falten al arrancar)."
else
  venv/bin/python -c 'from app.config_manager import ConfigManager; ConfigManager()' && echo "Creado config.json con valores predeterminados."
fi
chmod +x start_agent.sh main.py tools/*.py 2>/dev/null || true

# ---------------------------------------------------------------------------------------
step "10/10 Diagnóstico"
venv/bin/python main.py --check || warn "Hay componentes con problemas (ver arriba). Vimo arranca igualmente con funciones limitadas."
echo -e "\n${G}Listo.${N} Arranca Vimo con: ./start_agent.sh   (autoarranque: docs/instalacion.md)"
