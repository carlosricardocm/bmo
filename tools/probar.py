"""Pruebas manuales de cada componente de Vimo, por separado y con los mismos módulos que la app.

    venv/bin/python tools/probar.py microfono      # nivel del micrófono durante 5 s
    venv/bin/python tools/probar.py wake           # puntuación de la wake word en vivo (30 s)
    venv/bin/python tools/probar.py escuchar       # graba una frase (VAD) y la transcribe con Whisper
    venv/bin/python tools/probar.py sonido         # reproduce un sonido corto original (ack)
    venv/bin/python tools/probar.py voz ["texto"]  # sintetiza con Piper y lo reproduce
    venv/bin/python tools/probar.py claude ["pregunta"]
    venv/bin/python tools/probar.py busqueda ["consulta"]
    venv/bin/python tools/probar.py foto
    venv/bin/python tools/probar.py video [segundos] [perfil]
    venv/bin/python tools/probar.py musica ["canción" | género]   # 20 s y salto a la siguiente

Ordenadas de menor a mayor consumo: microfono, wake, claude, busqueda < sonido < voz, escuchar
(Whisper) < foto < video. Con problemas de alimentación, avanza de una en una.
"""
from __future__ import annotations

import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import numpy as np  # noqa: E402

from app.config_manager import ConfigManager, resolve_path  # noqa: E402

cfg = ConfigManager()


def mic():
    from app.audio_manager import MicrophoneStream
    a = cfg.section("audio")
    m = MicrophoneStream(a["input_device"], a["input_sample_rate"], 1280, a["queue_max_chunks"])
    m.start()
    return m


def cmd_microfono() -> None:
    from app.audio_manager import rms
    m = mic()
    print(f"Micrófono: {m.device_name} a {m.rate} Hz. Habla durante 5 s…")
    levels = []
    m.add_sink(lambda c: levels.append(rms(c)))
    t0 = time.monotonic()
    while time.monotonic() - t0 < 5:
        time.sleep(0.25)
        if levels:
            v = levels[-1]
            print(f"  {v:.4f} {'█' * min(60, int(v * 600))}")
    m.stop()
    if not levels:
        print("✗ No llegó audio del micrófono")
        return
    print(f"✓ {len(levels)} bloques; nivel mín {min(levels):.4f} (ruido), máx {max(levels):.4f}. "
          f"Umbral de voz configurado: {cfg.get('listening.energy_threshold')}")


def cmd_wake() -> None:
    from app.wake_word import WakeWordDetector
    w = cfg.section("wake_word")
    det = WakeWordDetector(cfg.path_for("wake_word.model_path"), cfg.path_for("wake_word.melspec_model_path"),
                           cfg.path_for("wake_word.embedding_model_path"), w["threshold"], w["cooldown_seconds"], 0)
    det.load()
    hits = []
    det.on_detect = lambda s: (hits.append(s), print(f"  ★ DETECTADA (puntuación {s:.2f})"))
    m = mic()
    m.add_sink(det.process)
    print(f"Di «{w['phrase']}» varias veces durante 30 s (umbral {w['threshold']})…")
    best = 0.0
    t0 = time.monotonic()
    while time.monotonic() - t0 < 30:
        time.sleep(0.5)
        best = max(best, det.last_score)
        print(f"  puntuación actual {det.last_score:.2f}   máxima {best:.2f}", end="\r")
    m.stop()
    print(f"\n{'✓' if hits else '✗'} {len(hits)} detecciones; puntuación máxima {best:.2f}; "
          f"inferencia media {det.mean_inference_ms():.1f} ms por bloque")


def cmd_escuchar() -> None:
    from app.speech_to_text import NoiseTracker, UtteranceRecorder, WhisperTranscriber
    s = cfg.section("stt")
    stt = WhisperTranscriber(os.path.expanduser(s["whisper_dir"]), s["server_binary"], s["cli_binary"], s["model"],
                             s["language"], s["threads"], use_server=False, timeout=s["timeout_seconds"])
    stt.check()
    noise = NoiseTracker()
    m = mic()
    m.add_sink(noise)
    print("Midiendo ruido ambiente 1 s (silencio, por favor)…")
    time.sleep(1)
    print(f"Ruido {noise.level:.4f}. Ahora di una frase; termina cuando te calles ~1 s.")
    utt = UtteranceRecorder(m, noise, cfg.section("listening")).record(threading.Event())
    m.stop()
    print(f"  grabado {utt.duration:.1f} s, voz {utt.speech_seconds:.1f} s, fin: {utt.reason}")
    if not utt.has_speech:
        print("✗ No se detectó voz (sube el volumen o acércate)")
        return
    print(f"Transcribiendo con {os.path.basename(stt.model)} (whisper-cli)…")
    t0 = time.monotonic()
    text = stt.transcribe(utt.audio)
    print(f"{'✓' if text else '✗'} «{text}»  ({time.monotonic() - t0:.1f} s)")


def player():
    from app.audio_manager import AudioPlayer
    a = cfg.section("audio")
    p = AudioPlayer(a["output_device"], a["output_sample_rate"])
    p.open()
    return p


def cmd_sonido() -> None:
    from app.audio_manager import SoundBank
    s = cfg.section("sounds")
    base = resolve_path(s["directory"])
    bank = SoundBank(os.path.join(base, s["language_subdir"]) if s["language_subdir"] else base)
    name, audio, rate = bank.pick("ack_sounds")
    p = player()
    print(f"Reproduciendo {name} ({len(audio) / rate:.1f} s) por el dispositivo {p.device} a {p.rate} Hz…")
    p.play(audio, rate, volume=cfg.get("audio.effects_volume"), block=True)
    print("✓ Reproducción terminada (¿se oyó?)")


def cmd_voz(text: str | None) -> None:
    from app.text_to_speech import PiperTTS, split_sentences
    t = cfg.section("tts")
    tts = PiperTTS(resolve_path(t["voice_model"]), resolve_path(t["voice_config"]) if t["voice_config"] else None,
                   t["length_scale"], t["noise_scale"], t["noise_w_scale"], t["speaker_id"])
    print("Cargando la voz de Piper…")
    tts.load()
    text = text or "Hola, soy Vimo. Esta es una prueba de mi voz."
    stats: dict = {}
    p = player()
    done = threading.Event()
    t0 = time.monotonic()
    p.play_stream(tts.stream(split_sentences(text), threading.Event(), stats), on_done=lambda i: done.set())
    done.wait(120)
    print(f"✓ Primera frase lista en {stats.get('first_segment_seconds', 0):.2f} s; síntesis total "
          f"{stats.get('synth_seconds', 0):.2f} s; reproducción completa en {time.monotonic() - t0:.1f} s")


def claude_client():
    from app.claude_code_client import ClaudeCodeClient
    c = ClaudeCodeClient(cfg.section("claude"), cfg.get("agent.personality"),
                         os.path.join(ROOT, "data", "claude_workspace"), os.path.join(ROOT, "data"), cfg.path)
    c.write_mcp_config()
    print("Claude Code:", c.check().get("version"))
    return c


def cmd_claude(question: str | None) -> None:
    c = claude_client()
    question = question or "Hola Vimo, preséntate en una frase."
    t0 = time.monotonic()
    r = c.submit(question, lambda k, d: print(f"  … {k}: {d}")).result()
    print(f"✓ ({time.monotonic() - t0:.1f} s) {r.text}")
    for s in r.sources[:5]:
        print(f"   fuente: {s['title'][:60]} — {s['url']}")
    c.shutdown()


def cmd_busqueda(query: str | None) -> None:
    from app.web_search import WebSearch
    w = cfg.section("web_search")
    ws = WebSearch(w["region"], w["safesearch"], w["max_results"], w["timeout_seconds"],
                   state_path=os.path.join(ROOT, "data", "search_state.json"))
    query = query or "noticias Raspberry Pi"
    t0 = time.monotonic()
    results = ws.search(query, max_results=5)
    print(f"✓ {len(results)} resultados de DuckDuckGo en {time.monotonic() - t0:.1f} s")
    for r in results:
        print(f"   {r.title[:70]}\n     {r.url}")


def cmd_foto() -> None:
    from app.camera_manager import CameraManager
    c = cfg.section("camera")
    cam = CameraManager(resolve_path(c["photo_dir"]), tuple(c["photo_resolution"]), c["rotation"])
    if not cam.detect():
        print("✗", cam.error)
        return
    path = cam.take_photo()
    print(f"✓ Foto guardada: {path} ({os.path.getsize(path) // 1024} KB)")


def cmd_video(seconds: float, profile: str | None) -> None:
    from app.camera_manager import CameraManager
    from app.video_recorder import VideoRecorder
    c, v = cfg.section("camera"), cfg.section("video")
    cam = CameraManager(resolve_path(c["photo_dir"]), tuple(c["photo_resolution"]), c["rotation"])
    if not cam.detect():
        print("✗", cam.error)
        return
    rec = VideoRecorder(cam, resolve_path(v["directory"]), v["profiles"], profile or "720p30", v["min_free_mb"])
    print(f"Grabando {seconds:.0f} s con el perfil {profile or '720p30'}…", rec.start())
    time.sleep(seconds)
    result = rec.stop()
    print(f"✓ {result.describe()}  (códec {result.codec}, {result.frames} fotogramas)")
    print(f"   Reprodúcelo con: ffplay '{result.path}'   o   vlc '{result.path}'")


def cmd_musica(query: str | None) -> None:
    from app.music_player import MpvBackend, MusicPlayer, YouTubeSource
    m = cfg.section("music")
    src = YouTubeSource(resolve_path(m["ytdlp"]), resolve_path(m["cache_dir"]), m["js_runtime"] or None,
                        m["download_timeout_seconds"])
    be = MpvBackend(os.path.join(ROOT, "data", "mpv.sock"), list(m["mpv_args"]))
    print("yt-dlp", src.check(), "·", be.check().split(" Copyright")[0])
    p = MusicPlayer(src, be, m["volume"], m["volume_step"], m["min_duration_seconds"], m["max_duration_seconds"],
                    m["mix_size"])
    try:
        from app.intents import normalize
        from app.music_genres import match_genre
        query = query or "Soda Stereo De música ligera"
        genre = match_genre(normalize(query))
        t0 = time.monotonic()
        if genre:
            seeds = p.find_genre(genre)
            track = seeds[0]
            print(f"Género «{genre.label}»: {len(seeds)} canciones en {time.monotonic() - t0:.1f} s")
        else:
            seeds = None
            track = p.find(query)
            print(f"Encontrada en {time.monotonic() - t0:.1f} s: {track.label()}")
        t0 = time.monotonic()
        p.play(track, genre=genre, seeds=seeds[1:] if seeds else None)
        print(f"✓ Sonando tras {time.monotonic() - t0:.1f} s (volumen {p.volume} %). Escuchando 20 s…")
        time.sleep(20)
        print(f"  cola de similares: {len(p.queue)} → " + ", ".join(t.title[:28] for t in p.queue[:4]))
        prev = p.current.id
        t0 = time.monotonic()
        p.next()
        while p.current and p.current.id == prev and time.monotonic() - t0 < 60:
            time.sleep(0.2)
        print(f"✓ Siguiente en {time.monotonic() - t0:.1f} s: {p.current.label() if p.current else '—'}")
        time.sleep(8)
    finally:
        p.shutdown()
        print("Música detenida y caché vaciada")


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        return 0
    what, rest = sys.argv[1], sys.argv[2:]
    arg = " ".join(rest) or None
    commands = {
        "microfono": cmd_microfono, "wake": cmd_wake, "escuchar": cmd_escuchar, "sonido": cmd_sonido,
        "voz": lambda: cmd_voz(arg), "claude": lambda: cmd_claude(arg), "busqueda": lambda: cmd_busqueda(arg),
        "foto": cmd_foto, "musica": lambda: cmd_musica(arg),
        "video": lambda: cmd_video(float(rest[0]) if rest else 5, rest[1] if len(rest) > 1 else None),
    }
    if what not in commands:
        print(f"Prueba desconocida: {what}\n{__doc__}")
        return 2
    try:
        commands[what]()
    except Exception as exc:
        print(f"✗ {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
