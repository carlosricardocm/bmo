"""Evalúa un modelo de wake word de openWakeWord con archivos WAV (sin micrófono ni altavoz).

Para cada archivo calcula la puntuación máxima procesando bloques de 1280 muestras a 16 kHz,
igual que Vimo en tiempo real (con 1 s de silencio antes y después).

    venv/bin/python tools/test_wake_word.py --positive ~/muestras/positivas --negative sounds/ \\
        [--model models/hey_vimo.onnx] [--threshold 0.5] [--limit 60]

Las muestras positivas deberían superar el umbral; las negativas (otras voces, ruido, los
sonidos del propio Vimo) no.
"""
from __future__ import annotations

import argparse
import glob
import os
import random
import sys
import time

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.audio_manager import read_wav, resample  # noqa: E402
from app.config_manager import ConfigManager  # noqa: E402
from app.wake_word import WakeWordDetector  # noqa: E402


def wavs(paths: list[str], limit: int | None, seed: int = 0) -> list[str]:
    files: list[str] = []
    for p in paths:
        p = os.path.expanduser(p)
        files += sorted(glob.glob(os.path.join(p, "**", "*.wav"), recursive=True)) if os.path.isdir(p) else [p]
    if limit and len(files) > limit:
        files = sorted(random.Random(seed).sample(files, limit))
    return files


def max_score(det: WakeWordDetector, path: str) -> float:
    audio, rate = read_wav(path)
    audio = resample(audio, rate, 16000)
    pad = np.zeros(16000, dtype=np.int16)
    audio = np.concatenate([pad, audio, pad])
    det.model.reset()
    best = 0.0
    for i in range(0, len(audio) - 1279, 1280):
        best = max(best, float(det.model.predict(audio[i:i + 1280])[det.name]))
    return best


def main() -> int:
    cfg = ConfigManager()
    w = cfg.section("wake_word")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--model", default=cfg.path_for("wake_word.model_path"))
    ap.add_argument("--threshold", type=float, default=w["threshold"])
    ap.add_argument("--positive", nargs="*", default=[])
    ap.add_argument("--negative", nargs="*", default=[])
    ap.add_argument("--limit", type=int, default=None, help="máximo de archivos por grupo (muestra aleatoria)")
    args = ap.parse_args()

    # Umbral 0,2 para la carga: la puntuación es exacta desde 0,1 y la tabla barre desde 0,2.
    det = WakeWordDetector(args.model, cfg.path_for("wake_word.melspec_model_path"),
                           cfg.path_for("wake_word.embedding_model_path"), min(args.threshold, 0.2),
                           exact_gate=w["exact_gate"], threads=w["threads"])
    det.load()
    t0 = time.monotonic()
    for label, paths in (("POSITIVAS", args.positive), ("NEGATIVAS", args.negative)):
        files = wavs(paths, args.limit)
        if not files:
            continue
        scores = [(max_score(det, f), f) for f in files]
        hits = [s for s, _ in scores if s >= args.threshold]
        vals = np.array([s for s, _ in scores])
        print(f"\n{label}: {len(hits)}/{len(files)} superan el umbral {args.threshold} "
              f"(mín {vals.min():.3f}, mediana {np.median(vals):.3f}, máx {vals.max():.3f})")
        worst = sorted(scores) if label == "POSITIVAS" else sorted(scores, reverse=True)
        sweep = "  ".join(f"{t:.1f}→{int((vals >= t).sum())}/{len(vals)}" for t in (0.2, 0.3, 0.4, 0.5, 0.6))
        print(f"   por umbral: {sweep}")
        for s, f in worst[:5]:
            print(f"   {s:.3f}  {os.path.relpath(f)}")
    print(f"\nTiempo total: {time.monotonic() - t0:.1f} s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
