"""Cancelación de eco (AEC) para que Vimo oiga «Hey Vimo» con música sonando.

Sin cancelación, la música que sale del altavoz llega al micrófono mucho más fuerte que la voz
(medido: a volumen 100 el micrófono capta la música casi saturado, unos 16–19 dB por encima de la
voz) y la wake word no se detecta nunca. Con el módulo WebRTC de PipeWire la música se reduce
~30 dB, hasta el nivel del ruido de la habitación.

Vimo lanza su propio proceso `pipewire -c data/aec/vimo_aec.conf` al arrancar y lo cierra al salir:
no modifica la configuración de audio del sistema ni cambia la salida predeterminada. Crea:

    vimo_aec_sink    altavoz virtual: lo que Vimo reproduce (voz, efectos, música) pasa por aquí
                     y sale por el altavoz real que era el predeterminado al arrancar
    vimo_aec_source  micrófono virtual: el micrófono real con la señal del altavoz restada

Para que PortAudio (sounddevice) pueda abrirlos, ~/.asoundrc define los PCM ALSA `vimo_aec_out` y
`vimo_aec_in` (ensure_alsa_pcms los añade si faltan, entre marcas «Vimo»).
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import signal
import subprocess
import time

log = logging.getLogger(__name__)

SINK = "vimo_aec_sink"
SOURCE = "vimo_aec_source"
ALSA_OUT = "vimo_aec_out"
ALSA_IN = "vimo_aec_in"
ASOUNDRC_BLOCK = f"""# --- Vimo: dispositivos con cancelación de eco (los crea Vimo al arrancar; ver docs/eco.md) ---
pcm.{ALSA_OUT} {{
    type pipewire
    playback_node "{SINK}"
    hint {{ show on description "Vimo: salida con cancelación de eco" }}
}}
pcm.{ALSA_IN} {{
    type pipewire
    capture_node "{SOURCE}"
    hint {{ show on description "Vimo: micrófono con cancelación de eco" }}
}}
# --- fin Vimo ---
"""


class EchoCancelError(RuntimeError):
    pass


def ensure_alsa_pcms(path: str = os.path.expanduser("~/.asoundrc")) -> bool:
    """Añade los PCM de Vimo a ~/.asoundrc si no están. Devuelve True si hubo que añadirlos."""
    try:
        with open(path, encoding="utf-8") as fh:
            current = fh.read()
    except FileNotFoundError:
        current = ""
    if f"pcm.{ALSA_IN}" in current and f"pcm.{ALSA_OUT}" in current:
        return False
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(("\n" if current and not current.endswith("\n") else "") + ASOUNDRC_BLOCK)
    log.info("Añadidos los dispositivos ALSA de cancelación de eco a %s", path)
    return True


def _nodes() -> dict[str, dict]:
    out = subprocess.run(["pw-dump"], capture_output=True, text=True, timeout=10).stdout
    nodes = {}
    for obj in json.loads(out or "[]"):
        props = (obj.get("info") or {}).get("props") or {}
        if props.get("node.name"):
            nodes[props["node.name"]] = props
    return nodes


def find_source(spec: str | None) -> str | None:
    """Nombre del nodo de micrófono de PipeWire cuyo nombre o descripción contiene `spec`."""
    if not spec:
        return None
    spec_l = str(spec).lower()
    for name, props in _nodes().items():
        if props.get("media.class") == "Audio/Source" and not name.startswith("vimo_aec") and (
                spec_l in name.lower() or spec_l in str(props.get("node.description", "")).lower()):
            return name
    return None


def _default_node(kind: str) -> str | None:
    target = "@DEFAULT_AUDIO_SINK@" if kind == "sink" else "@DEFAULT_AUDIO_SOURCE@"
    out = subprocess.run(["wpctl", "inspect", target], capture_output=True, text=True, timeout=10).stdout
    for line in out.splitlines():
        if "node.name" in line and "=" in line:
            return line.split("=", 1)[1].strip().strip('"')
    return None


class EchoCanceller:
    def __init__(self, conf_path: str, capture_target: str | None, playback_target: str | None,
                 aec_args: dict | None = None, log_path: str | None = None):
        self.conf_path = conf_path
        self.capture_target = capture_target
        self.playback_target = playback_target
        self.aec_args = aec_args or {}
        self.log_path = log_path
        self._proc: subprocess.Popen | None = None
        self._log = None
        self.playback_to: str | None = None

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _write_conf(self, playback_to: str | None, capture_from: str | None) -> None:
        args = "\n".join(f"              {k} = {'true' if v is True else 'false' if v is False else v}"
                         for k, v in self.aec_args.items())
        cap = f' target.object = "{capture_from}"' if capture_from else ""
        play = f' target.object = "{playback_to}"' if playback_to else ""
        conf = f"""# Generado por Vimo (app/echo_cancel.py). Proceso propio: no cambia la configuración del sistema.
context.properties = {{ log.level = 2 }}
context.spa-libs = {{
    audio.convert.* = audioconvert/libspa-audioconvert
    support.*       = support/libspa-support
}}
context.modules = [
    {{ name = libpipewire-module-rt args = {{ }} flags = [ ifexists nofail ] }}
    {{ name = libpipewire-module-protocol-native }}
    {{ name = libpipewire-module-client-node }}
    {{ name = libpipewire-module-adapter }}
    {{ name = libpipewire-module-echo-cancel
      args = {{
          library.name = aec/libspa-aec-webrtc
          monitor.mode = false
          audio.rate = 48000
          audio.channels = 1
          aec.args = {{
{args}
          }}
          capture.props  = {{ node.name = "vimo_aec_capture"{cap} }}
          source.props   = {{ node.name = "{SOURCE}" node.description = "Vimo AEC microfono" priority.session = 1 priority.driver = 1 }}
          sink.props     = {{ node.name = "{SINK}" node.description = "Vimo AEC altavoz" priority.session = 1 priority.driver = 1 }}
          playback.props = {{ node.name = "vimo_aec_playback"{play} }}
      }}
    }}
]
"""
        os.makedirs(os.path.dirname(self.conf_path), exist_ok=True)
        with open(self.conf_path, "w", encoding="utf-8") as fh:
            fh.write(conf)

    def start(self, timeout: float = 10.0) -> str:
        for tool in ("pipewire", "pw-dump", "wpctl"):
            if not shutil.which(tool):
                raise EchoCancelError(f"Falta la herramienta {tool} (PipeWire)")
        self._kill_orphans()
        ensure_alsa_pcms()
        # La salida va al altavoz real que es el predeterminado ahora (nunca al propio filtro).
        playback_to = self.playback_target or _default_node("sink")
        if playback_to in (SINK, "auto_null"):
            # auto_null = no había altavoz al arrancar (p. ej. Bluetooth aún sin conectar): fijarlo
            # mandaría toda la sesión al vacío; sin destino, WirePlumber usa el predeterminado.
            playback_to = None
        self.playback_to = playback_to
        self._write_conf(playback_to, self.capture_target)
        if self.log_path:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            self._log = open(self.log_path, "ab")
        self._proc = subprocess.Popen(["pipewire", "-c", self.conf_path], stdin=subprocess.DEVNULL,
                                      stdout=self._log or subprocess.DEVNULL, stderr=subprocess.STDOUT,
                                      start_new_session=True)
        t0 = time.monotonic()
        try:
            while time.monotonic() - t0 < timeout:
                if self._proc.poll() is not None:
                    raise EchoCancelError("El proceso de cancelación de eco terminó al arrancar (ver logs/aec.log)")
                nodes = _nodes()
                if SINK in nodes and SOURCE in nodes:
                    log.info("Cancelación de eco activa en %.1f s (micrófono %s → %s; %s → %s)",
                             time.monotonic() - t0, self.capture_target or "predeterminado", SOURCE, SINK,
                             playback_to or "predeterminado")
                    return playback_to or "salida predeterminada"
                time.sleep(0.3)
            raise EchoCancelError("Los dispositivos de cancelación de eco no aparecieron a tiempo")
        except BaseException:
            # Cualquier fallo (también uno inesperado de pw-dump) deja el proceso parado: si no,
            # `running` seguiría siendo True y Vimo usaría unos dispositivos que no funcionan.
            self.stop()
            raise

    def _kill_orphans(self) -> None:
        """Un cierre brusco de Vimo puede dejar vivo el proceso anterior: se termina."""
        out = subprocess.run(["pgrep", "-f", f"^pipewire -c {self.conf_path}$"], capture_output=True, text=True)
        for pid in out.stdout.split():
            try:
                os.kill(int(pid), signal.SIGTERM)
                log.info("Cerrado un proceso de cancelación de eco anterior (pid %s)", pid)
            except (ProcessLookupError, ValueError):
                pass

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc and proc.poll() is None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                proc.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                proc.kill()
                try:
                    proc.wait(timeout=2)  # sin esto quedaba un zombi
                except subprocess.TimeoutExpired:
                    pass
        if self._log:
            self._log.close()
            self._log = None
