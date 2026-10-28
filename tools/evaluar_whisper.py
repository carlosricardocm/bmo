"""Compara configuraciones de Whisper con frases reales grabadas (tools/grabar_frases_stt.py).

Para cada configuración (modelo, prompt de vocabulario, búsqueda en haz) transcribe todas las
frases con whisper-server, igual que Vimo, y mide:
  - órdenes correctas: Vimo haría lo mismo con la transcripción que con la frase real (luces,
    música, volumen… o «se la paso a Claude» para las preguntas). Es la métrica principal.
  - WER: porcentaje de palabras mal transcritas.
  - tiempo medio y máximo por frase.

Vigila la Raspberry Pi: si la entrada de 5 V baja de 4,85 V o la CPU pasa de 85 °C, corta. Antes de
cada frase espera a que la CPU baje de 72 °C (en el uso real Vimo trabaja a ráfagas; sin esa pausa
la Pi se calienta, baja su velocidad y los tiempos medidos no serían los reales).

    venv/bin/python tools/evaluar_whisper.py                     # todas las configuraciones
    venv/bin/python tools/evaluar_whisper.py --solo small         # solo las de un modelo
    venv/bin/python tools/evaluar_whisper.py --tanda musica
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import unicodedata
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

from app import intents  # noqa: E402
from app.audio_manager import read_wav  # noqa: E402
from app.config_manager import DEFAULTS, ConfigManager, resolve_path  # noqa: E402
from app.smart_lights import SmartLights  # noqa: E402
from app.speech_to_text import clean_transcript, wav_bytes  # noqa: E402

PORT = 8179
LIMIT_VOLT, LIMIT_TEMP = 4.85, 85.0
COOL_TEMP = 72.0

# Vocabulario que se le da a Whisper como contexto previo (el de Vimo; no son las frases de la prueba).
PROMPT = DEFAULTS["stt"]["prompt"]

CONFIGS = [
    # (nombre, modelo, campos extra de la petición)
    ("small actual", "small", {}),
    ("small + prompt", "small", {"prompt": PROMPT}),
    ("small + haz 5", "small", {"beam_size": "5"}),
    ("small + prompt + haz 5", "small", {"prompt": PROMPT, "beam_size": "5"}),
    ("medium actual", "medium", {}),
    ("medium + prompt", "medium", {"prompt": PROMPT}),
]
MODELS = {"small": "models/ggml-small-q5_1.bin", "medium": "models/ggml-medium-q5_0.bin"}


def vc(*args) -> str:
    return subprocess.run(["vcgencmd", *args], capture_output=True, text=True).stdout


def reading() -> tuple[float, float]:
    temp = float(re.search(r"([\d.]+)", vc("measure_temp")).group(1))
    volt = float(re.search(r"EXT5V_V volt\(\d+\)=([\d.]+)V", vc("pmic_read_adc")).group(1))
    return temp, volt


def watchdog(proc, stop, tripped, peak):
    while not stop.is_set() and proc.poll() is None:
        t, v = reading()
        peak["temp"], peak["volt"] = max(peak["temp"], t), min(peak["volt"], v)
        if t > LIMIT_TEMP or v < LIMIT_VOLT:
            tripped.append(f"{t:.1f} °C, 5 V {v:.3f} V")
            proc.kill()
            return
        time.sleep(0.3)


def norm_words(text: str) -> list[str]:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"[^\w\s]", " ", text).split()


def wer(ref: str, hyp: str) -> tuple[int, int]:
    r, h = norm_words(ref), norm_words(hyp)
    d = list(range(len(h) + 1))
    for i in range(1, len(r) + 1):
        prev, d[0] = d[0], i
        for j in range(1, len(h) + 1):
            cur = min(d[j] + 1, d[j - 1] + 1, prev + (r[i - 1] != h[j - 1]))
            prev, d[j] = d[j], cur
    return d[len(h)], len(r)


def decide(text: str, lights: SmartLights):
    """Lo que haría Vimo con este texto (sin ejecutarlo)."""
    text = intents.strip_wake_phrase(text)
    if not text:
        return ("nada",)
    light = lights.parser.parse(text)
    if light:
        return ("luz", light.action, tuple(light.targets), light.value)
    intent = intents.parse(text)
    if intent:
        return ("orden", intent.name, intent.arg)
    return ("claude",)


def ask(fields: dict, wav: bytes) -> str:
    b = uuid.uuid4().hex
    body = io.BytesIO()
    for k, v in fields.items():
        body.write(f"--{b}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n{v}\r\n".encode())
    body.write(f"--{b}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"q.wav\"\r\n"
               f"Content-Type: audio/wav\r\n\r\n".encode())
    body.write(wav)
    body.write(f"\r\n--{b}--\r\n".encode())
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}/inference", data=body.getvalue(),
                                 headers={"Content-Type": f"multipart/form-data; boundary={b}"})
    return json.loads(urllib.request.urlopen(req, timeout=180).read())["text"]


def run_model(model: str, configs: list, clips: list, stt: dict, lights: SmartLights, out: list) -> bool:
    wdir = os.path.expanduser(stt["whisper_dir"])
    cmd = [os.path.join(wdir, stt["server_binary"]), "-m", os.path.join(wdir, MODELS[model]), "-l", stt["language"],
           "-t", str(stt["threads"]), "--host", "127.0.0.1", "--port", str(PORT), "-nt"]
    if stt["audio_ctx"]:
        cmd += ["-ac", str(stt["audio_ctx"])]
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    stop, tripped, peak = threading.Event(), [], {"temp": 0.0, "volt": 9.9}
    threading.Thread(target=watchdog, args=(proc, stop, tripped, peak), daemon=True).start()
    try:
        for _ in range(300):
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", PORT)) == 0:
                    break
            time.sleep(0.3)
        base = {"response_format": "json", "temperature": "0.0", "language": stt["language"],
                "prompt": "", "beam_size": "-1"}
        ask(base, clips[0]["wav"])  # calentamiento (no cuenta)
        for name, _, extra in configs:
            fields = {**base, **extra}
            rows = []
            for c in clips:
                if tripped:
                    break
                while reading()[0] > COOL_TEMP and not tripped:
                    time.sleep(1)
                t0 = time.perf_counter()
                hyp = clean_transcript(ask(fields, c["wav"]))
                dt = time.perf_counter() - t0
                e, n = wer(c["text"], hyp)
                ok = decide(hyp, lights) == c["expected"]
                rows.append({"file": c["file"], "ref": c["text"], "hyp": hyp, "ok": ok, "err": e, "words": n,
                             "seconds": round(dt, 2), "category": c["category"]})
                print(f"  {name:24} {c['file']} {dt:4.1f}s {'✔' if ok else '✘'} «{hyp}»", flush=True)
            if rows:
                out.append({"config": name, "rows": rows})
            if tripped:
                break
    except Exception as exc:
        print("error:", exc, flush=True)
    finally:
        stop.set()
        proc.terminate()
        try:
            proc.wait(10)
        except subprocess.TimeoutExpired:
            proc.kill()
    print(f"  [{model}] picos: {peak['temp']:.1f} °C, 5 V mín {peak['volt']:.3f} V"
          + (f"  ¡VIGILANTE: {tripped[0]}!" if tripped else ""), flush=True)
    return not tripped


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--tanda", default="normal")
    ap.add_argument("--solo", choices=sorted(MODELS), help="solo las configuraciones de este modelo")
    ap.add_argument("--hilos", type=int, help="hilos de Whisper (por defecto, los de config.json); "
                    "menos hilos calientan menos y no cambian la transcripción, solo el tiempo")
    ap.add_argument("--config", action="append", help="solo esta configuración (se puede repetir)")
    args = ap.parse_args()

    cfg = ConfigManager()
    stt = dict(cfg.section("stt"))
    if args.hilos:
        stt["threads"] = args.hilos
    lc = cfg.section("lights")
    lights = SmartLights(lc, resolve_path(lc["devices_file"]), resolve_path(lc["modes_file"]))
    folder = os.path.join(ROOT, "data", "stt_eval", args.tanda)
    with open(os.path.join(folder, "frases.json"), encoding="utf-8") as fh:
        index = json.load(fh)
    clips = []
    for e in index:
        audio, rate = read_wav(os.path.join(folder, e["file"]))
        clips.append({**e, "wav": wav_bytes(audio, rate), "expected": decide(e["text"], lights)})
    print(f"{len(clips)} frases de {folder}; {stt['threads']} hilos, audio_ctx {stt['audio_ctx']}\n")

    results: list = []
    for model in MODELS:
        if args.solo and model != args.solo:
            continue
        configs = [c for c in CONFIGS if c[1] == model and (not args.config or c[0] in args.config)]
        if not configs:
            continue
        if not run_model(model, configs, clips, stt, lights, results):
            print("Prueba cortada por el vigilante.")
            break
        time.sleep(20)  # deja enfriar entre modelos

    print("\n=== Resumen ===")
    print(f"{'configuración':26} {'órdenes ok':>11} {'WER':>6} {'medio':>6} {'máx':>6}")
    for r in results:
        rows = r["rows"]
        ok = sum(x["ok"] for x in rows)
        w = 100 * sum(x["err"] for x in rows) / max(1, sum(x["words"] for x in rows))
        ts = [x["seconds"] for x in rows]
        print(f"{r['config']:26} {ok:>4}/{len(rows):<6} {w:5.1f}% {sum(ts) / len(ts):5.1f}s {max(ts):5.1f}s")
    out = os.path.join(folder, f"resultados_{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"prompt": PROMPT, "results": results}, fh, indent=2, ensure_ascii=False)
    print(f"\nDetalle en {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
