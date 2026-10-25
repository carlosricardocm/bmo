"""Evalúa la wake word con grabaciones reales y recomienda umbral, bloques seguidos y VAD.

Calcula una vez, para cada grabación, la puntuación del modelo y del VAD Silero cada 80 ms, y
simula con la MISMA lógica de activación que usa Vimo (app.wake_word.TriggerLogic) cada
combinación de parámetros:

- Positivas: % de grabaciones en las que Vimo se habría activado.
- Negativas: activaciones falsas por hora de audio.

    venv/bin/python tools/evaluar_wake_word.py                       # data/muestras_wake/
    venv/bin/python tools/evaluar_wake_word.py --max-falsas-hora 1 --aplicar
    venv/bin/python tools/evaluar_wake_word.py --positivas ~/bmo/muestras/positivas --negativas sounds/

Por defecto usa data/muestras_wake/{positivas,negativas} (grabadas con tools/grabar_muestras.py).
--aplicar guarda la configuración recomendada en config.json.
"""
from __future__ import annotations

import argparse
import glob
import itertools
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

import numpy as np  # noqa: E402

from app.audio_manager import read_wav, resample  # noqa: E402
from app.config_manager import ConfigManager, resolve_path  # noqa: E402
from app.wake_word import CHUNK_SECONDS, SileroVAD, TriggerLogic, WakeWordDetector  # noqa: E402

THRESHOLDS = [0.15, 0.2, 0.25, 0.3, 0.35, 0.4, 0.45, 0.5, 0.55, 0.6, 0.7]
PATIENCES = [1, 2, 3]
VADS = [0.0, 0.3, 0.5]


def wav_files(paths: list[str]) -> list[str]:
    files: list[str] = []
    for p in paths:
        p = os.path.expanduser(p)
        if os.path.isdir(p):
            files += sorted(glob.glob(os.path.join(p, "**", "*.wav"), recursive=True))
        elif os.path.isfile(p):
            files.append(p)
    return files


def score_file(det: WakeWordDetector, vad, path: str, pad: bool) -> tuple[np.ndarray, np.ndarray]:
    audio, rate = read_wav(path)
    audio = resample(audio, rate, 16000)
    if pad:  # las positivas son cortas: silencio alrededor, como en una situación real
        silence = np.zeros(16000, np.int16)
        audio = np.concatenate([silence, audio, silence])
    det.model.reset()
    vad.reset_states()
    vad.prediction_buffer.clear()
    scores, vads = [], []
    for i in range(0, len(audio) - 1279, 1280):
        chunk = audio[i:i + 1280]
        scores.append(float(det.model.predict(chunk)[det.name]))
        vad(chunk)
        vads.append(float(vad.prediction_buffer[-1]))
    return np.array(scores), np.array(vads)


def triggers(scores: np.ndarray, vads: np.ndarray, thr: float, patience: int, vad_thr: float,
             cooldown: float) -> int:
    logic = TriggerLogic(thr, patience, vad_thr, cooldown)
    n = 0
    for i, s in enumerate(scores):
        if logic.feed(s, i * CHUNK_SECONDS, list(vads[max(0, i - 10):i + 1])):
            n += 1
    return n


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    base = os.path.join(ROOT, "data", "muestras_wake")
    ap.add_argument("--positivas", nargs="*", default=[os.path.join(base, "positivas")])
    ap.add_argument("--negativas", nargs="*", default=[os.path.join(base, "negativas")])
    ap.add_argument("--modelo", default=None, help="por defecto el de config.json")
    ap.add_argument("--max-falsas-hora", type=float, default=0.5,
                    help="máximo de activaciones falsas por hora aceptable (defecto 0,5)")
    ap.add_argument("--aplicar", action="store_true", help="guardar la recomendación en config.json")
    ap.add_argument("--forzar", action="store_true", help="aplicar aunque haya menos de 15 min de negativas")
    args = ap.parse_args()

    cfg = ConfigManager()
    w = cfg.section("wake_word")
    model = args.modelo or resolve_path(w["model_path"])
    pos_files, neg_files = wav_files(args.positivas), wav_files(args.negativas)
    if not pos_files:
        print("No hay muestras positivas. Grábalas con: venv/bin/python tools/grabar_muestras.py positivas")
        return 1
    # Umbral mínimo de la tabla: la puntuación exacta de livekit se calcula desde ahí.
    det = WakeWordDetector(model, resolve_path(w["melspec_model_path"]), resolve_path(w["embedding_model_path"]),
                           threshold=min(THRESHOLDS), exact_gate=w["exact_gate"], threads=w["threads"])
    det.load()
    vad = SileroVAD(resolve_path(w["vad_model_path"]))

    t0 = time.monotonic()
    pos = [score_file(det, vad, f, pad=True) for f in pos_files]
    neg = [score_file(det, vad, f, pad=False) for f in neg_files]
    neg_hours = sum(len(s) for s, _ in neg) * CHUNK_SECONDS / 3600
    print(f"Modelo: {os.path.basename(model)}")
    print(f"Positivas: {len(pos)} grabaciones · Negativas: {len(neg)} grabaciones ({neg_hours * 60:.1f} min)"
          f" · cálculo en {time.monotonic() - t0:.0f} s")
    if neg_hours < 0.25:
        print("⚠ Menos de 15 min de negativas: la estimación de falsas activaciones es poco fiable.")

    rows = []
    for thr, pat, vth in itertools.product(THRESHOLDS, PATIENCES, VADS):
        hits = sum(triggers(s, v, thr, pat, vth, w["cooldown_seconds"]) > 0 for s, v in pos)
        fa = sum(triggers(s, v, thr, pat, vth, w["cooldown_seconds"]) for s, v in neg)
        rows.append({"thr": thr, "pat": pat, "vad": vth, "recall": hits / len(pos), "fa": fa,
                     "fa_h": fa / neg_hours if neg_hours else float("nan")})

    current = next((r for r in rows if r["thr"] == w["threshold"] and r["pat"] == w["patience_frames"]
                    and r["vad"] == w["vad_threshold"]), None)
    allowed = [r for r in rows if neg_hours and r["fa_h"] <= args.max_falsas_hora]
    # La mejor: más detecciones; a igualdad, menos falsas y luego la más conservadora (umbral alto).
    best = max(allowed, key=lambda r: (round(r["recall"], 4), -r["fa"], r["thr"], r["vad"])) if allowed else None

    print(f"\n{'umbral':>6} {'seguidos':>8} {'VAD':>5} {'detecta':>8} {'falsas':>7} {'falsas/h':>9}")
    shown = sorted(rows, key=lambda r: (-r["recall"], r["fa"]))[:15]
    for r in shown + ([current] if current and current not in shown else []):
        mark = "  ← actual" if r is current else ("  ← recomendada" if r is best else "")
        print(f"{r['thr']:>6.2f} {r['pat']:>8} {r['vad']:>5.1f} {r['recall']:>7.0%} {r['fa']:>7} "
              f"{r['fa_h']:>9.1f}{mark}")

    if current:
        print(f"\nActual: umbral {current['thr']}, {current['pat']} bloque(s), VAD {current['vad']} → "
              f"detecta {current['recall']:.0%}, {current['fa']} falsas")
    if not best:
        print(f"\nNinguna combinación cumple ≤ {args.max_falsas_hora} falsas/hora"
              + (" (no hay negativas)." if not neg_hours else ". Graba más negativas o reentrena el modelo."))
        return 0
    print(f"Recomendada: umbral {best['thr']}, {best['pat']} bloque(s), VAD {best['vad']} → "
          f"detecta {best['recall']:.0%}, {best['fa']} falsas ({best['fa_h']:.1f}/h)")
    if args.aplicar and neg_hours < 0.25 and not args.forzar:
        print("No se aplica: con menos de 15 min de negativas un umbral bajo puede provocar muchas "
              "activaciones falsas en la vida real. Graba más con tools/grabar_muestras.py negativas "
              "(o usa --forzar si sabes lo que haces).")
    elif args.aplicar:
        cfg.update({"wake_word.threshold": best["thr"], "wake_word.patience_frames": best["pat"],
                    "wake_word.vad_threshold": best["vad"]})
        print("Guardado en config.json. Reinicia Vimo para aplicarlo.")
    else:
        print("Para guardarla: añade --aplicar")
    return 0


if __name__ == "__main__":
    sys.exit(main())
