"""Síntesis de voz local con Piper (piper-tts 1.8, OHF-Voice/piper1-gpl) y adaptación del texto.

prepare_for_speech()
    Convierte la respuesta de Claude Code (que puede traer Markdown, código, tablas, URLs o rutas)
    en texto natural para leer en voz alta. La respuesta original se conserva aparte en la GUI.

split_sentences()
    Divide el texto en segmentos ordenados para sintetizar el primero mientras se reproduce y
    reducir la latencia percibida.

PiperTTS
    Carga la voz una sola vez y genera audio int16 a la frecuencia del modelo
    (voz BMO: 22050 Hz, mono). La reproducción y su interrupción las gestiona AudioPlayer.
"""
from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time
from typing import Iterator

import numpy as np

log = logging.getLogger(__name__)


class TTSError(RuntimeError):
    pass


# ---------------------------------------------------------------------------------------
# Adaptación de texto
# ---------------------------------------------------------------------------------------
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿️‍]")


def prepare_for_speech(text: str, max_chars: int = 900) -> str:
    """Texto apto para TTS. No modifica el original (devuelve una cadena nueva)."""
    t = text or ""
    # Bloques de código: no se leen literalmente.
    t = re.sub(r"```.*?(```|$)", " (te muestro el código en la pantalla) ", t, flags=re.S)
    t = re.sub(r"`([^`]{1,40})`", r"\1", t)  # código en línea corto: se lee su contenido
    t = re.sub(r"`[^`]*`", " ", t)
    # Enlaces Markdown: se lee el texto del enlace, no la URL.
    t = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"\[([^\]]+)\]\((?:https?://|www\.)[^)]*\)", r"\1", t)
    # URLs sueltas: se sustituyen por el dominio.
    t = re.sub(r"https?://(?:www\.)?([^/\s)]+)[^\s)]*", r"\1", t)
    # Rutas de archivo largas: solo el nombre del archivo.
    t = re.sub(r"(?<![\w.])(?:~?/[\w.\-]+){2,}/([\w.\-]+)", r"\1", t)
    # Tablas: se descartan las filas separadoras y las barras.
    t = re.sub(r"^\s*\|?\s*:?-{3,}.*$", " ", t, flags=re.M)
    t = re.sub(r"\s*\|\s*", ", ", t)
    # Encabezados, citas, viñetas y énfasis.
    t = re.sub(r"^\s{0,3}#{1,6}\s*", "", t, flags=re.M)
    t = re.sub(r"^\s*>\s?", "", t, flags=re.M)
    t = re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s+", "", t, flags=re.M)
    t = re.sub(r"(\*\*|__)(.+?)\1", r"\2", t)
    t = re.sub(r"(?<!\w)[*_](.+?)[*_](?!\w)", r"\1", t)
    t = re.sub(r"~~(.+?)~~", r"\1", t)
    t = re.sub(r"<[^>]{1,40}>", " ", t)  # etiquetas HTML sueltas
    t = _EMOJI.sub("", t)
    # Saltos de línea → pausas.
    t = re.sub(r"\n{2,}", ". ", t)
    t = re.sub(r"\n", ", ", t)
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)
    t = re.sub(r"([,.;:])(?:\s*[,.;:])+", r"\1", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" ,;")
    if len(t) > max_chars:
        cut = t[:max_chars]
        end = max(cut.rfind(". "), cut.rfind("? "), cut.rfind("! "))
        t = (cut[: end + 1] if end > max_chars * 0.5 else cut.rsplit(" ", 1)[0]).strip()
        t += " El resto de la respuesta está en la pantalla."
    return t


def split_sentences(text: str, max_len: int = 220) -> list[str]:
    """Segmentos en orden; los largos se parten por comas o espacios."""
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    out: list[str] = []
    for part in parts:
        part = part.strip()
        while len(part) > max_len:
            cut = part.rfind(", ", 0, max_len)
            cut = cut + 1 if cut > max_len // 3 else part.rfind(" ", 0, max_len)
            if cut <= 0:
                cut = max_len
            out.append(part[:cut].strip())
            part = part[cut:].strip()
        if part and re.search(r"\w", part):
            out.append(part)
    return out


# ---------------------------------------------------------------------------------------
# Piper
# ---------------------------------------------------------------------------------------
class PiperTTS:
    def __init__(self, voice_model: str, voice_config: str | None = None, length_scale: float = 1.0,
                 noise_scale: float | None = None, noise_w_scale: float | None = None,
                 speaker_id: int | None = None, volume: float = 1.0, threads: int | None = None):
        self.voice_model = voice_model
        self.voice_config = voice_config or (voice_model + ".json")
        self.params = dict(length_scale=length_scale, noise_scale=noise_scale, noise_w_scale=noise_w_scale,
                           speaker_id=speaker_id, volume=volume)
        self.voice = None
        self.threads = threads      # hilos de onnxruntime; None = todos los núcleos
        self.sample_rate = 22050
        self._lock = threading.Lock()

    def load(self) -> None:
        if not os.path.isfile(self.voice_model):
            raise TTSError(f"No se encuentra la voz de Piper: {self.voice_model}")
        if not os.path.isfile(self.voice_config):
            raise TTSError(f"Falta el archivo de configuración de la voz: {self.voice_config}")
        try:
            from piper import PiperVoice
        except ImportError as exc:
            raise TTSError(f"piper-tts no está instalado o está dañado: {exc}") from exc
        t0 = time.monotonic()
        self.voice = PiperVoice.load(self.voice_model, config_path=self.voice_config)
        if self.threads:
            # piper-tts crea la sesión con todos los núcleos: se sustituye por una limitada para no
            # sumar picos de consumo con Whisper, yt-dlp y mpv (ver docs/diagnostico.md).
            import onnxruntime as ort

            opts = ort.SessionOptions()
            opts.intra_op_num_threads = int(self.threads)
            opts.inter_op_num_threads = 1
            self.voice.session = ort.InferenceSession(self.voice_model, sess_options=opts,
                                                      providers=["CPUExecutionProvider"])
        self.sample_rate = int(self.voice.config.sample_rate)
        log.info("Voz Piper %s cargada en %.1f s (%d Hz)", os.path.basename(self.voice_model),
                 time.monotonic() - t0, self.sample_rate)

    def set_length_scale(self, value: float) -> None:
        self.params["length_scale"] = value

    def synthesize(self, text: str) -> np.ndarray:
        """Sintetiza un segmento completo y devuelve int16 mono a self.sample_rate."""
        if self.voice is None:
            raise TTSError("La voz de Piper no está cargada")
        from piper import SynthesisConfig

        cfg = SynthesisConfig(**self.params)
        with self._lock:
            chunks = [c.audio_int16_bytes for c in self.voice.synthesize(text, cfg)]
        return np.frombuffer(b"".join(chunks), dtype=np.int16)

    def stream(self, segments: list[str], stop: threading.Event, stats: dict | None = None
               ) -> Iterator[tuple[np.ndarray, int]]:
        """Generador para AudioPlayer.play_stream. Un hilo productor sintetiza por adelantado
        (como mucho `ahead` segmentos) mientras se reproduce el anterior; el orden se conserva
        porque hay un único productor y una cola FIFO. Se detiene si `stop` se activa."""
        ahead = 2
        q: queue.Queue = queue.Queue(maxsize=ahead)
        stats = stats if stats is not None else {}
        stats["synth_seconds"] = 0.0
        t_start = time.monotonic()
        closed = threading.Event()  # el consumidor terminó (fin, interrupción o error)

        def halted() -> bool:
            return stop.is_set() or closed.is_set()

        def produce() -> None:
            try:
                for seg in segments:
                    if halted():
                        break
                    t0 = time.monotonic()
                    audio = self.synthesize(seg)
                    stats["synth_seconds"] += time.monotonic() - t0
                    stats.setdefault("first_segment_seconds", time.monotonic() - t_start)
                    while not halted():
                        try:
                            q.put(audio, timeout=0.2)
                            break
                        except queue.Full:
                            continue
            except Exception as exc:  # se propaga al consumidor
                item = exc
            else:
                item = None
            while not halted():
                try:
                    q.put(item, timeout=0.2)
                    return
                except queue.Full:
                    continue

        producer = threading.Thread(target=produce, name="tts-synth", daemon=True)
        producer.start()
        try:
            while True:
                try:
                    item = q.get(timeout=0.2)
                except queue.Empty:
                    if stop.is_set() or (not producer.is_alive() and q.empty()):
                        return
                    continue
                if item is None:
                    return
                if isinstance(item, Exception):
                    raise TTSError(f"Piper falló al sintetizar: {item}") from item
                if len(item):
                    yield item, self.sample_rate
        finally:
            closed.set()
            producer.join(timeout=5)
