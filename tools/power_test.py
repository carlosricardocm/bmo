"""Prueba de alimentación por etapas para la Raspberry Pi 5 con SAI X120x.

Aplica cargas de CPU crecientes y registra en cada muestra la tensión de entrada de la Pi
(EXT5V), la corriente del núcleo, la tensión de la batería y los avisos de subtensión. Cada línea
se escribe con fsync en logs/power_test.log: si la Pi se apaga, el registro indica en qué etapa y
con qué tensiones ocurrió.

    python3 tools/power_test.py                 # etapas: reposo, 1, 2, 3 y 4 núcleos
    python3 tools/power_test.py --max-cores 2   # más suave
    python3 tools/power_test.py --label "solo bateria"
    python3 tools/power_test.py --summary       # analiza el registro (también tras un apagado)

Detén la prueba en cualquier momento con Ctrl+C.
"""
from __future__ import annotations

import argparse
import multiprocessing as mp
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from power_logger import sample  # noqa: E402

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs", "power_test.log")
UNDERVOLT_BITS = {0x1: "subtensión AHORA", 0x10000: "hubo subtensión", 0x4: "throttling AHORA",
                  0x40000: "hubo throttling"}


def burn(stop) -> None:
    x = 0
    while not stop.is_set():
        for i in range(100_000):
            x = (x * 31 + i) % 1_000_003


def run_stage(fh, test: str, stage: str, seconds: float, cores: int, interval: float) -> None:
    stop = mp.Event()
    procs = [mp.Process(target=burn, args=(stop,), daemon=True) for _ in range(cores)]
    for p in procs:
        p.start()
    print(f"  · {stage}: {seconds:.0f} s con {cores} núcleo(s) al 100 %", flush=True)
    end = time.monotonic() + seconds
    try:
        while time.monotonic() < end:
            fh.write(f"[{test}] [{stage}] {sample()}\n")
            fh.flush()
            os.fsync(fh.fileno())
            time.sleep(interval)
    finally:
        stop.set()
        for p in procs:
            p.join(timeout=3)


def summary(path: str) -> None:
    rx = re.compile(r"^\[(?P<test>[^\]]*)\] \[(?P<stage>[^\]]*)\] (?P<t>\S+) ext5v=(?P<v>[\d.?]+) "
                    r"core=(?P<a>[\d.?]+)A .*?throttled=(?P<th>\S+) ac=(?P<ac>\S+) bat=(?P<cap>\S+) "
                    r"(?P<bv>[\d.?]+)V (?P<st>\S+)")
    stages: dict[tuple, dict] = {}
    last = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = rx.match(line)
            if not m:
                continue
            key = (m["test"], m["stage"])
            s = stages.setdefault(key, {"n": 0, "v": [], "bv": [], "a": [], "th": 0, "first": m["t"], "ac": set()})
            s["n"] += 1
            s["last"] = m["t"]
            s["ac"].add(m["ac"])
            for k in ("v", "bv", "a"):
                try:
                    s[k].append(float(m[k]))
                except ValueError:
                    pass
            try:
                s["th"] |= int(m["th"], 16)
            except ValueError:
                pass
            last = (key, line.strip())
    if not stages:
        print("No hay datos en", path)
        return
    print(f"{'prueba':<16} {'etapa':<14} {'muestras':>8} {'5V mín':>7} {'bat mín':>8} {'bat máx':>8} "
          f"{'caída bat':>9} {'núcleo máx':>10}  avisos")
    for (test, stage), s in stages.items():
        vmin = min(s["v"]) if s["v"] else float("nan")
        bmin = min(s["bv"]) if s["bv"] else float("nan")
        bmax = max(s["bv"]) if s["bv"] else float("nan")
        amax = max(s["a"]) if s["a"] else float("nan")
        flags = ", ".join(t for bit, t in UNDERVOLT_BITS.items() if s["th"] & bit) or "-"
        ac = "red" if s["ac"] == {"1"} else ("SIN red" if s["ac"] == {"0"} else "/".join(sorted(s["ac"])))
        print(f"{test[:16]:<16} {stage[:14]:<14} {s['n']:>8} {vmin:>7.3f} {bmin:>8.3f} {bmax:>8.3f} "
              f"{bmax - bmin:>9.3f} {amax:>9.2f}A  {flags}; {ac}")
    print("\nÚltima muestra registrada:")
    print(" ", last[1])
    print("\nReferencias: 5V de la Pi < 4,75 V = la fuente no da suficiente corriente; caída de batería "
          "> 0,3 V con carga moderada = celdas débiles o mal contacto; batería cerca de 3,2 V = riesgo de corte.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--label", default=time.strftime("%H:%M"), help="nombre de esta prueba en el registro")
    ap.add_argument("--max-cores", type=int, default=4, choices=range(1, 5))
    ap.add_argument("--stage-seconds", type=float, default=60)
    ap.add_argument("--rest-seconds", type=float, default=20)
    ap.add_argument("--interval", type=float, default=0.25)
    ap.add_argument("--summary", action="store_true")
    ap.add_argument("--log", default=LOG)
    args = ap.parse_args()
    log = os.path.abspath(args.log)
    if args.summary:
        summary(log)
        return 0
    os.makedirs(os.path.dirname(log), exist_ok=True)
    os.sync()
    print(f"Prueba '{args.label}' → {log}")
    with open(log, "a", encoding="utf-8") as fh:
        try:
            run_stage(fh, args.label, "reposo", 30, 0, args.interval)
            for cores in range(1, args.max_cores + 1):
                run_stage(fh, args.label, f"{cores} núcleo(s)", args.stage_seconds, cores, args.interval)
                run_stage(fh, args.label, f"descanso {cores}", args.rest_seconds, 0, args.interval)
        except KeyboardInterrupt:
            print("\nInterrumpida por el usuario")
    print()
    summary(log)
    return 0


if __name__ == "__main__":
    sys.exit(main())
