"""Graba frases reales con tu voz para evaluar y ajustar Whisper (tools/evaluar_whisper.py).

Muestra cada frase, pulsas Enter y la dices como se la dirías a Vimo justo después de «Hey Vimo»
(sin decir «Hey Vimo»). La grabación termina sola tras ~1 s de silencio, con el mismo detector
que usa Vimo. Usa el mismo micrófono que Vimo: ciérralo antes.

    venv/bin/python tools/grabar_frases_stt.py                  # todas las frases
    venv/bin/python tools/grabar_frases_stt.py --etiqueta musica  # otra tanda (p. ej. con música)
    venv/bin/python tools/grabar_frases_stt.py --rehacer        # vuelve a grabar las ya hechas

Teclas en cada frase: Enter = grabar · s = saltar · q = salir (lo grabado se conserva).
Tras grabar: Enter = siguiente · r = repetir · q = salir.

Se guarda en data/stt_eval/<etiqueta>/ (WAV de 16 kHz + frases.json), fuera de git.
Si sales a medias, al volver a ejecutarlo continúa por la primera frase sin grabar.
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import numpy as np  # noqa: E402

from app.audio_manager import AudioUnavailable, MicrophoneStream, find_device, refresh_devices, rms, write_wav  # noqa: E402
from app.config_manager import ConfigManager, resolve_path  # noqa: E402
from app.echo_cancel import ALSA_IN, EchoCanceller, find_source  # noqa: E402
from app.speech_to_text import EndpointDetector, NoiseTracker  # noqa: E402

OUT = os.path.join(ROOT, "data", "stt_eval")
SR = 16000

# (categoría, frase). Incluye las que Whisper ya confundió en el log («pon la luz cálida» →
# «Buena luz, calidad», «modo fiesta» → «Bolo Fiesta», «reproduce» → «el producto»…) y preguntas
# normales para comprobar que un ajuste no empeora el resto.
PHRASES: list[tuple[str, str]] = [
    ("luces", "Prende las luces."),
    ("luces", "Apaga la luz."),
    ("luces", "Pon la luz cálida."),
    ("luces", "Pon la luz fría."),
    ("luces", "Pon la luz roja."),
    ("luces", "Cambia la lámpara izquierda a azul."),
    ("luces", "Apaga la lámpara derecha."),
    ("luces", "Prende la de la izquierda."),
    ("luces", "Pon la luz al cincuenta por ciento."),
    ("luces", "Sube la luz."),
    ("luces", "Baja la luz un poco."),
    ("luces", "Modo fiesta."),
    ("luces", "Pon el modo lectura."),
    ("luces", "Modo noche."),
    ("luces", "Pon la luz blanca."),
    ("musica", "Reproduce The Blaze."),
    ("musica", "Reproduce José José."),
    ("musica", "Pon música de Soda Stereo."),
    ("musica", "Para la música."),
    ("musica", "Pausa la música."),
    ("musica", "Siguiente canción."),
    ("musica", "¿Qué canción es esta?"),
    ("volumen", "Sube el volumen."),
    ("volumen", "Baja el volumen."),
    ("volumen", "Pon el volumen al noventa."),
    ("otros", "Toma una foto."),
    ("otros", "Graba un video."),
    ("otros", "Empecemos una nueva conversación."),
    ("pregunta", "¿Cuál es la capital de México?"),
    ("pregunta", "¿Qué tiempo va a hacer mañana?"),
    ("pregunta", "Explícame cómo funciona un motor eléctrico."),
    ("pregunta", "¿Cuántos habitantes tiene Guadalajara?"),
    ("pregunta", "Recuérdame qué hablamos ayer."),
]


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


def ask(prompt: str) -> str:
    try:
        return input(prompt).strip().lower()
    except EOFError:
        return "q"


def record(mic_q: queue.Queue, listening: dict, noise: NoiseTracker) -> tuple[np.ndarray, str, float]:
    """Graba hasta el silencio final, como Vimo (mismo umbral adaptado al ruido de la habitación).
    Devuelve (audio, motivo de fin, nivel máximo)."""
    while not mic_q.empty():  # descarta lo anterior a pulsar Enter
        mic_q.get_nowait()
    threshold = max(listening["energy_threshold"], noise.level * listening["noise_multiplier"])
    noise.paused = True
    ep = EndpointDetector(threshold, listening["silence_seconds"],
                          listening["max_seconds"], listening["no_speech_timeout_seconds"])
    peak = 0.0
    while True:
        chunk = mic_q.get(timeout=5)
        level = rms(chunk)
        peak = max(peak, level)
        print(f"\r   🎙  {'█' * min(30, int(level * 300)):<30} {ep.elapsed:4.1f} s", end="", flush=True)
        if ep.feed(chunk):
            break
    print()
    noise.paused = False
    utt = ep.result()
    return utt.audio, utt.reason, peak


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--etiqueta", default="normal", help="nombre de la tanda (carpeta)")
    ap.add_argument("--rehacer", action="store_true", help="volver a grabar también las ya grabadas")
    ap.add_argument("--sin-eco", action="store_true", help="micrófono directo, sin la cancelación de eco de Vimo")
    args = ap.parse_args()

    if vimo_running():
        print("Vimo está abierto y usa el micrófono. Ciérralo y vuelve a ejecutar esto.")
        return 1
    cfg = ConfigManager()
    listening = cfg.section("listening")
    folder = os.path.join(OUT, args.etiqueta)
    os.makedirs(folder, exist_ok=True)
    index_path = os.path.join(folder, "frases.json")
    try:
        with open(index_path, encoding="utf-8") as fh:
            index = {e["file"]: e for e in json.load(fh)}
    except FileNotFoundError:
        index = {}

    def save_index():
        tmp = index_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(sorted(index.values(), key=lambda e: e["file"]), fh, indent=2, ensure_ascii=False)
        os.replace(tmp, index_path)

    mic_q: queue.Queue = queue.Queue(maxsize=400)
    a = cfg.section("audio")
    # Mismo camino de audio que Vimo: micrófono → cancelación de eco (filtro de graves, supresión
    # de golpes) → 16 kHz, para que Whisper reciba lo mismo que en el uso real.
    aec, device = None, a["input_device"]
    e = a["echo_cancel"]
    if e["enabled"] and not args.sin_eco:
        try:
            aec = EchoCanceller(resolve_path("data/aec/vimo_aec.conf"),
                                e["capture_target"] or find_source(a["input_device"]), e["playback_target"],
                                e["aec_args"], resolve_path("logs/aec.log"))
            aec.start()
            for _ in range(5):
                refresh_devices()
                try:
                    find_device(ALSA_IN, "input")
                    device = ALSA_IN
                    break
                except AudioUnavailable:
                    time.sleep(1.0)
        except Exception as exc:
            print(f"Sin cancelación de eco ({exc}); se usa el micrófono directo.")
    noise = NoiseTracker()
    mic = MicrophoneStream(device, a["input_sample_rate"], 1280, a["queue_max_chunks"])
    mic.start()
    mic.add_sink(noise)
    mic.add_sink(lambda c: mic_q.put_nowait(c) if not mic_q.full() else None)
    print("Midiendo el ruido de la habitación (2 s, no hables)…")
    time.sleep(2.0)
    print(f"Micrófono: {mic.device_name} (ruido {noise.level:.4f}). Se guarda en {folder}\n")
    print("Di cada frase con tu voz normal, a la distancia a la que sueles hablarle a Vimo.")
    print("Enter = grabar · s = saltar · q = salir\n")

    done = 0
    try:
        for i, (category, text) in enumerate(PHRASES, 1):
            name = f"{i:02d}.wav"
            if name in index and not args.rehacer:
                continue
            while True:
                key = ask(f"[{i}/{len(PHRASES)}] ({category})  «{text}»  → Enter para grabar: ")
                if key == "q":
                    raise KeyboardInterrupt
                if key == "s":
                    break
                audio, reason, peak = record(mic_q, listening, noise)
                if reason == "no_speech" or len(audio) == 0:
                    print("   No te oí. Habla un poco más fuerte o más cerca e inténtalo otra vez.")
                    continue
                note = ""
                if peak < 0.03:
                    note = " (muy bajo: acércate al micrófono)"
                elif reason == "max_duration":
                    note = " (se cortó por tiempo máximo)"
                print(f"   Grabado: {len(audio) / SR:.1f} s{note}")
                key = ask("   Enter = siguiente · r = repetir · q = salir: ")
                if key == "r":
                    continue
                write_wav(os.path.join(folder, name), audio, SR)
                index[name] = {"file": name, "text": text, "category": category,
                               "seconds": round(len(audio) / SR, 2), "end": reason}
                save_index()
                done += 1
                if key == "q":
                    raise KeyboardInterrupt
                break
    except KeyboardInterrupt:
        print("\nSaliendo…")
    finally:
        mic.stop()
        if aec:
            aec.stop()
    print(f"\nGrabadas en esta sesión: {done}. Total en {folder}: {len(index)}/{len(PHRASES)}.")
    if len(index) < len(PHRASES):
        print("Vuelve a ejecutar el mismo comando para continuar con las que faltan.")
    else:
        print("¡Listo! Avísame y comparo las configuraciones de Whisper con tu voz.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
