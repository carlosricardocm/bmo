"""Detección local de la wake word personalizada con livekit-wakeword (ONNX, CPU).

El modelo (`models/hey_vimo_*.onnx`) se entrenó con livekit-wakeword, así que se ejecuta con su
mismo preprocesado: audio normalizado a ±1 y espectrograma mel calculado sobre ventanas de 2 s.
(openWakeWord, que se usaba antes, pasaba audio en escala int16 y calculaba el mel en trozos de
~0,1 s; el mel lleva una normalización respecto al máximo de la entrada, así que las
características no coincidían con las del entrenamiento y el modelo puntuaba más bajo).

`WakeWordModel.predict()` de livekit no guarda estado: recalcula los 16 embeddings de la ventana
en cada llamada (~86 ms por bloque de 80 ms en la Raspberry Pi 5 con un hilo). `LiveKitModel` hace
lo mismo solo cuando hace falta:
- en cada bloque calcula el mel de los últimos 2 s y un único embedding nuevo, y puntúa con los
  16 últimos guardados (aproximación, ~11 ms);
- si esa puntuación llega a `exact_gate`, recalcula los 16 embeddings con el mel actual, que es
  exactamente lo que hace livekit (comprobado: puntuaciones idénticas en las grabaciones de
  data/eval_wake). Ocurre en <1 % de los bloques con ruido normal.

Si el modelo no existe o no carga, el detector queda desactivado con un mensaje claro y el
usuario puede usar «pulsar para hablar». No hay motor sustituto.

El detector consume los bloques de 1280 muestras (80 ms a 16 kHz) que entrega MicrophoneStream.
- `suspend()/resume()` lo silencian mientras Vimo reproduce audio (evita autoactivaciones).
- Tras cada detección se aplica `cooldown_seconds` y se reinician los búferes del modelo.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from typing import Callable

import numpy as np

log = logging.getLogger(__name__)

EXPECTED_INPUT = [1, 16, 96]  # 16 embeddings de 96 dimensiones (clasificadores de livekit-wakeword)
SAMPLE_RATE = 16000
CHUNK_SAMPLES = 1280
CHUNK_SECONDS = CHUNK_SAMPLES / SAMPLE_RATE  # 80 ms
WINDOW_SAMPLES = 2 * SAMPLE_RATE             # ventana de 2 s, la que usa livekit
EMB_WINDOW = 76                              # fotogramas mel por embedding
EMB_STRIDE = 8                               # fotogramas mel entre embeddings (= 80 ms)
N_EMBEDDINGS = 16


class WakeWordError(RuntimeError):
    pass


def _session_options(threads: int):
    """Opciones de onnxruntime con KleidiAI desactivado (lo hace livekit: en arm64 algunas
    versiones de onnxruntime dan resultados erróneos en convoluciones de lote 1)."""
    import onnxruntime as ort

    opts = ort.SessionOptions()
    opts.intra_op_num_threads = max(1, int(threads))
    opts.inter_op_num_threads = 1
    try:
        from livekit.wakeword.session import session_options
        return session_options(opts)
    except ImportError:
        opts.add_session_config_entry("mlas.disable_kleidiai", "1")
        return opts


def validate_model(path: str) -> None:
    """Comprueba que el ONNX tiene la entrada de un clasificador de livekit-wakeword."""
    if not os.path.isfile(path):
        raise WakeWordError(f"No se encuentra el modelo de wake word: {path}")
    try:
        import onnxruntime as ort
    except ImportError as exc:
        raise WakeWordError(f"onnxruntime no está instalado: {exc}") from exc
    try:
        sess = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
    except Exception as exc:
        raise WakeWordError(f"El modelo {path} no es un ONNX válido: {exc}") from exc
    inputs = sess.get_inputs()
    shape = list(inputs[0].shape) if inputs else []
    if len(inputs) != 1 or shape[1:] != EXPECTED_INPUT[1:]:
        raise WakeWordError(
            f"El modelo {os.path.basename(path)} tiene entrada {shape}; se espera {EXPECTED_INPUT}. "
            "¿Es un modelo entrenado con livekit-wakeword?"
        )


class LiveKitModel:
    """Pipeline de livekit-wakeword (mel → embeddings → clasificador) para audio en streaming.

    Misma interfaz que `livekit.wakeword.WakeWordModel`: `predict(chunk)` devuelve
    {nombre: puntuación}, pero recibe bloques de 80 ms y guarda la ventana de 2 s internamente.
    """

    def __init__(self, model_path: str, melspec_path: str | None = None, embedding_path: str | None = None,
                 exact_gate: float = 0.1, threads: int = 1):
        try:
            import onnxruntime as ort
            from livekit.wakeword.models.feature_extractor import MelSpectrogramFrontend, SpeechEmbedding
            from livekit.wakeword.resources import get_embedding_model_path, get_mel_model_path
        except ImportError as exc:
            raise WakeWordError(f"livekit-wakeword no está instalado: {exc}") from exc
        # Los auxiliares de config.json son los mismos que trae el paquete (mismo md5); si faltan,
        # se usan los del paquete.
        mel = melspec_path if melspec_path and os.path.isfile(melspec_path) else get_mel_model_path()
        emb = embedding_path if embedding_path and os.path.isfile(embedding_path) else get_embedding_model_path()
        self._mel = MelSpectrogramFrontend(onnx_path=mel, sess_options=_session_options(threads))
        self._emb = SpeechEmbedding(onnx_path=emb, sess_options=_session_options(threads))
        self._clf = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"],
                                         sess_options=_session_options(threads))
        self._clf_input = self._clf.get_inputs()[0].name
        self.name = os.path.splitext(os.path.basename(model_path))[0]
        self.models = {self.name: self._clf}  # mismo atributo que openWakeWord/livekit
        self.exact_gate = exact_gate
        self.exact_runs = 0
        # Embedding de una ventana de silencio: rellena el historial tras reset() para puntuar
        # desde el primer bloque (la puntuación exacta usa igualmente el mel real de la ventana).
        silence_mel = self._mel(np.zeros(WINDOW_SAMPLES, np.float32))[0]
        self._silence_emb = self._emb(silence_mel[None, :EMB_WINDOW])[0]
        self.reset()

    def reset(self) -> None:
        self._ring = np.zeros(WINDOW_SAMPLES, np.float32)
        self._embs: deque = deque([self._silence_emb] * N_EMBEDDINGS, maxlen=N_EMBEDDINGS)

    def _classify(self, embeddings: np.ndarray) -> float:
        out = self._clf.run(None, {self._clf_input: embeddings[None].astype(np.float32)})
        return float(out[0][0, 0])

    def predict(self, chunk: np.ndarray) -> dict[str, float]:
        x = chunk.astype(np.float32) / 32768.0 if chunk.dtype == np.int16 else chunk.astype(np.float32)
        x = x.reshape(-1)[-WINDOW_SAMPLES:]
        self._ring = np.concatenate((self._ring[len(x):], x))
        mel = self._mel(self._ring)[0]
        # Ventanas alineadas desde el principio de la ventana, igual que WakeWordModel.predict().
        last = (mel.shape[0] - EMB_WINDOW) // EMB_STRIDE * EMB_STRIDE
        self._embs.append(self._emb(mel[None, last:last + EMB_WINDOW])[0])
        score = self._classify(np.stack(self._embs))
        if score >= self.exact_gate:
            starts = range(last - (N_EMBEDDINGS - 1) * EMB_STRIDE, last + 1, EMB_STRIDE)
            exact = self._emb(np.stack([mel[s:s + EMB_WINDOW] for s in starts]))
            score = self._classify(exact)
            self.exact_runs += 1
        return {self.name: score}


class SileroVAD:
    """Filtro de voz Silero (modelo silero_vad.onnx con estados h/c). Guarda en
    `prediction_buffer` la probabilidad de voz de cada bloque de 80 ms (últimos 10 s)."""

    def __init__(self, model_path: str, threads: int = 1):
        import onnxruntime as ort

        self.model = ort.InferenceSession(model_path, sess_options=_session_options(threads),
                                          providers=["CPUExecutionProvider"])
        self.prediction_buffer: deque = deque(maxlen=125)
        self._sr = np.array(SAMPLE_RATE, dtype=np.int64)
        self.reset_states()

    def reset_states(self) -> None:
        self._h = np.zeros((2, 1, 64), np.float32)
        self._c = np.zeros((2, 1, 64), np.float32)

    def predict(self, x: np.ndarray, frame_size: int = 640) -> float:
        probs = []
        for i in range(0, len(x), frame_size):
            frame = (x[i:i + frame_size] / 32767).astype(np.float32)
            out, self._h, self._c = self.model.run(
                None, {"input": frame[None], "h": self._h, "c": self._c, "sr": self._sr})
            probs.append(float(out[0][0]))
        return float(np.mean(probs)) if probs else 0.0

    def __call__(self, x: np.ndarray) -> None:
        self.prediction_buffer.append(self.predict(x))


class TriggerLogic:
    """Decide cuándo una secuencia de puntuaciones es una activación. La usan el detector en vivo y
    la herramienta de evaluación (tools/evaluar_wake_word.py), así ambas deciden exactamente igual.

    - threshold: puntuación mínima del modelo.
    - patience: bloques seguidos (de 80 ms) por encima del umbral antes de activar. 1 = inmediato.
    - vad_threshold: si > 0, solo activa si hubo voz humana (Silero VAD) entre 0,4 y 0,56 s antes
      (la frase ya terminó cuando el modelo la reconoce).
    - cooldown: segundos mínimos entre activaciones.
    """

    def __init__(self, threshold: float, patience: int = 1, vad_threshold: float = 0.0,
                 cooldown: float = 2.0):
        self.threshold = threshold
        self.patience = max(1, int(patience))
        self.vad_threshold = vad_threshold
        self.cooldown = cooldown
        self._streak = 0
        self._last = -1e9
        self.vad_rejected = 0

    def reset(self) -> None:
        self._streak = 0

    def feed(self, score: float, now: float, vad_history: list[float] | None = None) -> bool:
        if score < self.threshold:
            self._streak = 0
            return False
        self._streak += 1
        if self._streak < self.patience:
            return False
        if self.vad_threshold > 0:
            window = (vad_history or [])[-7:-4]
            if not window or max(window) < self.vad_threshold:
                self.vad_rejected += 1
                return False
        if now - self._last < self.cooldown:
            return False
        self._last = now
        self._streak = 0
        return True


class WakeWordDetector:
    def __init__(self, model_path: str, melspec_path: str | None = None, embedding_path: str | None = None,
                 threshold: float = 0.3, cooldown_seconds: float = 2.0, resume_delay_seconds: float = 0.6,
                 patience_frames: int = 1, vad_model_path: str | None = None, vad_threshold: float = 0.0,
                 partial_threshold: float | None = None, exact_gate: float = 0.1, threads: int = 1):
        self.model_path = model_path
        self.melspec_path = melspec_path
        self.embedding_path = embedding_path
        self.resume_delay_seconds = resume_delay_seconds
        self.vad_model_path = vad_model_path
        self.partial_threshold = partial_threshold
        self.exact_gate = exact_gate
        self.threads = threads
        self.logic = TriggerLogic(threshold, patience_frames, vad_threshold, cooldown_seconds)
        self.model = None
        self.vad = None
        self.name = os.path.splitext(os.path.basename(model_path))[0]
        self.on_detect: Callable[[float], None] | None = None
        # Aviso temprano: la puntuación empieza a subir («Hey…»). Sirve para bajar la música.
        self.on_partial: Callable[[float], None] | None = None
        self._last_partial = -1e9
        self.enabled = True
        self._suspend_count = 0
        self._resume_at = 0.0
        self._lock = threading.Lock()
        self.last_score = 0.0
        self.stats = {"chunks": 0, "total_ms": 0.0, "detections": 0, "vad_rejected": 0}

    # Compatibilidad: el umbral se ajusta en caliente desde Ajustes.
    @property
    def threshold(self) -> float:
        return self.logic.threshold

    @threshold.setter
    def threshold(self, value: float) -> None:
        self.logic.threshold = value

    @property
    def cooldown_seconds(self) -> float:
        return self.logic.cooldown

    def load(self) -> None:
        validate_model(self.model_path)
        t0 = time.monotonic()
        # La aproximación nunca debe quedarse por debajo de lo que hay que detectar o avisar.
        gate = min(g for g in (self.exact_gate, self.logic.threshold * 0.5, self.partial_threshold) if g)
        self.model = LiveKitModel(self.model_path, self.melspec_path, self.embedding_path, gate, self.threads)
        self.name = self.model.name
        if self.logic.vad_threshold > 0:
            if not self.vad_model_path or not os.path.isfile(self.vad_model_path):
                # Sin VAD la wake word sigue funcionando (solo se pierde el filtro): mejor que nada.
                log.warning("Falta el modelo Silero VAD (%s); la wake word funciona sin filtro de voz. "
                            "Descárgalo con ./setup.sh --download-models", self.vad_model_path)
                self.logic.vad_threshold = 0.0
            else:
                self.vad = SileroVAD(self.vad_model_path)
        log.info("Wake word '%s' (livekit-wakeword) cargada en %.2f s (umbral %.2f, %d bloque(s) seguidos, "
                 "VAD %s)", self.name, time.monotonic() - t0, self.logic.threshold, self.logic.patience,
                 f"{self.logic.vad_threshold:.2f}" if self.vad else "no")

    # Control --------------------------------------------------------------------------
    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self.enabled = enabled
            self._reset_buffers()

    def suspend(self) -> None:
        with self._lock:
            self._suspend_count += 1

    def resume(self) -> None:
        with self._lock:
            self._suspend_count = max(0, self._suspend_count - 1)
            if self._suspend_count == 0:
                self._resume_at = time.monotonic() + self.resume_delay_seconds
                self._reset_buffers()

    @property
    def active(self) -> bool:
        return (self.model is not None and self.enabled and self._suspend_count == 0
                and time.monotonic() >= self._resume_at)

    def _reset_buffers(self) -> None:
        self.logic.reset()
        if self.model is not None:
            self.model.reset()

    # Proceso de audio (hilo del micrófono) ---------------------------------------------
    def process(self, chunk: np.ndarray) -> None:
        if not self.active:
            return
        t0 = time.perf_counter()
        scores = self.model.predict(chunk)
        if self.vad is not None:
            self.vad(chunk)  # en todos los bloques, para que su historial esté alineado
        elapsed = (time.perf_counter() - t0) * 1000
        self.stats["chunks"] += 1
        self.stats["total_ms"] += elapsed
        score = float(scores.get(self.name, 0.0))
        self.last_score = score
        now = time.monotonic()
        if (self.on_partial and self.partial_threshold and score >= self.partial_threshold
                and now - self._last_partial > 0.5):
            self._last_partial = now
            try:
                self.on_partial(score)
            except Exception:
                log.exception("Error en el aviso de puntuación parcial")
        vad_history = list(self.vad.prediction_buffer) if self.vad is not None else None
        with self._lock:
            rejected = self.logic.vad_rejected
            fired = self.active and self.logic.feed(score, now, vad_history)
            if self.logic.vad_rejected != rejected:
                self.stats["vad_rejected"] += 1
                log.info("Activación descartada por el VAD (puntuación %.2f, sin voz)", score)
            if not fired:
                return
            self.stats["detections"] += 1
            self._reset_buffers()
        log.info("Wake word detectada (score %.2f, inferencia %.1f ms)", score, elapsed)
        if self.on_detect:
            self.on_detect(score)

    def mean_inference_ms(self) -> float:
        return self.stats["total_ms"] / self.stats["chunks"] if self.stats["chunks"] else 0.0
