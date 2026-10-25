"""Graba muestras reales para evaluar y ajustar la wake word («Hey Vimo»).

Usa el mismo micrófono y el mismo procesado que Vimo (captura del dispositivo configurado,
remuestreo a 16 kHz mono) y guarda WAV de 16 kHz en data/muestras_wake/. Cierra Vimo antes: el
micrófono solo puede usarlo un programa a la vez.

    # Positivas: te pide que digas la frase N veces (cuenta atrás en pantalla)
    venv/bin/python tools/grabar_muestras.py positivas --n 20 --etiqueta cerca
    venv/bin/python tools/grabar_muestras.py positivas --n 15 --etiqueta 2m
    venv/bin/python tools/grabar_muestras.py positivas --n 15 --etiqueta musica --con-musica rock

    # Negativas: graba la habitación (conversación, tele, música…) en trozos de 30 s
    venv/bin/python tools/grabar_muestras.py negativas --minutos 10 --etiqueta tele
    venv/bin/python tools/grabar_muestras.py negativas --minutos 5 --etiqueta musica --con-musica techno

Después: venv/bin/python tools/evaluar_wake_word.py

Privacidad: las negativas graban el sonido de la habitación. Se quedan en esta Raspberry Pi
(data/muestras_wake/, excluido de git) y puedes borrarlas cuando quieras.
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import numpy as np  # noqa: E402

from app.audio_manager import MicrophoneStream, rms, write_wav  # noqa: E402
from app.config_manager import ConfigManager, resolve_path  # noqa: E402
from app.wake_word import WakeWordDetector  # noqa: E402

OUT = os.path.join(ROOT, "data", "muestras_wake")
SR = 16000


def vimo_running() -> bool:
    import fcntl
    path = os.path.join(ROOT, "data", "vimo.lock")
    if not os.path.exists(path):
        return False
    with open(path) as fh:
        try:
            fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(fh, fcntl.LOCK_UN)
            return False
        except BlockingIOError:
            return True


class Recorder:
    """Micrófono + modelo actual: acumula audio y la puntuación en directo."""

    def __init__(self, cfg: ConfigManager):
        a, w = cfg.section("audio"), cfg.section("wake_word")
        self.mic = MicrophoneStream(a["input_device"], a["input_sample_rate"], 1280, a["queue_max_chunks"])
        self.det = WakeWordDetector(resolve_path(w["model_path"]), resolve_path(w["melspec_model_path"]),
                                    resolve_path(w["embedding_model_path"]), threshold=w["threshold"],
                                    cooldown_seconds=w["cooldown_seconds"], resume_delay_seconds=0,
                                    patience_frames=w["patience_frames"],
                                    vad_model_path=resolve_path(w["vad_model_path"]),
                                    vad_threshold=w["vad_threshold"], exact_gate=w["exact_gate"],
                                    threads=w["threads"])
        self.det.load()
        self.lock = threading.Lock()
        self.chunks: list[np.ndarray] = []
        self.max_score = 0.0
        self.detections = 0
        self.det.on_detect = self._hit

    def _hit(self, score):
        self.detections += 1

    def _sink(self, chunk):
        self.det.process(chunk)
        with self.lock:
            self.chunks.append(chunk)
            self.max_score = max(self.max_score, self.det.last_score)

    def start(self):
        self.mic.start()
        self.mic.add_sink(self._sink)

    def take(self) -> tuple[np.ndarray, float, int]:
        """Devuelve el audio acumulado, la puntuación máxima y las activaciones, y reinicia."""
        with self.lock:
            audio = np.concatenate(self.chunks) if self.chunks else np.zeros(0, np.int16)
            out = (audio, self.max_score, self.detections)
            self.chunks, self.max_score, self.detections = [], 0.0, 0
        return out

    def stop(self):
        self.mic.stop()


def start_music(cfg: ConfigManager, query: str):
    from app.intents import normalize
    from app.music_genres import match_genre
    from app.music_player import MpvBackend, MusicPlayer, YouTubeSource
    m = cfg.section("music")
    player = MusicPlayer(YouTubeSource(resolve_path(m["ytdlp"]), resolve_path(m["cache_dir"]), m["js_runtime"],
                                       m["download_timeout_seconds"]),
                         MpvBackend(os.path.join(ROOT, "data", "mpv.sock"), list(m["mpv_args"])), m["volume"])
    print(f"Poniendo música («{query}», volumen {m['volume']} %)…")
    genre = match_genre(normalize(query))
    if genre:
        seeds = player.find_genre(genre)
        player.play(seeds[0], genre=genre, seeds=seeds[1:])
    else:
        player.play(player.find(query))
    print(f"♪ {player.current.label()}")
    time.sleep(2)
    return player


def bar(level: float) -> str:
    return "█" * min(30, int(level * 300))


def record_positives(rec: Recorder, n: int, tag: str, phrase: str, seconds: float) -> None:
    folder = os.path.join(OUT, "positivas")
    os.makedirs(folder, exist_ok=True)
    print(f"\nVas a decir «{phrase}» {n} veces. Habla como le hablarías a Vimo.")
    print("Tras «¡Ahora!» tienes ~2 s. Ctrl+C para terminar antes.\n")
    detected = 0
    for i in range(1, n + 1):
        for c in ("3", "2", "1"):
            print(f"  [{i}/{n}] {c}…", end="\r", flush=True)
            time.sleep(0.6)
        rec.take()  # descarta lo anterior a la cuenta atrás
        print(f"  [{i}/{n}] ¡Ahora! di «{phrase}»      ", end="\r", flush=True)
        time.sleep(seconds)
        audio, score, hits = rec.take()
        path = os.path.join(folder, f"{tag}_{time.strftime('%Y%m%d-%H%M%S')}_{i:02d}.wav")
        write_wav(path, audio, SR)
        detected += hits > 0
        ok = "✓ DETECTADA" if hits else ("· casi" if score >= 0.2 else "✗ no detectada")
        print(f"  [{i}/{n}] puntuación máx {score:.2f}  {ok:15} nivel {bar(rms(audio))}")
        time.sleep(0.8)
    print(f"\nDetectadas en directo: {detected}/{n} (con la configuración actual). Guardadas en {folder}")


def record_negatives(rec: Recorder, minutes: float, tag: str, chunk_seconds: float = 30) -> None:
    folder = os.path.join(OUT, "negativas")
    os.makedirs(folder, exist_ok=True)
    print(f"\nGrabando {minutes:g} min de sonido de la habitación SIN decir «Hey Vimo».")
    print("Habla, pon la tele o música con normalidad. Ctrl+C para terminar antes.\n")
    total = minutes * 60
    t0 = time.monotonic()
    false_hits = 0
    part = 0
    try:
        while time.monotonic() - t0 < total:
            time.sleep(min(chunk_seconds, total - (time.monotonic() - t0)))
            audio, score, hits = rec.take()
            part += 1
            false_hits += hits
            write_wav(os.path.join(folder, f"{tag}_{time.strftime('%Y%m%d-%H%M%S')}_{part:03d}.wav"), audio, SR)
            el = time.monotonic() - t0
            warn = f"  ⚠ {hits} activación(es) falsa(s)" if hits else ""
            print(f"  {int(el) // 60:02d}:{int(el) % 60:02d}  trozo {part}: puntuación máx {score:.2f}{warn}")
    except KeyboardInterrupt:
        audio, _, hits = rec.take()
        false_hits += hits
        if len(audio) > SR:
            part += 1
            write_wav(os.path.join(folder, f"{tag}_{time.strftime('%Y%m%d-%H%M%S')}_{part:03d}.wav"), audio, SR)
    mins = (time.monotonic() - t0) / 60
    print(f"\n{part} trozos ({mins:.1f} min) en {folder}. Activaciones falsas en directo: {false_hits}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("modo", choices=["positivas", "negativas"])
    ap.add_argument("--n", type=int, default=20, help="repeticiones (positivas)")
    ap.add_argument("--segundos", type=float, default=2.2, help="duración de cada positiva")
    ap.add_argument("--minutos", type=float, default=10, help="duración total (negativas)")
    ap.add_argument("--etiqueta", default="general", help="p. ej. cerca, 2m, 4m, bajo, tele, musica")
    ap.add_argument("--con-musica", metavar="CONSULTA", help="reproduce música mientras graba (canción o género)")
    args = ap.parse_args()
    if vimo_running():
        print("Vimo está abierto y usa el micrófono. Ciérralo y vuelve a ejecutar esta herramienta.")
        return 1
    tag = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in args.etiqueta)[:30]
    cfg = ConfigManager()
    rec = Recorder(cfg)
    player = start_music(cfg, args.con_musica) if args.con_musica else None
    try:
        rec.start()
        print(f"Micrófono: {rec.mic.device_name} · modelo {rec.det.name} (umbral {rec.det.threshold})")
        time.sleep(0.5)
        rec.take()
        if args.modo == "positivas":
            record_positives(rec, args.n, tag, cfg.get("wake_word.phrase"), args.segundos)
        else:
            record_negatives(rec, args.minutos, tag)
    except KeyboardInterrupt:
        print("\nInterrumpido.")
    finally:
        rec.stop()
        if player:
            player.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
