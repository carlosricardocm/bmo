#!/usr/bin/env bash
# Arranca Vimo: activa el entorno virtual, comprueba lo mínimo y lanza la GUI.
#   ./start_agent.sh [--windowed] [--config archivo.json]
# También lo usa el icono del escritorio: sin terminal, los errores se muestran en una ventana.
set -uo pipefail
cd "$(dirname "$(readlink -f "$0")")"

in_terminal() { [ -t 1 ] || [ -t 2 ]; }

# Si Vimo terminó de forma brusca con la pantalla «apagada», devolver el brillo guardado.
restore_backlight() {
  if [ -f data/backlight.saved ]; then
    { read -r bl_path; read -r bl_value; } < data/backlight.saved
    [ -n "${bl_path:-}" ] && [ -w "$bl_path" ] && echo "${bl_value:-255}" > "$bl_path"
    rm -f data/backlight.saved
  fi
}

# Lanza Python en segundo plano y le reenvía SIGTERM/SIGHUP: si bash lo ejecutara en primer plano
# sin reenviar, `systemctl stop` (o un kill) mataría solo a bash y Vimo no se cerraría limpiamente
# (grabación sin cerrar, brillo sin restaurar) hasta el SIGKILL de TimeoutStopSec.
run_vimo() {
  python main.py "$@" &
  local pid=$!
  trap 'kill -TERM "$pid" 2>/dev/null' TERM HUP
  trap ':' INT  # Ctrl+C ya le llega a Python directamente desde el terminal
  wait "$pid"
  local rc=$?
  while kill -0 "$pid" 2>/dev/null; do  # wait se interrumpe al recibir una señal
    wait "$pid"
    rc=$?
  done
  trap - TERM HUP INT
  return $rc
}

fail() {  # fail "mensaje"
  echo "Vimo: $1" >&2
  if ! in_terminal && command -v zenity >/dev/null; then
    zenity --error --title "Vimo no pudo arrancar" --width 420 --text "$1" 2>/dev/null
  fi
  exit 1
}

[ -x venv/bin/python ] || fail "No existe el entorno virtual (venv/). Ejecuta primero ./setup.sh en un terminal."
if [ -z "${DISPLAY:-}" ] && [ -z "${WAYLAND_DISPLAY:-}" ]; then
  fail "No hay sesión gráfica (DISPLAY/WAYLAND_DISPLAY). Arranca Vimo desde el escritorio."
fi
# Si se lanza desde un terminal de Claude Code, no heredar sus variables de sesión.
unset CLAUDECODE CLAUDE_CODE_ENTRYPOINT CLAUDE_CODE_SESSION_ID 2>/dev/null || true
# Claude Code suele estar en ~/.local/bin o /usr/bin; un lanzador gráfico puede no tener ese PATH.
export PATH="$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

# shellcheck disable=SC1091
source venv/bin/activate
if in_terminal; then
  run_vimo "$@"
  status=$?
  restore_backlight
  exit $status
fi

# Desde el icono: si Vimo ya está abierto, avisar sin tocar el registro de la instancia en marcha
# (main.py usa el mismo bloqueo flock sobre data/vimo.lock).
mkdir -p logs data
if command -v flock >/dev/null && ! flock -n data/vimo.lock true; then
  command -v zenity >/dev/null && zenity --info --title "Vimo" --width 300 --text "Vimo ya está abierto." 2>/dev/null
  exit 0
fi
# La salida va al registro y, si Vimo termina con error, se muestra un aviso.
run_vimo "$@" > logs/arranque.log 2>&1
status=$?
restore_backlight
if [ "$status" -ne 0 ]; then
  detail=$(tail -n 8 logs/arranque.log | sed 's/&/\&amp;/g; s/</\&lt;/g; s/>/\&gt;/g')
  fail "Vimo se cerró con un error (código $status).

Últimas líneas de logs/arranque.log:
<tt>$detail</tt>"
fi
