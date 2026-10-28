"""Captura de la consulta tras la activación y transcripción local con Whisper.cpp.

La wake word (livekit-wakeword) solo activa a Vimo; lo que el usuario dice después se graba aquí y se transcribe
con Whisper.cpp.

UtteranceRecorder
    Detecta el final de la intervención con un VAD de energía: umbral = máx(energy_threshold,
    ruido_ambiente × noise_multiplier). El ruido ambiente se estima de forma continua con
    NoiseTracker mientras Vimo espera. Termina tras `silence_seconds` de silencio, al alcanzar
    `max_seconds`, o si nadie habla en `no_speech_timeout_seconds`. Las grabaciones con menos de
    `min_speech_seconds` de voz no se envían a Whisper.

WhisperTranscriber
    Usa `whisper-server` (modelo cargado una vez, petición HTTP local por consulta) y, si no
    puede arrancarlo, `whisper-cli` por consulta. Nada sale de la Raspberry Pi.
"""
from __future__ import annotations

import io
import json
import logging
import os
import queue
import re
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import wave
from collections import deque
from dataclasses import dataclass
from typing import Callable

import numpy as np

from .audio_manager import TARGET_RATE, rms

log = logging.getLogger(__name__)

CHUNK_SECONDS = 1280 / TARGET_RATE


class STTError(RuntimeError):
    pass


# ---------------------------------------------------------------------------------------
# VAD / grabación de la consulta
# ---------------------------------------------------------------------------------------
class NoiseTracker:
    """Media exponencial del nivel de los bloques sin voz (sink permanente del micrófono)."""

    def __init__(self, initial: float = 0.003, alpha: float = 0.05):
        self.level = initial
        self.alpha = alpha
        self.paused = False

    def __call__(self, chunk: np.ndarray) -> None:
        if self.paused:
            return
        level = rms(chunk)
        # Solo actualiza con bloques cercanos al ruido actual (ignora picos de voz).
        if level < self.level * 3 or level < 0.002:
            self.level = (1 - self.alpha) * self.level + self.alpha * level


@dataclass
class Utterance:
    audio: np.ndarray
    speech_seconds: float
    duration: float
    reason: str  # "silence" | "max_duration" | "no_speech" | "cancelled"

    @property
    def has_speech(self) -> bool:
        return self.reason not in ("no_speech", "cancelled") and len(self.audio) > 0


class EndpointDetector:
    """Lógica pura del VAD (sin E/S) para poder probarla con audio sintético."""

    def __init__(self, threshold: float, silence_seconds: float, max_seconds: float,
                 no_speech_timeout: float, preroll_seconds: float = 0.3):
        self.threshold = threshold
        self.silence_seconds = silence_seconds
        self.max_seconds = max_seconds
        self.no_speech_timeout = no_speech_timeout
        self.preroll = max(1, int(preroll_seconds / CHUNK_SECONDS))
        self.chunks: list[np.ndarray] = []
        self.started = False
        self.start_index = 0
        self.speech_chunks = 0
        self.loud_run = 0
        self.silence = 0.0
        self.elapsed = 0.0
        self.reason: str | None = None

    def feed(self, chunk: np.ndarray) -> bool:
        """Añade un bloque. Devuelve True cuando la intervención ha terminado."""
        self.chunks.append(chunk)
        self.elapsed += CHUNK_SECONDS
        loud = rms(chunk) >= self.threshold
        if loud:
            self.loud_run += 1
            self.speech_chunks += 1
            self.silence = 0.0
            if not self.started and self.loud_run >= 2:  # ≥160 ms seguidos de voz
                self.started = True
                self.start_index = max(0, len(self.chunks) - self.loud_run - self.preroll)
        else:
            self.loud_run = 0
            if self.started:
                self.silence += CHUNK_SECONDS
        if self.started and self.silence >= self.silence_seconds:
            self.reason = "silence"
        elif self.elapsed >= self.max_seconds:
            self.reason = "max_duration" if self.started else "no_speech"
        elif not self.started and self.elapsed >= self.no_speech_timeout:
            self.reason = "no_speech"
        return self.reason is not None

    def result(self) -> Utterance:
        if not self.started:
            audio = np.zeros(0, dtype=np.int16)
        else:
            audio = np.concatenate(self.chunks[self.start_index:])
        return Utterance(audio, self.speech_chunks * CHUNK_SECONDS, self.elapsed, self.reason or "cancelled")


class MicHistory:
    """Sink del micrófono siempre activo con los últimos segundos de audio. Tras «Hey Vimo», el
    pitido de confirmación y el arranque del turno tardan ~1–2 s; quien sigue hablando de corrido
    («Hey Vimo, reproduce…») perdía el principio de la orden («…produce»). `attach()` entrega el
    audio desde la detección y, sin huecos ni duplicados, los bloques siguientes."""

    def __init__(self, seconds: float = 3.0):
        self._buf: deque = deque(maxlen=max(1, int(seconds / CHUNK_SECONDS)))
        self._lock = threading.Lock()
        self._listener: Callable[[np.ndarray], None] | None = None
        # Sin cancelación de eco, lo grabado mientras Vimo suena (el pitido) se descarta: si no, el
        # pitido contaría como voz y la consulta de quien espera al pitido se cortaría antes de hablar.
        self.playing = False
        self.keep_while_playing = True

    def __call__(self, chunk: np.ndarray) -> None:
        with self._lock:
            if not self.playing or self.keep_while_playing:
                self._buf.append((time.monotonic(), chunk))
            listener = self._listener
        if listener:
            listener(chunk)

    def attach(self, since: float, listener: Callable[[np.ndarray], None]) -> list[np.ndarray]:
        with self._lock:
            past = [c for t, c in self._buf if t > since]
            self._listener = listener
        return past

    def detach(self) -> None:
        with self._lock:
            self._listener = None


class UtteranceRecorder:
    def __init__(self, mic, noise: NoiseTracker, cfg: dict):
        self.mic = mic
        self.noise = noise
        self.cfg = cfg

    def record(self, cancel: threading.Event, history: MicHistory | None = None,
               since: float | None = None) -> Utterance:
        """Graba una consulta. Con `history` y `since` (instante de la wake word), empieza por el
        audio captado desde entonces."""
        threshold = max(self.cfg["energy_threshold"], self.noise.level * self.cfg["noise_multiplier"])
        det = EndpointDetector(threshold, self.cfg["silence_seconds"], self.cfg["max_seconds"],
                               self.cfg["no_speech_timeout_seconds"])
        q: queue.Queue = queue.Queue(maxsize=400)
        sink = lambda c: q.put_nowait(c) if not q.full() else None  # noqa: E731
        self.noise.paused = True
        use_history = history is not None and since is not None
        past = history.attach(since, sink) if use_history else []
        if not use_history:
            self.mic.add_sink(sink)
        if past:
            log.debug("Consulta: %.1f s de audio previo (desde la wake word)", len(past) * CHUNK_SECONDS)
        t0 = time.monotonic()
        try:
            done = False
            for chunk in past:
                if det.feed(chunk):
                    done = True
                    break
            while not done and not cancel.is_set():
                try:
                    chunk = q.get(timeout=0.5)
                except queue.Empty:
                    if time.monotonic() - t0 > self.cfg["max_seconds"] + 2:
                        raise STTError("El micrófono no entrega audio")
                    continue
                if det.feed(chunk):
                    break
        finally:
            if use_history:
                history.detach()
            else:
                self.mic.remove_sink(sink)
            self.noise.paused = False
        utt = det.result()
        if cancel.is_set():
            utt.reason = "cancelled"
        if utt.has_speech and utt.speech_seconds < self.cfg["min_speech_seconds"]:
            utt.reason = "no_speech"
        log.info("Consulta grabada: %.1f s (voz %.1f s, fin=%s, umbral %.4f)", utt.duration,
                 utt.speech_seconds, utt.reason, threshold)
        return utt


# ---------------------------------------------------------------------------------------
# Limpieza de transcripciones
# ---------------------------------------------------------------------------------------
# Frases que Whisper suele "alucinar" con silencio o ruido (subtítulos de su entrenamiento).
HALLUCINATIONS = (
    "subtítulos realizados por la comunidad de amara.org",
    "subtítulos por la comunidad de amara.org",
    "gracias por ver el video",
    "gracias por ver",
    "suscríbete",
    "thank you for watching",
    "¡gracias!",
)


def clean_transcript(text: str) -> str:
    text = re.sub(r"\[[^\]]*\]|\([^)]*\)|\*[^*]*\*", " ", text)  # [Música], (risas), *tos*
    text = re.sub(r"\s+", " ", text).strip()
    low = text.lower().strip(" .!¡?¿")
    if any(low == h.strip(" .!¡?¿") for h in HALLUCINATIONS):
        return ""
    if not re.search(r"\w", text):
        return ""
    return text


def wav_bytes(audio: np.ndarray, rate: int = TARGET_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(audio.astype(np.int16).tobytes())
    return buf.getvalue()


# ---------------------------------------------------------------------------------------
# Whisper.cpp
# ---------------------------------------------------------------------------------------
class WhisperTranscriber:
    def __init__(self, whisper_dir: str, server_binary: str, cli_binary: str, model: str,
                 language: str = "es", threads: int = 4, use_server: bool = True, port: int = 8178,
                 timeout: float = 60.0, log_dir: str | None = None, audio_ctx: int = 0, prompt: str = ""):
        self.whisper_dir = whisper_dir
        self.server_bin = self._abs(server_binary)
        self.cli_bin = self._abs(cli_binary)
        self.model = self._abs(model)
        self.language = language
        self.threads = threads
        self.use_server = use_server
        self.port = port
        self.timeout = timeout
        self.log_dir = log_dir
        # Contexto de audio de Whisper (-ac): 1500 = ventana completa de 30 s. Las consultas duran como
        # máximo listening.max_seconds (12 s), así que 768 (~15 s) basta y el codificador tarda menos
        # de la mitad, con el mismo texto (medido en esta Raspberry Pi el 25-10-2026). 0 = completo.
        self.audio_ctx = int(audio_ctx or 0)
        # Vocabulario previo (stt.prompt): orienta a Whisper hacia las órdenes de Vimo.
        self.prompt = prompt or ""
        self._server: subprocess.Popen | None = None
        self._server_log = None
        self._lock = threading.Lock()

    def _abs(self, p: str) -> str:
        p = os.path.expanduser(p)
        return p if os.path.isabs(p) else os.path.join(self.whisper_dir, p)

    def check(self) -> None:
        if not os.path.isfile(self.model):
            raise STTError(f"No se encuentra el modelo de Whisper: {self.model}")
        if not (os.access(self.cli_bin, os.X_OK) or os.access(self.server_bin, os.X_OK)):
            raise STTError(f"No se encuentra whisper.cpp compilado en {self.whisper_dir}")

    # Servidor ---------------------------------------------------------------------------
    def start(self) -> None:
        self.check()
        if not self.use_server or not os.access(self.server_bin, os.X_OK):
            log.info("Whisper.cpp en modo CLI (%s)", os.path.basename(self.model))
            return
        if self._port_busy():
            self._kill_orphans()
        if self._port_busy():
            # Otro programa usa el puerto: Vimo sigue funcionando con whisper-cli.
            log.error("El puerto %d está en uso por otro programa; se usará whisper-cli (cambia stt.server_port)",
                      self.port)
            return
        self._launch_server()

    def _common_args(self) -> list[str]:
        args = ["-m", self.model, "-l", self.language, "-t", str(self.threads)]
        return args + (["-ac", str(self.audio_ctx)] if self.audio_ctx else [])

    def _kill_orphans(self) -> None:
        """Un cierre brusco de Vimo deja vivo el whisper-server anterior (sesión propia) ocupando el
        puerto; antes eso desactivaba el reconocimiento de voz hasta reiniciar la Raspberry Pi."""
        out = subprocess.run(["pgrep", "-u", str(os.getuid()), "-f", f"^{self.server_bin} .*--port {self.port}( |$)"],
                             capture_output=True, text=True)
        for pid in out.stdout.split():
            try:
                os.kill(int(pid), signal.SIGTERM)
                log.info("Cerrado un whisper-server anterior (pid %s)", pid)
            except (ProcessLookupError, ValueError):
                pass
        t0 = time.monotonic()
        while self._port_busy() and time.monotonic() - t0 < 5:
            time.sleep(0.2)

    def _launch_server(self) -> None:
        cmd = [self.server_bin] + self._common_args() + ["--host", "127.0.0.1", "--port", str(self.port), "-nt"]
        if self.log_dir and self._server_log is None:
            os.makedirs(self.log_dir, exist_ok=True)
            self._server_log = open(os.path.join(self.log_dir, "whisper-server.log"), "ab")
        t0 = time.monotonic()
        self._server = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=self._server_log or subprocess.DEVNULL,
                                        stderr=subprocess.STDOUT, start_new_session=True)
        while time.monotonic() - t0 < 60:
            if self._server.poll() is not None:
                self._server = None
                log.error("whisper-server terminó al arrancar; se usará whisper-cli")
                return
            if self._port_busy():
                log.info("whisper-server listo en %.1f s (%s)", time.monotonic() - t0, os.path.basename(self.model))
                return
            time.sleep(0.3)
        log.error("whisper-server no respondió en 60 s; se usará whisper-cli")
        self.stop()

    def _port_busy(self) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            return s.connect_ex(("127.0.0.1", self.port)) == 0

    def stop(self) -> None:
        proc, self._server = self._server, None
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        if self._server_log:
            self._server_log.close()
            self._server_log = None

    # Transcripción ----------------------------------------------------------------------
    def transcribe(self, audio: np.ndarray, rate: int = TARGET_RATE) -> str:
        if len(audio) < rate * 0.3:
            return ""
        with self._lock:
            t0 = time.monotonic()
            if self._server and self._server.poll() is not None:
                log.warning("whisper-server terminó inesperadamente (código %s); se relanza", self._server.returncode)
                self._server = None
                self._launch_server()
            if self._server and self._server.poll() is None:
                try:
                    text = self._via_server(wav_bytes(audio, rate))
                except TimeoutError as exc:
                    # El servidor sigue procesando: lanzar además whisper-cli duplicaría la carga (dos
                    # Whisper de varios hilos a la vez, picos de consumo) para llegar igual de tarde.
                    raise STTError(f"Whisper tardó más de {self.timeout:.0f} s") from exc
                except (OSError, urllib.error.URLError, ValueError) as exc:
                    log.warning("whisper-server falló (%s); se usa whisper-cli", exc)
                    text = self._via_cli(audio, rate)
            else:
                text = self._via_cli(audio, rate)
            cleaned = clean_transcript(text)
            log.info("Transcripción en %.2f s (%.1f s de audio): %r", time.monotonic() - t0,
                     len(audio) / rate, cleaned)
            return cleaned

    def _via_server(self, wav: bytes) -> str:
        boundary = uuid.uuid4().hex
        fields = {"response_format": "json", "temperature": "0.0", "language": self.language}
        if self.prompt:
            fields["prompt"] = self.prompt
        body = io.BytesIO()
        for k, v in fields.items():
            body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
        body.write(f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"q.wav\"\r\n"
                   "Content-Type: audio/wav\r\n\r\n".encode())
        body.write(wav)
        body.write(f"\r\n--{boundary}--\r\n".encode())
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/inference", data=body.getvalue(),
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}"})
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
        if "error" in data:
            raise ValueError(data["error"])
        return data.get("text", "")

    def _via_cli(self, audio: np.ndarray, rate: int) -> str:
        if not os.access(self.cli_bin, os.X_OK):
            raise STTError(f"No se encuentra whisper-cli en {self.cli_bin}")
        fd, path = tempfile.mkstemp(prefix="vimo-", suffix=".wav")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(wav_bytes(audio, rate))
            cmd = [self.cli_bin] + self._common_args() + ["-nt", "-np", "-f", path]
            if self.prompt:
                cmd += ["--prompt", self.prompt]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
            except subprocess.TimeoutExpired as exc:
                raise STTError(f"Whisper tardó más de {self.timeout:.0f} s; prueba un modelo más ligero") from exc
            if proc.returncode != 0:
                raise STTError(f"whisper-cli falló ({proc.returncode}): {proc.stderr.strip()[-300:]}")
            return proc.stdout
        finally:
            os.unlink(path)
