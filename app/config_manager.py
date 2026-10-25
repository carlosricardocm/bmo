"""Carga, validación y guardado de la configuración de Vimo (config.json).

- Los valores ausentes se completan con DEFAULTS.
- Los valores con tipo o rango inválido se sustituyen por el predeterminado y se registra un aviso.
- Un config.json dañado se conserva como config.json.corrupt-<fecha> y se continúa con los valores
  predeterminados, sin perder el archivo del usuario.
- El guardado es atómico (archivo temporal + fsync + rename) para sobrevivir a cortes de energía.
"""
from __future__ import annotations

import copy
import json
import logging
import os
import tempfile
import threading
import time
from typing import Any

log = logging.getLogger(__name__)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DEFAULT_PERSONALITY = (
    "Eres Vimo, un pequeño robot asistente que vive en una Raspberry Pi 5. "
    "Eres amable, curioso y algo juguetón, pero vas al grano. "
    "Respondes siempre en español salvo que el usuario hable en otro idioma."
)

DEFAULTS: dict[str, Any] = {
    "agent": {
        "name": "Vimo",
        "personality": DEFAULT_PERSONALITY,
        "language": "es",
    },
    "wake_word": {
        "enabled": True,
        "engine": "livekit",
        "phrase": "Hey Vimo",
        "model_path": "models/hey_vimo_mis_audios.onnx",  # livekit-wakeword + grabaciones del usuario (25-10-2026)
        "melspec_model_path": "models/openwakeword/melspectrogram.onnx",
        "embedding_model_path": "models/openwakeword/embedding_model.onnx",
        # Con el preprocesado de livekit (25-10-2026): 98 % de la voz del usuario, 0 falsas en 23 min de
        # música y noticias. (0,21 era el valor para openWakeWord, que puntuaba más bajo).
        "threshold": 0.3,
        "sample_rate": 16000,
        "chunk_size": 1280,
        "cooldown_seconds": 2.0,
        "resume_delay_seconds": 0.6,
        # Bloques de 80 ms seguidos por encima del umbral antes de activar (1 = inmediato).
        "patience_frames": 1,
        # Filtro de voz Silero: > 0 descarta activaciones sin voz humana (música, golpes). 0 = desactivado.
        "vad_threshold": 0.5,
        "vad_model_path": "models/openwakeword/silero_vad.onnx",
        # Puntuación a partir de la que se baja la música mientras terminas de decir la frase.
        "duck_music_score": 0.15,
        # Puntuación aproximada a partir de la que se recalcula la ventana completa de livekit
        # (exacta pero ~86 ms). Se limita a la mitad del umbral y a duck_music_score.
        "exact_gate": 0.1,
        # Hilos de onnxruntime para la wake word (1 = carga mínima de CPU).
        "threads": 1,
    },
    "audio": {
        "input_device": "iTalk",
        "output_device": "default",
        "input_sample_rate": None,
        "output_sample_rate": None,
        "queue_max_chunks": 50,
        "effects_volume": 0.8,
        "speech_volume": 1.0,
        # Cancelación de eco (WebRTC de PipeWire): quita del micrófono lo que suena por el altavoz,
        # para detectar «Hey Vimo» con música. Vimo lanza su propio proceso; no cambia el sistema.
        "echo_cancel": {
            "enabled": True,
            "capture_target": None,     # None = el micrófono cuyo nombre contiene audio.input_device
            "playback_target": None,    # None = la salida predeterminada al arrancar
            "aec_args": {
                "webrtc.noise_suppression": False,   # no alterar la voz que analiza la wake word
                "webrtc.gain_control": False,
                "webrtc.high_pass_filter": True,
                "webrtc.transient_suppression": True,
                "webrtc.voice_detection": False,
            },
        },
    },
    "listening": {
        "max_seconds": 12.0,
        "silence_seconds": 1.0,
        "no_speech_timeout_seconds": 5.0,
        "min_speech_seconds": 0.35,
        "energy_threshold": 0.012,
        "noise_multiplier": 2.5,
    },
    "stt": {
        "engine": "whisper.cpp",
        "whisper_dir": "~/whisper.cpp",
        "server_binary": "build/bin/whisper-server",
        "cli_binary": "build/bin/whisper-cli",
        "model": "models/ggml-small-q5_1.bin",
        "language": "es",
        # 2 hilos: con 4, la Raspberry Pi se apagó dos veces el 25-10-2026 (pico de consumo; ver
        # docs/whisper.md). Con audio_ctx 768, 2 hilos tardan menos que 4 sin él.
        "threads": 2,
        # Contexto de audio (-ac): 768 ≈ 15 s, suficiente para listening.max_seconds. 0 = 30 s completos.
        "audio_ctx": 768,
        "use_server": True,
        "server_port": 8178,
        "timeout_seconds": 60.0,
    },
    "claude": {
        "executable": "claude",
        "model": None,
        "timeout_seconds": 120.0,
        "session_max_turns": 40,
        "max_budget_usd": None,
        "enable_web_search": True,
        "extra_args": [],
    },
    "web_search": {
        "enabled": True,
        "region": "es-es",
        "safesearch": "moderate",
        "max_results": 5,
        "timeout_seconds": 10,
    },
    "tts": {
        "enabled": True,
        "engine": "piper",
        "voice_model": "voices/voz_bmo_final_v2.onnx",
        "voice_config": None,
        "length_scale": 1.0,
        "noise_scale": None,
        "noise_w_scale": None,
        "speaker_id": None,
        "max_spoken_chars": 900,
        "threads": 2,          # hilos de Piper (limitar reduce los picos de consumo)
        "timeout_seconds": 60.0,
    },
    "sounds": {
        "enabled": True,
        "directory": "sounds",
        "language_subdir": "",
        "thinking_sound": True,
    },
    "faces": {
        "directory": "faces",
        "frame_ms": {"speaking": 160, "thinking": 350, "listening": 450, "default": 500,
                     "music_dance": 190, "music_sing": 230, "music_vibe": 420, "music_wow": 220,
                     "idle_blink": 160, "idle_look": 260, "idle_smile": 320, "idle_breathe": 300,
                     "idle_yawn": 230, "idle_sleepy": 480},
        # Gestos breves en reposo (faces/idle_*, generadas con tools/generar_caras_reposo.py).
        "idle_animations": ["idle_blink", "idle_look", "idle_smile", "idle_breathe", "idle_yawn", "idle_sleepy"],
        "idle_gesture_min_seconds": 8,
        "idle_gesture_max_seconds": 20,
        # Animaciones mientras suena música (faces/music_*, generadas con tools/generar_caras_musica.py).
        "music_animations": ["music_dance", "music_sing", "music_vibe", "music_wow"],
        "music_switch_seconds": 20,
    },
    "camera": {
        "enabled": True,
        "photo_dir": "photos",
        "photo_resolution": [2328, 1748],
        "rotation": 0,
    },
    "video": {
        "directory": "recordings",
        "profile": "1080p30",
        "profiles": {
            "720p30": {"size": [1280, 720], "fps": 30, "bitrate": 4000000},
            "1080p30": {"size": [1920, 1080], "fps": 30, "bitrate": 8000000},
            "4k": {"size": [3840, 2160], "fps": 15, "bitrate": 20000000},
        },
        "min_free_mb": 500,
        # Giro de 90/270° (camera.rotation): "metadata" marca la orientación en el MP4 sin recodificar
        # (instantáneo; lo respetan VLC, mpv, navegadores y móviles); "reencode" gira los píxeles con
        # ffmpeg (compatible con cualquier programa, pero lento y con mucha CPU).
        "rotate_mode": "metadata",
        "max_minutes": 30,
    },
    "music": {
        "enabled": True,
        "volume": 60,
        # Tope de volumen. Con la cancelación de eco, por encima de 70 la música satura el micrófono y
        # «Hey Vimo» se oye peor (docs/eco.md); el usuario prefirió 90 el 25-10-2026.
        "max_volume": 90,
        "volume_step": 10,
        "min_duration_seconds": 60,
        "max_duration_seconds": 900,
        "mix_size": 25,
        "ytdlp": "venv/bin/yt-dlp",
        "js_runtime": "node",
        "cache_dir": "data/music_cache",
        "mpv_args": ["--ao=pipewire"],
        "pause_while_talking": True,
        # Al empezar a decir «Hey Vimo» la música baja a este porcentaje de su volumen unos segundos.
        "duck_ratio": 0.3,
        "duck_seconds": 3.0,
        "download_timeout_seconds": 90,
        # Núcleos y prioridad para yt-dlp (descargas): limitar evita sumar picos de consumo.
        "ytdlp_cpu_cores": [3],
        "ytdlp_nice": 10,
    },
    "memory": {
        "enabled": True,
        "path": "data/conversation_memory.json",
        "max_turns": 50,
    },
    "gui": {
        "fullscreen": True,
        "show_text": True,
        # Pantalla «apagada» tras este tiempo en reposo (sin música ni grabación). 0 = nunca.
        "sleep_after_seconds": 120,
        "sleep_backlight": True,        # además del fondo negro, apaga la retroiluminación
        "backlight_path": None,         # None = la primera de /sys/class/backlight
    },
    "logging": {
        "level": "INFO",
        "file": "logs/vimo.log",
    },
}

# Reglas de validación: ruta -> (tipos aceptados, mínimo, máximo). None = sin límite.
_NUM = (int, float)
RULES: dict[str, tuple] = {
    "wake_word.threshold": (_NUM, 0.05, 0.99),
    "wake_word.sample_rate": ((int,), 16000, 16000),
    "wake_word.chunk_size": ((int,), 400, 16000),
    "wake_word.cooldown_seconds": (_NUM, 0.0, 30.0),
    "wake_word.resume_delay_seconds": (_NUM, 0.0, 10.0),
    "wake_word.patience_frames": ((int,), 1, 10),
    "wake_word.vad_threshold": (_NUM, 0.0, 0.95),
    "wake_word.duck_music_score": (_NUM, 0.0, 0.95),
    "wake_word.exact_gate": (_NUM, 0.01, 0.9),
    "wake_word.threads": ((int,), 1, 4),
    "audio.queue_max_chunks": ((int,), 5, 1000),
    "audio.effects_volume": (_NUM, 0.0, 1.0),
    "audio.speech_volume": (_NUM, 0.0, 1.0),
    "listening.max_seconds": (_NUM, 2.0, 60.0),
    "listening.silence_seconds": (_NUM, 0.3, 5.0),
    "listening.no_speech_timeout_seconds": (_NUM, 1.0, 30.0),
    "listening.min_speech_seconds": (_NUM, 0.1, 5.0),
    "listening.energy_threshold": (_NUM, 0.0005, 0.5),
    "listening.noise_multiplier": (_NUM, 1.0, 10.0),
    "stt.threads": ((int,), 1, 8),
    "stt.audio_ctx": ((int,), 0, 1500),
    "stt.server_port": ((int,), 1024, 65535),
    "stt.timeout_seconds": (_NUM, 5.0, 600.0),
    "claude.timeout_seconds": (_NUM, 10.0, 1800.0),
    "claude.session_max_turns": ((int,), 0, 1000),
    "web_search.max_results": ((int,), 1, 10),
    "web_search.timeout_seconds": ((int,), 2, 60),
    "tts.length_scale": (_NUM, 0.5, 2.0),
    "tts.max_spoken_chars": ((int,), 100, 5000),
    "tts.threads": ((int,), 1, 4),
    "camera.rotation": ((int,), 0, 270),
    "video.min_free_mb": ((int,), 50, 100000),
    "video.max_minutes": (_NUM, 1, 600),
    "memory.max_turns": ((int,), 1, 1000),
    "faces.music_switch_seconds": (_NUM, 3, 600),
    "faces.idle_gesture_min_seconds": (_NUM, 2, 600),
    "faces.idle_gesture_max_seconds": (_NUM, 2, 1200),
    "gui.sleep_after_seconds": (_NUM, 0, 86400),
    "music.volume": ((int,), 0, 100),
    "music.max_volume": ((int,), 10, 100),
    "music.duck_ratio": (_NUM, 0.0, 1.0),
    "music.duck_seconds": (_NUM, 0.5, 30.0),
    "music.volume_step": ((int,), 1, 50),
    "music.min_duration_seconds": (_NUM, 0, 3600),
    "music.max_duration_seconds": (_NUM, 60, 36000),
    "music.mix_size": ((int,), 5, 100),
    "music.download_timeout_seconds": (_NUM, 10, 600),
}
CHOICES: dict[str, tuple] = {
    "wake_word.engine": ("livekit",),
    "stt.engine": ("whisper.cpp",),
    "tts.engine": ("piper",),
    "camera.rotation": (0, 90, 180, 270),
    "video.rotate_mode": ("metadata", "reencode"),
    "logging.level": ("DEBUG", "INFO", "WARNING", "ERROR"),
}


def _get(d: dict, dotted: str) -> Any:
    for key in dotted.split("."):
        d = d[key]
    return d


def _set(d: dict, dotted: str, value: Any) -> None:
    keys = dotted.split(".")
    for key in keys[:-1]:
        d = d.setdefault(key, {})
    d[keys[-1]] = value


def _merge(defaults: dict, user: dict, path: str, warnings: list[str]) -> dict:
    out = copy.deepcopy(defaults)
    for key, value in user.items():
        full = f"{path}.{key}" if path else key
        if key.startswith("_"):  # comentarios ("_comentario": "...")
            out[key] = value
        elif key not in defaults:
            # Claves desconocidas se conservan (compatibilidad hacia delante) pero se avisa.
            warnings.append(f"Clave desconocida en config.json: {full}")
            out[key] = value
        elif isinstance(defaults[key], dict) and key != "profiles" and key != "frame_ms":
            if isinstance(value, dict):
                out[key] = _merge(defaults[key], value, full, warnings)
            else:
                warnings.append(f"{full} debe ser un objeto; se usa el valor predeterminado")
        else:
            out[key] = value
    return out


def validate(cfg: dict) -> list[str]:
    """Corrige en el sitio los valores inválidos y devuelve la lista de avisos."""
    warnings: list[str] = []
    for dotted, (types, lo, hi) in RULES.items():
        value = _get(cfg, dotted)
        default = _get(DEFAULTS, dotted)
        if isinstance(value, bool) or not isinstance(value, types):
            warnings.append(f"{dotted}={value!r} tiene un tipo inválido; se usa {default!r}")
            _set(cfg, dotted, default)
            continue
        if (lo is not None and value < lo) or (hi is not None and value > hi):
            warnings.append(f"{dotted}={value!r} fuera de rango [{lo}, {hi}]; se usa {default!r}")
            _set(cfg, dotted, default)
    for dotted, allowed in CHOICES.items():
        value = _get(cfg, dotted)
        if value not in allowed:
            default = _get(DEFAULTS, dotted)
            warnings.append(f"{dotted}={value!r} no es válido (opciones: {allowed}); se usa {default!r}")
            _set(cfg, dotted, default)
    profiles = cfg["video"].get("profiles")
    if not isinstance(profiles, dict) or not profiles:
        warnings.append("video.profiles inválido; se usan los perfiles predeterminados")
        cfg["video"]["profiles"] = copy.deepcopy(DEFAULTS["video"]["profiles"])
    for name, prof in list(cfg["video"]["profiles"].items()):
        ok = (
            isinstance(prof, dict)
            and isinstance(prof.get("size"), list)
            and len(prof["size"]) == 2
            and all(isinstance(v, int) and 64 <= v <= 8000 for v in prof["size"])
            and isinstance(prof.get("fps"), _NUM)
            and 1 <= prof["fps"] <= 120
        )
        if not ok:
            warnings.append(f"Perfil de vídeo {name!r} inválido; se descarta")
            del cfg["video"]["profiles"][name]
    if cfg["video"]["profile"] not in cfg["video"]["profiles"]:
        fallback = next(iter(cfg["video"]["profiles"]), None)
        if fallback is None:
            cfg["video"]["profiles"] = copy.deepcopy(DEFAULTS["video"]["profiles"])
            fallback = DEFAULTS["video"]["profile"]
        warnings.append(f"video.profile={cfg['video']['profile']!r} no existe; se usa {fallback!r}")
        cfg["video"]["profile"] = fallback
    if not isinstance(cfg["claude"].get("extra_args"), list) or not all(
        isinstance(a, str) for a in cfg["claude"]["extra_args"]
    ):
        warnings.append("claude.extra_args debe ser una lista de cadenas; se ignora")
        cfg["claude"]["extra_args"] = []
    ctx = cfg["stt"]["audio_ctx"]
    if ctx and cfg["listening"]["max_seconds"] > ctx * 30 / 1500 - 1:
        # Whisper solo mira audio_ctx/1500 × 30 s: el final de una consulta más larga se perdería.
        warnings.append(f"stt.audio_ctx={ctx} no cubre listening.max_seconds={cfg['listening']['max_seconds']}; "
                        "se usa el contexto completo (0)")
        cfg["stt"]["audio_ctx"] = 0
    allowed, rejected = _filter_claude_args(cfg["claude"]["extra_args"])
    if rejected:
        warnings.append("claude.extra_args solo admite " + ", ".join(sorted(CLAUDE_EXTRA_ARGS))
                        + "; se eliminan: " + " ".join(rejected))
        cfg["claude"]["extra_args"] = allowed
    return warnings


# Opciones de `claude -p` que se pueden añadir desde config.json (todas llevan un valor). Es una
# lista blanca: cualquier otra (--tools, --allowedTools, --add-dir, --settings, --mcp-config,
# --permission-mode, --dangerously-skip-permissions…) podría devolver a Claude el acceso a la
# terminal o a los archivos de la Raspberry Pi.
CLAUDE_EXTRA_ARGS = {"--model", "--fallback-model", "--effort"}


def _filter_claude_args(args: list[str]) -> tuple[list[str], list[str]]:
    allowed: list[str] = []
    rejected: list[str] = []
    i = 0
    while i < len(args):
        arg = args[i]
        name, sep, _value = arg.partition("=")
        if name in CLAUDE_EXTRA_ARGS and sep:
            allowed.append(arg)
        elif name in CLAUDE_EXTRA_ARGS and i + 1 < len(args) and not args[i + 1].startswith("-"):
            allowed += [arg, args[i + 1]]
            i += 1
        else:
            rejected.append(arg)
        i += 1
    return allowed, rejected


class ConfigManager:
    """Acceso seguro entre hilos a la configuración."""

    def __init__(self, path: str | None = None):
        self.path = path or os.path.join(PROJECT_ROOT, "config.json")
        self._lock = threading.RLock()
        self.warnings: list[str] = []
        self.data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.load()

    # -- carga / guardado ---------------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            warnings: list[str] = []
            user: dict[str, Any] = {}
            if os.path.exists(self.path):
                try:
                    with open(self.path, encoding="utf-8") as fh:
                        user = json.load(fh)
                    if not isinstance(user, dict):
                        raise ValueError("la raíz no es un objeto JSON")
                except (OSError, ValueError) as exc:
                    backup = f"{self.path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
                    try:
                        os.replace(self.path, backup)
                        warnings.append(f"config.json dañado ({exc}); copia guardada en {backup}")
                    except OSError:
                        warnings.append(f"config.json dañado ({exc}) y no se pudo respaldar")
                    user = {}
            else:
                warnings.append("config.json no existe; se crea con valores predeterminados")
            data = _merge(DEFAULTS, user, "", warnings)
            warnings.extend(validate(data))
            self.data = data
            self.warnings = warnings
            for w in warnings:
                log.warning(w)
            if not os.path.exists(self.path):
                self.save()

    def save(self) -> None:
        with self._lock:
            directory = os.path.dirname(os.path.abspath(self.path))
            fd, tmp = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=directory)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(self.data, fh, ensure_ascii=False, indent=2)
                    fh.write("\n")
                    fh.flush()
                    os.fsync(fh.fileno())
                os.chmod(tmp, 0o644)
                os.replace(tmp, self.path)
            except BaseException:
                if os.path.exists(tmp):
                    os.unlink(tmp)
                raise

    # -- acceso -------------------------------------------------------------------------
    def get(self, dotted: str, default: Any = None) -> Any:
        with self._lock:
            try:
                return copy.deepcopy(_get(self.data, dotted))
            except (KeyError, TypeError):
                return default

    def section(self, name: str) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self.data.get(name, {}))

    def update(self, changes: dict[str, Any], persist: bool = True) -> list[str]:
        """Aplica cambios {"seccion.clave": valor}, valida y opcionalmente guarda."""
        with self._lock:
            candidate = copy.deepcopy(self.data)
            for dotted, value in changes.items():
                _set(candidate, dotted, value)
            warnings = validate(candidate)
            self.data = candidate
            if persist:
                self.save()
            return warnings

    def path_for(self, dotted: str) -> str | None:
        """Resuelve una ruta de la configuración (~ y relativa a la raíz del proyecto)."""
        value = self.get(dotted)
        return resolve_path(value) if value else None


def resolve_path(value: str, base: str = PROJECT_ROOT) -> str:
    value = os.path.expanduser(value)
    return value if os.path.isabs(value) else os.path.join(base, value)
