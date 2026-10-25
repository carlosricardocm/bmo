"""Entrada y salida de audio para Vimo (sounddevice / PortAudio sobre ALSA o PipeWire).

MicrophoneStream
    Captura continua del micrófono configurado a la frecuencia que el dispositivo admita
    (p. ej. 48 kHz), la convierte a mono 16 kHz int16 y la reparte en bloques fijos
    (1280 muestras = 80 ms) entre los consumidores registrados (wake word, grabación de la
    consulta). La cola entre el callback de PortAudio y el hilo de proceso está limitada: si se
    llena se descartan los bloques más antiguos en lugar de acumular retraso.

AudioPlayer
    Reproducción no bloqueante e interrumpible por el dispositivo de salida, remuestreando a una
    frecuencia admitida y duplicando a estéreo si el dispositivo lo requiere. Notifica el inicio y
    el fin de la reproducción para silenciar la wake word mientras Vimo habla.
"""
from __future__ import annotations

import logging
import math
import queue
import random
import threading
import time
import wave
from typing import Callable, Iterable

import numpy as np

log = logging.getLogger(__name__)

try:
    import sounddevice as sd
except (ImportError, OSError) as exc:  # PortAudio ausente: la GUI sigue funcionando
    sd = None
    _SD_ERROR = exc
else:
    _SD_ERROR = None

TARGET_RATE = 16000


class AudioUnavailable(RuntimeError):
    pass


class RateLimitedLogger:
    """Evita saturar el registro con el mismo error repetido (p. ej. overflows de captura)."""

    def __init__(self, logger: logging.Logger, interval: float = 30.0):
        self.logger = logger
        self.interval = interval
        self._last: dict[str, float] = {}
        self._count: dict[str, int] = {}

    def warning(self, key: str, msg: str, *args) -> None:
        now = time.monotonic()
        self._count[key] = self._count.get(key, 0) + 1
        if now - self._last.get(key, -math.inf) >= self.interval:
            suffix = f" (x{self._count[key]})" if self._count[key] > 1 else ""
            self.logger.warning(msg + suffix, *args)
            self._last[key] = now
            self._count[key] = 0


# ---------------------------------------------------------------------------------------
# Dispositivos
# ---------------------------------------------------------------------------------------
def require_sd() -> None:
    if sd is None:
        raise AudioUnavailable(f"sounddevice/PortAudio no disponible: {_SD_ERROR}")


def refresh_devices() -> None:
    """Vuelve a enumerar los dispositivos. PortAudio hace la lista una sola vez (al importar
    sounddevice), así que no ve los que aparecen después, como los de la cancelación de eco.
    Solo es seguro sin streams abiertos (se usa durante el arranque)."""
    require_sd()
    sd._terminate()
    sd._initialize()


def list_devices(kind: str) -> list[dict]:
    """Dispositivos con canales de entrada ('input') o salida ('output')."""
    require_sd()
    key = "max_input_channels" if kind == "input" else "max_output_channels"
    out = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev[key] > 0:
            out.append({"index": idx, "name": dev["name"], "channels": dev[key],
                        "default_samplerate": dev["default_samplerate"]})
    return out


def find_device(spec, kind: str) -> int | None:
    """Resuelve el dispositivo: índice, subcadena del nombre (sin distinguir mayúsculas) o
    None/'default' para el predeterminado del sistema. Lanza AudioUnavailable si no existe."""
    devices = list_devices(kind)
    if not devices:
        raise AudioUnavailable(f"No hay dispositivos de audio de {'entrada' if kind == 'input' else 'salida'}")
    if spec is None or spec == "default":
        default = sd.default.device[0 if kind == "input" else 1]
        if default is not None and default >= 0 and any(d["index"] == default for d in devices):
            return default
        return devices[0]["index"]
    if isinstance(spec, int):
        if any(d["index"] == spec for d in devices):
            return spec
        raise AudioUnavailable(f"El dispositivo {spec} no existe o no es de {kind}")
    for d in devices:
        if str(spec).lower() in d["name"].lower():
            return d["index"]
    names = ", ".join(d["name"] for d in devices)
    raise AudioUnavailable(f"No se encontró el dispositivo '{spec}'. Disponibles: {names}")


def choose_samplerate(device: int, kind: str, preferred: int | None = None,
                      channels: int = 1) -> int:
    """Primera frecuencia admitida por el dispositivo entre las candidatas."""
    require_sd()
    info = sd.query_devices(device)
    # Entrada: 16 kHz evita remuestrear para la wake word. Salida: frecuencias altas para no
    # degradar la voz de Piper (22050 Hz) ni los efectos.
    order = (16000, 48000, 44100) if kind == "input" else (48000, 44100)
    candidates = [r for r in (preferred, *order, int(info["default_samplerate"]), 32000, 22050) if r]
    check = sd.check_input_settings if kind == "input" else sd.check_output_settings
    for rate in dict.fromkeys(candidates):
        try:
            check(device=device, samplerate=rate, channels=channels, dtype="int16")
            return rate
        except Exception:
            continue
    raise AudioUnavailable(f"El dispositivo {info['name']} no admite ninguna frecuencia probada")


def output_channels(device: int) -> int:
    """1 si el dispositivo acepta mono; si no, 2 (p. ej. el altavoz USB C-Media)."""
    require_sd()
    try:
        sd.check_output_settings(device=device, channels=1, dtype="int16")
        return 1
    except Exception:
        return min(2, int(sd.query_devices(device)["max_output_channels"]))


def system_volume(set_to: float | None = None, delta: float | None = None) -> float:
    """Lee o cambia el volumen general del altavoz predeterminado de PipeWire (0.0–1.0) con
    `wpctl`. Devuelve el nivel resultante. Lanza RuntimeError si wpctl no está disponible."""
    import re
    import subprocess

    sink = "@DEFAULT_AUDIO_SINK@"

    def current() -> float:
        out = subprocess.run(["wpctl", "get-volume", sink], capture_output=True, text=True, timeout=5)
        m = re.search(r"Volume:\s*([\d.]+)", out.stdout)
        if out.returncode != 0 or not m:
            raise RuntimeError("no se pudo leer el volumen del sistema (wpctl)")
        return float(m.group(1))

    try:
        if set_to is None and delta is None:
            return current()
        target = set_to if set_to is not None else current() + delta
        target = max(0.0, min(1.0, target))
        subprocess.run(["wpctl", "set-volume", sink, f"{target:.2f}"], check=True, timeout=5,
                       capture_output=True)
        return current()
    except (OSError, subprocess.SubprocessError) as exc:
        raise RuntimeError(f"no se pudo cambiar el volumen del sistema: {exc}") from exc


# ---------------------------------------------------------------------------------------
# Conversión de señal
# ---------------------------------------------------------------------------------------
def resample(x: np.ndarray, src: int, dst: int) -> np.ndarray:
    """Remuestreo polifásico (antialiasing incluido). Mantiene int16 si la entrada es int16."""
    if src == dst or len(x) == 0:
        return x
    from scipy.signal import resample_poly

    g = math.gcd(int(src), int(dst))
    y = resample_poly(x.astype(np.float32), dst // g, src // g)
    if x.dtype == np.int16:
        return np.clip(np.round(y), -32768, 32767).astype(np.int16)
    return y.astype(x.dtype)


def to_mono(x: np.ndarray) -> np.ndarray:
    if x.ndim == 1:
        return x
    if x.shape[1] == 1:
        return x[:, 0]
    return x.mean(axis=1).astype(x.dtype)


def rms(x: np.ndarray) -> float:
    """Nivel RMS normalizado a [0, 1] para int16."""
    if len(x) == 0:
        return 0.0
    f = x.astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(f * f)))


def read_wav(path: str) -> tuple[np.ndarray, int]:
    """Lee un WAV PCM de 16 bits y lo devuelve en mono int16."""
    with wave.open(path, "rb") as w:
        if w.getsampwidth() != 2:
            raise ValueError(f"{path}: solo se admite PCM de 16 bits")
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
        ch = w.getnchannels()
        if ch > 1:
            data = to_mono(data.reshape(-1, ch))
        return data.copy(), w.getframerate()


def write_wav(path: str, audio: np.ndarray, rate: int) -> None:
    with wave.open(path, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(audio.astype(np.int16).tobytes())


class ChunkBuffer:
    """Reagrupa muestras de tamaño variable en bloques de tamaño fijo."""

    def __init__(self, size: int):
        self.size = size
        self._buf = np.zeros(0, dtype=np.int16)

    def push(self, samples: np.ndarray) -> list[np.ndarray]:
        self._buf = np.concatenate([self._buf, samples])
        n = len(self._buf) // self.size
        chunks = [self._buf[i * self.size:(i + 1) * self.size] for i in range(n)]
        self._buf = self._buf[n * self.size:]
        return chunks

    def clear(self) -> None:
        self._buf = np.zeros(0, dtype=np.int16)


# ---------------------------------------------------------------------------------------
# Micrófono
# ---------------------------------------------------------------------------------------
class MicrophoneStream:
    def __init__(self, device_spec, preferred_rate: int | None = None, chunk_size: int = 1280,
                 queue_max: int = 50):
        self.device_spec = device_spec
        self.preferred_rate = preferred_rate
        self.chunk_size = chunk_size
        self._queue: queue.Queue = queue.Queue(maxsize=queue_max)
        self._sinks: list[Callable[[np.ndarray], None]] = []
        self._sinks_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._rlog = RateLimitedLogger(log)
        self.device: int | None = None
        self.device_name = ""
        self.rate: int | None = None
        self.dropped = 0
        self.error: str | None = None
        self.on_error: Callable[[str], None] | None = None

    # Consumidores ---------------------------------------------------------------------
    def add_sink(self, fn: Callable[[np.ndarray], None]) -> None:
        with self._sinks_lock:
            if fn not in self._sinks:
                self._sinks.append(fn)

    def remove_sink(self, fn: Callable[[np.ndarray], None]) -> None:
        with self._sinks_lock:
            if fn in self._sinks:
                self._sinks.remove(fn)

    # Ciclo de vida ----------------------------------------------------------------------
    def start(self) -> None:
        """Resuelve el dispositivo (lanza AudioUnavailable si no hay) e inicia el hilo."""
        self.device = find_device(self.device_spec, "input")
        self.device_name = sd.query_devices(self.device)["name"]
        self.rate = choose_samplerate(self.device, "input", self.preferred_rate)
        log.info("Micrófono: %s a %d Hz → %d Hz mono", self.device_name, self.rate, TARGET_RATE)
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="mic", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=3)
            self._thread = None

    def _callback(self, indata, frames, time_info, status) -> None:
        if status:
            self._rlog.warning("status", "Captura de audio: %s", status)
        block = indata[:, 0].copy() if indata.ndim > 1 else indata.copy()
        try:
            self._queue.put_nowait(block)
        except queue.Full:
            # Descarta el bloque más antiguo para no acumular latencia.
            try:
                self._queue.get_nowait()
            except queue.Empty:
                pass
            self.dropped += 1
            self._rlog.warning("drop", "Cola de audio llena; se descartan bloques antiguos")
            try:
                self._queue.put_nowait(block)
            except queue.Full:
                pass

    def _run(self) -> None:
        backoff = 1.0
        buffer = ChunkBuffer(self.chunk_size)
        while not self._stop.is_set():
            blocksize = int(self.rate * self.chunk_size / TARGET_RATE)
            try:
                with sd.InputStream(device=self.device, samplerate=self.rate, channels=1,
                                    dtype="int16", blocksize=blocksize, callback=self._callback):
                    self.error = None
                    backoff = 1.0
                    while not self._stop.is_set():
                        try:
                            block = self._queue.get(timeout=0.5)
                        except queue.Empty:
                            continue
                        for chunk in buffer.push(resample(block, self.rate, TARGET_RATE)):
                            with self._sinks_lock:
                                sinks = list(self._sinks)
                            for sink in sinks:
                                try:
                                    sink(chunk)
                                except Exception:
                                    self._rlog.warning("sink", "Error en consumidor de audio")
                                    log.debug("Detalle", exc_info=True)
            except Exception as exc:
                self.error = f"Error del micrófono: {exc}"
                self._rlog.warning("stream", "%s; reintento en %.0f s", self.error, backoff)
                if self.on_error:
                    self.on_error(self.error)
                buffer.clear()
                if self._stop.wait(backoff):
                    break
                backoff = min(backoff * 2, 30.0)
                # El dispositivo pudo cambiar de índice (desconexión/reconexión).
                try:
                    self.device = find_device(self.device_spec, "input")
                    self.rate = choose_samplerate(self.device, "input", self.preferred_rate)
                except AudioUnavailable as exc2:
                    self._rlog.warning("find", "%s", exc2)


# ---------------------------------------------------------------------------------------
# Reproducción
# ---------------------------------------------------------------------------------------
class AudioPlayer:
    """Reproduce audio int16 mono por el dispositivo configurado; una sola reproducción a la vez."""

    def __init__(self, device_spec, preferred_rate: int | None = None):
        self.device_spec = device_spec
        self.preferred_rate = preferred_rate
        self.device: int | None = None
        self.rate: int | None = None
        self.channels = 2
        self._lock = threading.Lock()          # serializa reproducciones
        self._stop = threading.Event()
        self._playing = threading.Event()
        self._thread: threading.Thread | None = None
        self.on_start: Callable[[], None] | None = None
        self.on_end: Callable[[], None] | None = None

    def open(self) -> None:
        self.device = find_device(self.device_spec, "output")
        self.channels = output_channels(self.device)
        self.rate = choose_samplerate(self.device, "output", self.preferred_rate, self.channels)
        log.info("Salida de audio: %s a %d Hz, %d canal(es)",
                 sd.query_devices(self.device)["name"], self.rate, self.channels)

    @property
    def playing(self) -> bool:
        return self._playing.is_set()

    def stop(self, wait: bool = True) -> None:
        """Interrumpe la reproducción en curso (si la hay)."""
        self._stop.set()
        t = self._thread
        if wait and t and t is not threading.current_thread():
            t.join(timeout=3)

    def play(self, audio: np.ndarray, rate: int, volume: float = 1.0, block: bool = False) -> None:
        self.play_stream([(audio, rate)], volume=volume, block=block)

    def play_stream(self, chunks: Iterable[tuple[np.ndarray, int]], volume: float = 1.0,
                    block: bool = False, on_done: Callable[[bool], None] | None = None) -> None:
        """Reproduce en orden los bloques (audio, frecuencia) que produce `chunks` (puede ser un
        generador que sintetiza mientras se reproduce). `on_done(interrumpido)` al terminar."""
        if self.device is None:
            self.open()
        self.stop(wait=True)
        self._stop.clear()
        self._playing.set()
        t = threading.Thread(target=self._run, args=(chunks, volume, on_done), name="player", daemon=True)
        self._thread = t
        t.start()
        if block:
            t.join()

    def _run(self, chunks, volume, on_done) -> None:
        interrupted = False
        with self._lock:
            if self.on_start:
                self.on_start()
            try:
                with sd.OutputStream(device=self.device, samplerate=self.rate, channels=self.channels,
                                     dtype="int16") as stream:
                    step = max(1, self.rate // 20)  # escribe en bloques de 50 ms para poder parar
                    for audio, rate in chunks:
                        if self._stop.is_set():
                            interrupted = True
                            break
                        data = resample(np.asarray(audio, dtype=np.int16), rate, self.rate)
                        if volume != 1.0:
                            data = np.clip(data.astype(np.float32) * volume, -32768, 32767).astype(np.int16)
                        frames = data.reshape(-1, 1)
                        if self.channels > 1:
                            frames = np.repeat(frames, self.channels, axis=1)
                        for i in range(0, len(frames), step):
                            if self._stop.is_set():
                                interrupted = True
                                break
                            stream.write(np.ascontiguousarray(frames[i:i + step]))
                        if interrupted:
                            break
                    if interrupted:
                        stream.abort()
            except Exception:
                log.exception("Error al reproducir audio")
                interrupted = True
            finally:
                close = getattr(chunks, "close", None)
                if close:  # generador de síntesis interrumpido: liberar sus recursos
                    close()
                self._playing.clear()
                if self.on_end:
                    self.on_end()
                if on_done:
                    on_done(interrupted)


# ---------------------------------------------------------------------------------------
# Efectos de sonido originales (sounds/<categoría>/*.wav)
# ---------------------------------------------------------------------------------------
class SoundBank:
    CATEGORIES = ("greeting_sounds", "ack_sounds", "thinking_sounds", "error_sounds")

    def __init__(self, directory: str):
        import os

        self.sounds: dict[str, list[tuple[str, np.ndarray, int]]] = {}
        for cat in self.CATEGORIES:
            folder = os.path.join(directory, cat)
            if not os.path.isdir(folder):
                continue
            items = []
            for name in sorted(os.listdir(folder)):
                if name.lower().endswith(".wav"):
                    path = os.path.join(folder, name)
                    try:
                        audio, rate = read_wav(path)
                        items.append((name, audio, rate))
                    except (OSError, ValueError, wave.Error) as exc:
                        log.warning("Sonido no válido %s: %s", path, exc)
            if items:
                self.sounds[cat] = items
        log.info("Sonidos cargados de %s: %s", directory, {k: len(v) for k, v in self.sounds.items()})

    def pick(self, category: str) -> tuple[str, np.ndarray, int] | None:
        items = self.sounds.get(category)
        return random.choice(items) if items else None
