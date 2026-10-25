"""Orquestador de Vimo: conecta audio, wake word, STT, Claude Code, TTS y cámara.

La GUI no implementa ninguna de estas operaciones: llama a métodos no bloqueantes de
VimoController y recibe eventos por `self.events` (queue.Queue) que consume en el hilo de Tk.

Eventos (tuplas):
    ("state", State)                      cambio de estado real
    ("status", str)                       mensaje informativo breve
    ("error", str)                        error comprensible para el usuario
    ("user_text", str)                    transcripción de la consulta
    ("response", str, list[dict])         respuesta original + fuentes
    ("listening", bool)                   escucha por wake word activada/desactivada
    ("recording", bool, str|None)         grabación iniciada/detenida (+ ruta o mensaje)
    ("components", dict)                  estado de cada componente tras el arranque
    ("progress", str)                     progreso (p. ej. «Buscando en DuckDuckGo…»)
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import queue
import threading
import time

from . import intents
from .audio_manager import (AudioPlayer, AudioUnavailable, MicrophoneStream, SoundBank, find_device, refresh_devices,
                            system_volume)
from .camera_manager import CameraError, CameraManager
from .claude_code_client import ClaudeCodeClient, ClaudeError
from .config_manager import PROJECT_ROOT, ConfigManager, resolve_path
from .conversation_memory import ConversationMemory
from .echo_cancel import ALSA_IN, ALSA_OUT, SINK as AEC_SINK, EchoCanceller, EchoCancelError, find_source
from .music_genres import match_genre
from .music_player import MpvBackend, MusicError, MusicPlayer, YouTubeSource
from .speech_to_text import MicHistory, NoiseTracker, STTError, UtteranceRecorder, WhisperTranscriber
from .state_manager import State, StateManager
from .text_to_speech import PiperTTS, TTSError, prepare_for_speech, split_sentences
from .video_recorder import VideoRecorder
from .wake_word import WakeWordDetector, WakeWordError

log = logging.getLogger(__name__)

WAKE_COMPONENT = "wake word (livekit)"


def _has_device(name: str, kind: str) -> bool:
    try:
        find_device(name, kind)
        return True
    except AudioUnavailable:
        return False
# Tras mostrar un error, Vimo vuelve solo al reposo pasado este tiempo (si nada lo ha cambiado).
ERROR_RECOVERY_SECONDS = 6.0

MUSIC_INTENTS = {intents.MUSIC_PLAY, intents.MUSIC_STOP, intents.MUSIC_PAUSE, intents.MUSIC_RESUME,
                 intents.MUSIC_NEXT, intents.MUSIC_NOW, intents.VOLUME_UP, intents.VOLUME_DOWN,
                 intents.VOLUME_SET, intents.STOP}


class VimoController:
    def __init__(self, config: ConfigManager):
        self.config = config
        self.events: queue.Queue = queue.Queue()
        self.state = StateManager()
        self.state.add_listener(lambda old, new, reason: self.events.put(("state", new)))
        self.components: dict[str, tuple[bool, str]] = {}
        self._turn_lock = threading.Lock()
        self._turn_cancel = threading.Event()
        self._speech_stop = threading.Event()
        self._claude_future: concurrent.futures.Future | None = None
        self._shutdown = threading.Event()
        self._last_wake = 0.0
        self._error_gen = 0

        self.mic: MicrophoneStream | None = None
        self.noise = NoiseTracker()
        self.mic_history = MicHistory()
        self._turn_since: float | None = None  # instante de la wake word del turno en curso
        self.detector: WakeWordDetector | None = None
        self.recorder: UtteranceRecorder | None = None
        self.stt: WhisperTranscriber | None = None
        self.player: AudioPlayer | None = None
        self.sounds: SoundBank | None = None
        self.tts: PiperTTS | None = None
        self.claude: ClaudeCodeClient | None = None
        self.camera: CameraManager | None = None
        self.video: VideoRecorder | None = None
        self.music: MusicPlayer | None = None
        self.aec: EchoCanceller | None = None
        self._resume_music_after_turn = False
        self._last_music_title: str | None = None
        self._duck_lock = threading.Lock()
        self._duck_timer: threading.Timer | None = None
        mem = config.section("memory")
        self.memory = ConversationMemory(resolve_path(mem["path"]), mem["enabled"], mem["max_turns"])

    # ===================================================================================
    # Arranque
    # ===================================================================================
    def start(self) -> None:
        self._warmup_thread = threading.Thread(target=self._warmup, name="warmup", daemon=True)
        self._warmup_thread.start()

    def _component(self, name: str, fn) -> bool:
        if self._shutdown.is_set():  # cerrando durante el arranque: no cargar nada más
            self.components[name] = (False, "arranque interrumpido")
            return False
        try:
            msg = fn() or "OK"
            self.components[name] = (True, msg)
            return True
        except Exception as exc:  # cada componente falla por separado sin tumbar a Vimo
            msg = str(exc) or type(exc).__name__
            self.components[name] = (False, msg)
            log.error("Componente %s no disponible: %s", name, msg, exc_info=not isinstance(
                exc, (AudioUnavailable, WakeWordError, STTError, TTSError, ClaudeError, CameraError,
                      EchoCancelError)))
            return False

    def _warmup(self) -> None:
        t0 = time.monotonic()
        cfg = self.config
        self.events.put(("status", "Preparando el audio…"))
        if self.config.get("audio.echo_cancel.enabled"):
            self._component("cancelación de eco", self._init_aec)
        else:
            self.components["cancelación de eco"] = (False, "desactivada en config.json")
        self._component("salida de audio", self._init_output)
        self._component("sonidos", self._init_sounds)
        self.events.put(("status", "Cargando la voz…"))
        self._component("voz (Piper)", self._init_tts)
        self.events.put(("status", "Preparando el micrófono…"))
        mic_ok = self._component("micrófono", self._init_mic)
        self.events.put(("status", "Cargando la palabra de activación…"))
        if mic_ok and cfg.get("wake_word.enabled"):
            self._component(WAKE_COMPONENT, self._init_wake)
        elif not mic_ok:
            self.components[WAKE_COMPONENT] = (False, "sin micrófono")
        else:
            self.components[WAKE_COMPONENT] = (False, "desactivada en config.json")
        self.events.put(("status", "Cargando Whisper…"))
        self._component("reconocimiento (Whisper.cpp)", self._init_stt)
        self.events.put(("status", "Comprobando Claude Code…"))
        self._component("Claude Code", self._init_claude)
        self._component("cámara (IMX519)", self._init_camera)
        self._component("música (YouTube)", self._init_music)
        self.events.put(("components", dict(self.components)))
        log.info("Arranque completado en %.1f s: %s", time.monotonic() - t0,
                 {k: v[0] for k, v in self.components.items()})
        if self._shutdown.is_set():
            log.info("Arranque interrumpido porque Vimo se está cerrando")
            return
        self.state.set(State.IDLE, "arranque completado")
        self.events.put(("listening", self.listening_enabled))
        problems = [k for k, (ok, _) in self.components.items() if not ok]
        if problems:
            self.events.put(("status", "Funcionando con limitaciones: " + ", ".join(problems)))
        else:
            self.events.put(("status", f"Listo. Di «{self.wake_label()}» o pulsa Hablar."))
        self._play_effect("greeting_sounds")

    @property
    def aec_active(self) -> bool:
        return bool(self.aec and self.aec.running)

    def _init_aec(self):
        e = self.config.get("audio.echo_cancel")
        capture = e["capture_target"] or find_source(self.config.get("audio.input_device"))
        self.aec = EchoCanceller(resolve_path("data/aec/vimo_aec.conf"), capture, e["playback_target"],
                                 e["aec_args"], resolve_path("logs/aec.log"))
        out = self.aec.start()
        # Los dispositivos ALSA de la cancelación de eco existen solo desde ahora: PortAudio tiene que
        # volver a enumerarlos (antes, el primer arranque se quedaba sin altavoz ni micrófono).
        missing = [ALSA_OUT, ALSA_IN]
        for _ in range(5):
            refresh_devices()
            missing = [d for d, kind in ((ALSA_OUT, "output"), (ALSA_IN, "input")) if not _has_device(d, kind)]
            if not missing:
                break
            time.sleep(1.0)
        if missing:
            self.aec.stop()  # mejor sin cancelación de eco que sin audio
            raise EchoCancelError(f"PortAudio no ve {', '.join(missing)}; se usan el micrófono y el altavoz "
                                  "sin cancelación de eco")
        return f"micrófono {capture or 'predeterminado'} · salida {out}"

    def _init_output(self):
        a = self.config.section("audio")
        # Se asigna solo si abre: un reproductor a medio iniciar haría fallar cada turno.
        player = AudioPlayer(ALSA_OUT if self.aec_active else a["output_device"], a["output_sample_rate"])
        player.open()
        player.on_start = self._on_playback_start
        player.on_end = self._on_playback_end
        self.player = player
        return f"{player.rate} Hz"

    def _init_sounds(self):
        s = self.config.section("sounds")
        if not s["enabled"]:
            return "desactivados"
        base = resolve_path(s["directory"])
        directory = os.path.join(base, s["language_subdir"]) if s["language_subdir"] else base
        self.sounds = SoundBank(directory)
        if not self.sounds.sounds:
            raise RuntimeError(f"No hay sonidos en {directory}")
        return ", ".join(f"{k}: {len(v)}" for k, v in self.sounds.sounds.items())

    def _init_tts(self):
        t = self.config.section("tts")
        if not t["enabled"]:
            raise TTSError("desactivada en config.json (las respuestas se muestran en pantalla)")
        self.tts = PiperTTS(resolve_path(t["voice_model"]),
                            resolve_path(t["voice_config"]) if t["voice_config"] else None,
                            t["length_scale"], t["noise_scale"], t["noise_w_scale"], t["speaker_id"],
                            self.config.get("audio.speech_volume"), t["threads"])
        self.tts.load()
        return os.path.basename(self.tts.voice_model)

    def _init_mic(self):
        a = self.config.section("audio")
        mic = MicrophoneStream(ALSA_IN if self.aec_active else a["input_device"], a["input_sample_rate"],
                               self.config.get("wake_word.chunk_size"), a["queue_max_chunks"])
        mic.on_error = lambda msg: self.events.put(("status", msg))
        mic.start()
        mic.add_sink(self.noise)
        mic.add_sink(self.mic_history)
        self.mic = mic
        self.recorder = UtteranceRecorder(mic, self.noise, self.config.section("listening"))
        return f"{mic.device_name} a {mic.rate} Hz"

    def _init_wake(self):
        w = self.config.section("wake_word")
        detector = WakeWordDetector(
            resolve_path(w["model_path"]), resolve_path(w["melspec_model_path"]),
            resolve_path(w["embedding_model_path"]), threshold=w["threshold"],
            cooldown_seconds=w["cooldown_seconds"], resume_delay_seconds=w["resume_delay_seconds"],
            patience_frames=w["patience_frames"], vad_model_path=resolve_path(w["vad_model_path"]),
            vad_threshold=w["vad_threshold"], partial_threshold=w["duck_music_score"] or None,
            exact_gate=w["exact_gate"], threads=w["threads"])
        detector.load()
        detector.on_detect = self._on_wake
        detector.on_partial = self._on_wake_partial
        self.detector = detector
        self.mic.add_sink(detector.process)
        return f"{detector.name} (umbral {w['threshold']})"

    def _init_stt(self):
        s = self.config.section("stt")
        stt = WhisperTranscriber(os.path.expanduser(s["whisper_dir"]), s["server_binary"], s["cli_binary"],
                                 s["model"], s["language"], s["threads"], s["use_server"], s["server_port"],
                                 s["timeout_seconds"], resolve_path("logs"), s["audio_ctx"])
        stt.start()
        self.stt = stt
        mode = "servidor" if stt._server else "CLI"
        return f"{os.path.basename(stt.model)} ({mode}, idioma {s['language']})"

    def _init_claude(self):
        c = self.config.section("claude")
        sid, turns = self.memory.resumable_session()
        self.claude = ClaudeCodeClient(c, self.config.get("agent.personality"),
                                       os.path.join(PROJECT_ROOT, "data", "claude_workspace"),
                                       os.path.join(PROJECT_ROOT, "data"), self.config.path, sid, turns)
        self.claude.on_session_change = self.memory.set_session
        self.claude.write_mcp_config()
        info = self.claude.check()
        return info.get("version", "")

    def _init_camera(self):
        cam = self.config.section("camera")
        if not cam["enabled"]:
            raise CameraError("desactivada en config.json")
        v = self.config.section("video")
        self.camera = CameraManager(resolve_path(cam["photo_dir"]), tuple(cam["photo_resolution"]), cam["rotation"])
        self.video = VideoRecorder(self.camera, resolve_path(v["directory"]), v["profiles"], v["profile"],
                                   v["min_free_mb"], v["max_minutes"], v["rotate_mode"])
        self.video.on_auto_stop = self._on_recording_auto_stop
        info = self.camera.detect()
        if not info:
            raise CameraError(self.camera.error or "sin cámara")
        recovered = self.video.recover_incomplete()
        if recovered:
            self.events.put(("status", f"Recuperadas {len(recovered)} grabaciones interrumpidas"))
        return str(info.get("Model"))

    def _init_music(self):
        m = self.config.section("music")
        if not m["enabled"]:
            raise MusicError("desactivada en config.json")
        source = YouTubeSource(resolve_path(m["ytdlp"]), resolve_path(m["cache_dir"]), m["js_runtime"] or None,
                               m["download_timeout_seconds"], m["ytdlp_cpu_cores"], m["ytdlp_nice"])
        mpv_args = list(m["mpv_args"])
        if self.aec_active:   # la música también pasa por la cancelación de eco
            mpv_args = [x for x in mpv_args if not x.startswith("--audio-device")] + [
                f"--audio-device=pipewire/{AEC_SINK}"]
        backend = MpvBackend(os.path.join(PROJECT_ROOT, "data", "mpv.sock"), mpv_args)
        versions = f"yt-dlp {source.check()}, {backend.check().split(' Copyright')[0]}"
        self.music = MusicPlayer(source, backend, m["volume"], m["volume_step"], m["min_duration_seconds"],
                                 m["max_duration_seconds"], m["mix_size"], m["max_volume"])
        self.music.on_change = self._on_music_change
        self.music.on_error = lambda msg: self.events.put(("error", f"Música: {msg}"))
        self.music.stop()  # vacía restos de la caché de una ejecución anterior
        return versions

    # ===================================================================================
    # Utilidades
    # ===================================================================================
    def wake_label(self) -> str:
        return self.config.get("wake_word.phrase") or "la palabra de activación"

    @property
    def listening_enabled(self) -> bool:
        return bool(self.detector and self.detector.enabled)

    def _on_playback_start(self) -> None:
        # Evita que la propia voz de Vimo active la wake word o falsee el nivel de ruido.
        self.noise.paused = True
        self.mic_history.keep_while_playing = self.aec_active  # con AEC el pitido no llega al micrófono
        self.mic_history.playing = True
        if self.detector:
            self.detector.suspend()

    def _on_playback_end(self) -> None:
        self.mic_history.playing = False
        self.noise.paused = bool(self.music and self.music.playing)
        if self.detector:
            self.detector.resume()

    def _on_music_change(self, status: dict) -> None:
        # Con música sonando no se aprende el «ruido ambiente» (subiría el umbral de voz).
        # La wake word sigue activa para poder decir «Hey Vimo, para la música».
        if not (self.player and self.player.playing):
            self.noise.paused = status["active"] and not status["paused"]
        if status["title"] != self._last_music_title:
            self.events.put(("status", f"Sonando: {status['title']}" if status["title"] else "Música detenida"))
            self._last_music_title = status["title"]
        self.events.put(("music", status))

    def _play_effect(self, category: str, block: bool = False) -> None:
        if not (self.sounds and self.player):
            return
        item = self.sounds.pick(category)
        if item:
            _name, audio, rate = item
            try:
                self.player.play(audio, rate, volume=self.config.get("audio.effects_volume"), block=block)
            except Exception:
                log.exception("No se pudo reproducir %s", category)

    def _error(self, message: str) -> None:
        log.warning("Error mostrado al usuario: %s", message)
        self.events.put(("error", message))
        self.state.set(State.ERROR, message[:60], strict=False)
        # Sin esto, ERROR no se abandona nunca: la cara de error queda fija y la pantalla no se
        # apaga por inactividad. Solo vuelve a IDLE si sigue siendo este mismo error.
        self._error_gen += 1
        gen = self._error_gen

        def recover() -> None:
            if gen == self._error_gen and not self._shutdown.is_set():
                self.state.transition_if({State.ERROR}, State.IDLE, "recuperado tras error")

        t = threading.Timer(ERROR_RECOVERY_SECONDS, recover)
        t.daemon = True
        t.start()

    # ===================================================================================
    # Acciones (llamadas desde la GUI o desde la wake word; no bloquean)
    # ===================================================================================
    def toggle_listening(self) -> None:
        if not self.detector:
            self.events.put(("error", "La wake word no está disponible: "
                             + self.components.get(WAKE_COMPONENT, (False, "?"))[1]
                             + ". Usa el botón Hablar."))
            return
        self.detector.set_enabled(not self.detector.enabled)
        self.events.put(("listening", self.detector.enabled))
        self.events.put(("status", "Escucha activada" if self.detector.enabled else "Escucha desactivada"))

    def _on_wake_partial(self, score: float) -> None:
        """La puntuación empieza a subir: baja la música para oír bien el resto de «Hey Vimo»."""
        m = self.music
        if not (m and m.playing):
            return
        with self._duck_lock:
            if self._duck_timer is None:
                # Se llama desde el hilo del micrófono: la orden a mpv (hasta 5 s si está ocupado)
                # va en otro hilo para no perder bloques de audio justo mientras dices «…Vimo».
                ratio = self.config.get("music.duck_ratio")

                def duck() -> None:
                    with self._duck_lock:  # si ya se restauró (orden retrasada), no volver a bajar
                        if self._duck_timer is None:
                            return
                    m.duck(ratio)

                threading.Thread(target=duck, name="music-duck", daemon=True).start()
                log.info("Música atenuada (posible «%s», puntuación %.2f)", self.wake_label(), score)
            else:
                self._duck_timer.cancel()
            self._duck_timer = threading.Timer(self.config.get("music.duck_seconds"), self._unduck)
            self._duck_timer.daemon = True
            self._duck_timer.start()

    def _unduck(self) -> None:
        with self._duck_lock:
            if self._duck_timer is not None:
                self._duck_timer.cancel()
            self._duck_timer = None
        if self.music:
            self.music.duck(None)

    def _on_wake(self, score: float) -> None:
        self._last_wake = time.monotonic()
        if self.state.state not in (State.IDLE, State.ERROR):
            log.info("Wake word ignorada en estado %s", self.state.state.name)
            return
        self._start_turn(source="wake word", since=self._last_wake)

    def push_to_talk(self) -> None:
        st = self.state.state
        if st == State.SPEAKING:
            self.interrupt(go_listen=True)
            return
        if st in (State.THINKING, State.LISTENING, State.WARMUP):
            self.events.put(("status", "Espera un momento…" if st != State.LISTENING else "Ya te escucho"))
            return
        if not self.recorder:
            self.events.put(("error", "No hay micrófono disponible"))
            return
        self._start_turn(source="botón")

    def submit_text(self, text: str) -> None:
        """Consulta escrita (útil sin micrófono)."""
        if text.strip():
            self._start_turn(source="texto", text=text.strip())

    def _start_turn(self, source: str, text: str | None = None, since: float | None = None) -> None:
        # La GUI enciende la pantalla al instante (antes del sonido de confirmación).
        self.events.put(("activity", source))
        if not self._turn_lock.acquire(blocking=False):
            log.info("Turno ignorado (%s): ya hay uno en curso", source)
            return
        self._turn_cancel.clear()
        self._speech_stop.clear()
        # Con la wake word, la consulta se graba desde la detección (ver MicHistory): así no se
        # pierde lo dicho mientras suena el pitido de confirmación.
        self._turn_since = since
        threading.Thread(target=self._turn, args=(source, text), name="turn", daemon=True).start()

    def interrupt(self, go_listen: bool = False) -> None:
        """Interrumpe la respuesta hablada o la petición en curso sin perder la conversación."""
        st = self.state.state
        self._speech_stop.set()
        if self.player:
            self.player.stop(wait=False)
        if st in (State.THINKING, State.LISTENING):
            self._turn_cancel.set()
            if self.claude and st == State.THINKING:
                self.claude.cancel()
        if go_listen:
            # Escuchar en cuanto termine el turno actual.
            threading.Thread(target=self._listen_after_interrupt, daemon=True).start()

    def _listen_after_interrupt(self) -> None:
        if self._turn_lock.acquire(timeout=5):
            self._turn_lock.release()
            self._start_turn(source="interrupción")
        else:
            self.events.put(("status", "Sigo terminando lo anterior; pulsa Hablar otra vez en un momento."))

    # ===================================================================================
    # Turno de conversación
    # ===================================================================================
    def _turn(self, source: str, text: str | None) -> None:
        timings: dict[str, float] = {}
        t0 = time.monotonic()
        # La música se pausa mientras se habla con Vimo y se reanuda al terminar el turno.
        self._resume_music_after_turn = False
        try:
            if self.music and self.music.playing and self.config.get("music.pause_while_talking"):
                try:
                    self.music.pause()
                    self._resume_music_after_turn = True
                except Exception:  # mpv caído: el turno sigue sin música
                    log.warning("No se pudo pausar la música", exc_info=True)
            if text is None:
                text = self._listen(timings)
                if text is None:
                    return
            self.events.put(("user_text", text))
            intent = intents.parse(text)
            if intent:
                self._run_intent(intent)
                return
            reply = self._ask_claude(text, timings)
            if reply is None:
                return
            self._speak(reply.text, timings)
            self.memory.add_turn(text, reply.text, reply.sources,
                                 {"searches": reply.searches} if reply.searches else None)
            if self.state.state == State.SPEAKING:
                self.state.set(State.IDLE, "respuesta completada")
        except Exception as exc:
            log.exception("Error inesperado en el turno")
            self._error(f"Error inesperado: {exc}")
        finally:
            timings["total"] = time.monotonic() - t0
            log.info("Tiempos del turno (%s): %s", source, {k: round(v, 2) for k, v in timings.items()})
            if self.state.state in (State.LISTENING, State.THINKING, State.SPEAKING, State.CAPTURING):
                self.state.set(State.IDLE, "fin de turno", strict=False)
            # Pase lo que pase con la música, el cerrojo se libera: si no, Vimo ignoraría todos
            # los turnos siguientes hasta reiniciarlo.
            try:
                if self.music and self._duck_timer is not None:
                    self._unduck()
                if self._resume_music_after_turn and self.music and self.music.active:
                    self.music.resume()
            except Exception:
                log.warning("No se pudo reanudar la música", exc_info=True)
            finally:
                self._resume_music_after_turn = False
                self._turn_lock.release()

    def _listen(self, timings: dict) -> str | None:
        if not (self.recorder and self.stt):
            self._error("No puedo escuchar: falta el micrófono o Whisper.cpp")
            return None
        # La wake word sigue suspendida también mientras Whisper transcribe: _on_wake ignora las
        # detecciones fuera de IDLE, y su inferencia solo competiría con Whisper por la CPU (y sumaría
        # consumo justo en el pico que ya apagó la Raspberry Pi).
        if self.detector:
            self.detector.suspend()
        try:
            return self._capture_and_transcribe(timings)
        finally:
            if self.detector:
                self.detector.resume()

    def _capture_and_transcribe(self, timings: dict) -> str | None:
        self._play_effect("ack_sounds", block=True)
        if self._turn_cancel.is_set():
            return None
        self.state.set(State.LISTENING, "captura de la consulta", strict=False)
        t = time.monotonic()
        utt = self.recorder.record(self._turn_cancel, self.mic_history, self._turn_since)
        timings["record"] = time.monotonic() - t
        if utt.reason == "cancelled":
            return None
        if not utt.has_speech:
            self.events.put(("status", "No te he oído. Vuelve a intentarlo."))
            self.state.set(State.IDLE, "sin voz", strict=False)
            return None
        self.state.set(State.THINKING, "transcribiendo", strict=False)
        t = time.monotonic()
        try:
            text = self.stt.transcribe(utt.audio)
        except STTError as exc:
            self._error(str(exc))
            return None
        timings["stt"] = time.monotonic() - t
        if self._turn_cancel.is_set():  # «Parar» pulsado durante la transcripción
            log.info("Turno cancelado durante la transcripción")
            return None
        text = intents.strip_wake_phrase(text)
        if not text:
            self.events.put(("status", "No he entendido nada. ¿Puedes repetirlo?"))
            self.state.set(State.IDLE, "transcripción vacía", strict=False)
            return None
        return text

    def _ask_claude(self, text: str, timings: dict):
        if not self.claude or not self.components.get("Claude Code", (False,))[0]:
            msg = self.components.get("Claude Code", (False, "no disponible"))[1]
            self._error(f"Claude Code no está disponible: {msg}")
            return None
        self.state.set(State.THINKING, "consulta a Claude Code", strict=False)
        if self.config.get("sounds.thinking_sound"):
            self._play_effect("thinking_sounds")

        def progress(kind: str, data: dict) -> None:
            if kind == "search":
                self.events.put(("progress", f"Buscando en DuckDuckGo: {data.get('query', '')}"))

        t = time.monotonic()
        try:
            self._claude_future = self.claude.submit(text, progress)
            reply = self._claude_future.result()
        except ClaudeError as exc:
            if exc.kind == "cancelled":
                self.events.put(("status", "Petición cancelada"))
                self.state.set(State.IDLE, "cancelado", strict=False)
            else:
                self._error(str(exc))
            return None
        finally:
            self._claude_future = None
            timings["claude"] = time.monotonic() - t
        if self._turn_cancel.is_set():
            self.state.set(State.IDLE, "cancelado", strict=False)
            return None
        self.events.put(("response", reply.text, reply.sources))
        if not reply.text:
            self._error("Claude Code devolvió una respuesta vacía")
            return None
        return reply

    def _speak(self, text: str, timings: dict | None = None) -> None:
        if not (self.tts and self.tts.voice and self.player):
            # Sin voz: la respuesta ya está en pantalla.
            self.events.put(("status", "Respuesta mostrada en pantalla (la voz no está disponible)"))
            self.state.set(State.IDLE, "sin TTS", strict=False)
            return
        spoken = prepare_for_speech(text, self.config.get("tts.max_spoken_chars"))
        segments = split_sentences(spoken)
        if not segments or self._speech_stop.is_set():
            return
        self.player.stop(wait=True)  # corta el sonido de «pensando»
        self.state.set(State.SPEAKING, "reproducción de la respuesta", strict=False)
        stats: dict = {}
        done = threading.Event()
        result = {"interrupted": False}

        def on_done(interrupted: bool) -> None:
            result["interrupted"] = interrupted
            done.set()

        t = time.monotonic()
        self.player.play_stream(self.tts.stream(segments, self._speech_stop, stats), on_done=on_done)
        done.wait(timeout=max(30.0, len(spoken) / 5))
        if timings is not None:
            timings["tts_first_segment"] = stats.get("first_segment_seconds", 0.0)
            timings["speech"] = time.monotonic() - t
        if result["interrupted"] or self._speech_stop.is_set():
            self.events.put(("status", "Respuesta interrumpida"))
        if self.state.state == State.SPEAKING:
            self.state.set(State.IDLE, "fin de la respuesta hablada")

    # ===================================================================================
    # Órdenes locales: cámara y conversación
    # ===================================================================================
    def _run_intent(self, intent: intents.Intent) -> None:
        log.info("Orden local: %s %s", intent.name, intent.arg or "")
        if intent.name in MUSIC_INTENTS:
            self._run_music_intent(intent)
            return
        intent = intent.name
        if intent == intents.RESET:
            self.reset_conversation()
            self._speak("Listo, empezamos una conversación nueva.")
        elif intent == intents.PHOTO:
            ok, msg = self._do_photo()
            self._speak("Foto guardada." if ok else msg)
        elif intent == intents.RECORD_START:
            ok, msg = self._do_record_start()
            self._speak("Empiezo a grabar." if ok else msg)
        elif intent == intents.RECORD_STOP:
            ok, msg = self._do_record_stop()
            self._speak("Grabación terminada y guardada." if ok else msg)

    # ===================================================================================
    # Música
    # ===================================================================================
    def _run_music_intent(self, intent: intents.Intent) -> None:
        name, arg = intent.name, intent.arg
        m = self.music
        if name in (intents.VOLUME_UP, intents.VOLUME_DOWN, intents.VOLUME_SET):
            self._change_volume(name, arg)
            return
        if m is None:
            self._speak("La música no está disponible: "
                        + self.components.get("música (YouTube)", (False, "?"))[1])
            return
        if name == intents.MUSIC_PLAY:
            self._resume_music_after_turn = False
            if arg in ("", "musica", "algo", "algo de musica", "una cancion", "canciones"):
                if m.active and m.paused:
                    m.resume()
                else:
                    self._speak("¿Qué quieres escuchar? Dime, por ejemplo: pon Soda Stereo.")
                return
            self._play_music(arg)
        elif name in (intents.MUSIC_STOP, intents.STOP):
            self._resume_music_after_turn = False
            if m.active:
                m.stop()
                self._speak("Música detenida.")
            elif name == intents.MUSIC_STOP:
                self._speak("No hay música sonando.")
        elif name == intents.MUSIC_PAUSE:
            self._resume_music_after_turn = False
            if m.active:
                m.pause()
            else:
                self._speak("No hay música sonando.")
        elif name == intents.MUSIC_RESUME:
            self._resume_music_after_turn = False
            if m.active:
                m.resume()
            else:
                self._speak("No hay música en pausa. Dime qué quieres escuchar.")
        elif name == intents.MUSIC_NEXT:
            if m.active:
                m.next()
                m.resume()
                self._resume_music_after_turn = False
            else:
                self._speak("No hay música sonando.")
        elif name == intents.MUSIC_NOW:
            if m.current:
                channel = f", del canal {m.current.channel}" if m.current.channel else ""
                self._speak(f"Está sonando {m.current.title}{channel}.")
            else:
                self._speak("Ahora no está sonando nada.")

    def _play_music(self, query: str) -> None:
        """Busca (rápido), anuncia la canción y la descarga y reproduce en segundo plano.
        Si `query` es un género («rock», «techno», «house»…) reproduce ese género."""
        genre = match_genre(query)
        if genre:
            self._play_genre(genre)
            return
        self.state.set(State.THINKING, "buscando música", strict=False)
        self.events.put(("progress", f"Buscando en YouTube: {query}"))
        try:
            track = self.music.find(query)
        except MusicError as exc:
            self._speak(f"No pude buscar esa canción. {exc}")
            return
        self.events.put(("status", f"Preparando ♪ {track.label()}"))

        def worker() -> None:
            try:
                self.music.play(track)
            except MusicError as exc:
                if str(exc) != "cancelado":
                    self.events.put(("error", f"No pude reproducir «{track.title}»: {exc}"))
        threading.Thread(target=worker, name="music-play", daemon=True).start()
        self._speak(f"Vale, pongo {track.title}.")

    def _play_genre(self, genre) -> None:
        self.state.set(State.THINKING, "buscando género", strict=False)
        self.events.put(("progress", f"Buscando {genre.label} en YouTube"))
        try:
            tracks = self.music.find_genre(genre)
        except MusicError as exc:
            self._speak(f"No pude encontrar {genre.label}. {exc}")
            return
        first = tracks[0]
        self.events.put(("status", f"Preparando {genre.label} ♪ {first.label()}"))

        def worker() -> None:
            try:
                self.music.play(first, genre=genre, seeds=tracks[1:])
            except MusicError as exc:
                if str(exc) != "cancelado":
                    self.events.put(("error", f"No pude reproducir {genre.label}: {exc}"))
        threading.Thread(target=worker, name="music-play", daemon=True).start()
        self._speak(f"Vale, pongo {genre.label}.")

    def _change_volume(self, name: str, arg: str | None) -> None:
        m = self.music
        if m and m.active:
            # Con música en curso, «volumen» se refiere a la música (la voz de Vimo no cambia).
            before = m.volume
            if name == intents.VOLUME_SET:
                m.set_volume(int(arg))
            elif name == intents.VOLUME_UP:
                m.volume_up()
            else:
                m.volume_down()
            self.config.update({"music.volume": m.volume})
            self.events.put(("status", f"Volumen de la música: {m.volume} %"))
            # Siempre se confirma en voz alta: antes, al estar ya en el tope (music.max_volume),
            # «sube el volumen» no hacía nada y no decía nada.
            wanted_more = name == intents.VOLUME_UP or (name == intents.VOLUME_SET and int(arg) > m.volume)
            if m.volume >= m.max_volume and wanted_more:
                # El tope existe por la cancelación de eco (docs/eco.md): más alto, no oiría «Hey Vimo».
                self._speak(f"{'Ya está' if before >= m.max_volume else 'La pongo'} al máximo, "
                            f"{m.max_volume} por ciento. Más alto no te oiría bien con la música.")
            elif m.volume == before and name == intents.VOLUME_DOWN:
                self._speak("La música ya está al mínimo.")
            else:
                self._speak(f"Música al {m.volume} por ciento.")
            return
        # Sin música: volumen general del altavoz (PipeWire).
        try:
            step = self.config.get("music.volume_step") / 100
            level = system_volume(set_to=int(arg) / 100 if name == intents.VOLUME_SET else None,
                                  delta=None if name == intents.VOLUME_SET else
                                  (step if name == intents.VOLUME_UP else -step))
        except RuntimeError as exc:
            self._speak(f"No pude cambiar el volumen: {exc}")
            return
        self.events.put(("status", f"Volumen del altavoz: {round(level * 100)} %"))
        self._speak(f"Volumen al {round(level * 100)} por ciento.")

    # Botones de la GUI (no bloquean)
    def music_toggle_pause(self) -> None:
        if self.music and self.music.active:
            (self.music.resume if self.music.paused else self.music.pause)()

    def music_next(self) -> None:
        if self.music and self.music.active:
            self.music.next()

    def music_volume(self, delta: int) -> None:
        if self.music and self.music.active:
            self.music.set_volume(self.music.volume + delta)
            self.config.update({"music.volume": self.music.volume})
            self.events.put(("status", f"Volumen de la música: {self.music.volume} %"))

    def music_stop(self) -> None:
        if self.music and self.music.active:
            threading.Thread(target=self.music.stop, name="music-stop", daemon=True).start()

    def reset_conversation(self) -> None:
        if self.claude:
            self.claude.reset_session()
        self.memory.reset()
        self.events.put(("status", "Conversación nueva"))
        self.events.put(("response", "", []))

    def take_photo(self) -> None:
        threading.Thread(target=self._do_photo, name="photo", daemon=True).start()

    def toggle_recording(self) -> None:
        fn = self._do_record_stop if (self.video and self.video.recording) else self._do_record_start
        threading.Thread(target=fn, name="recording", daemon=True).start()

    def _camera_ready(self) -> str | None:
        if not (self.camera and self.camera.available):
            return "La cámara no está disponible: " + self.components.get("cámara (IMX519)", (False, "?"))[1]
        return None

    def _with_capturing_face(self, fn):
        """Muestra la cara de cámara solo mientras se ejecuta la operación real."""
        entered = self.state.transition_if({State.IDLE, State.ERROR, State.THINKING}, State.CAPTURING, "cámara")
        try:
            return fn()
        finally:
            if entered:
                self.state.transition_if({State.CAPTURING}, State.IDLE, "cámara terminada")

    def _do_photo(self) -> tuple[bool, str]:
        problem = self._camera_ready()
        if problem:
            self.events.put(("error", problem))
            return False, problem
        try:
            path = self._with_capturing_face(self.camera.take_photo)
        except CameraError as exc:
            self.events.put(("error", str(exc)))
            return False, str(exc)
        self.events.put(("status", f"Foto guardada: {path}"))
        return True, path

    def _do_record_start(self) -> tuple[bool, str]:
        problem = self._camera_ready()
        if problem:
            self.events.put(("error", problem))
            return False, problem
        try:
            path = self._with_capturing_face(self.video.start)
        except CameraError as exc:
            self.events.put(("error", str(exc)))
            return False, str(exc)
        self.events.put(("recording", True, path))
        return True, path

    def _do_record_stop(self) -> tuple[bool, str]:
        if not (self.video and self.video.recording):
            return False, "No hay ninguna grabación en curso."
        self.events.put(("status", "Deteniendo la grabación…"))
        try:
            result = self._with_capturing_face(self.video.stop)
        except CameraError as exc:
            self.events.put(("recording", False, None))
            self.events.put(("error", f"La grabación falló: {exc}"))
            return False, f"La grabación falló: {exc}"
        self.events.put(("recording", False, result.path))
        self.events.put(("status", f"Vídeo guardado: {result.describe()}"))
        return True, result.path

    def _on_recording_auto_stop(self, result, reason: str) -> None:
        self.events.put(("recording", False, result.path if result else None))
        self.events.put(("error" if result is None else "status", f"Grabación detenida: {reason}"))

    # ===================================================================================
    # Configuración en caliente y cierre
    # ===================================================================================
    def apply_settings(self, changes: dict) -> list[str]:
        warnings = self.config.update(changes)
        if self.detector:
            self.detector.threshold = self.config.get("wake_word.threshold")
        if self.tts:
            self.tts.set_length_scale(self.config.get("tts.length_scale"))
        if self.video:
            self.video.profile = self.config.get("video.profile")
        if self.recorder:
            self.recorder.cfg = self.config.section("listening")
        self.memory.set_enabled(self.config.get("memory.enabled"))
        return warnings

    def shutdown(self) -> None:
        if self._shutdown.is_set():
            return
        self._shutdown.set()
        log.info("Cerrando Vimo…")
        # Si aún está arrancando, esperar a que termine el componente en curso: cerrar el proceso
        # mientras onnxruntime/PortAudio cargan en otro hilo provoca un abort (std::terminate).
        warmup = getattr(self, "_warmup_thread", None)
        if warmup and warmup.is_alive() and warmup is not threading.current_thread():
            log.info("Esperando a que termine el arranque en curso…")
            warmup.join(timeout=60)
        self._turn_cancel.set()
        self._speech_stop.set()
        if self.video:
            self.video.shutdown()
        if self.camera:
            self.camera.close()
        if self.claude:
            self.claude.shutdown()
        if self.music:
            self.music.shutdown()
        if self.player:
            self.player.stop(wait=True)
        if self.mic:
            self.mic.stop()
        if self.stt:
            self.stt.stop()
        if self.aec:
            self.aec.stop()
        if self.detector and self.detector.stats["chunks"]:
            log.info("Wake word: %d bloques, %.1f ms de media, %d detecciones", self.detector.stats["chunks"],
                     self.detector.mean_inference_ms(), self.detector.stats["detections"])
        log.info("Vimo cerrado correctamente")
