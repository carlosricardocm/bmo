"""Registrador de alimentación para diagnosticar apagados bruscos de la Raspberry Pi 5.

Guarda periódicamente la tensión EXT5V, el estado de throttling (vcgencmd) y el estado
de la batería X120x, forzando la escritura a disco (fsync) en cada muestra para que el
registro sobreviva a un corte de energía.

Uso:
    python3 tools/power_logger.py                # muestra cada 0.2 s en logs/power.log
    python3 tools/power_logger.py --interval 0.5 --out logs/power.log

Tras un apagado inesperado, revisa las últimas líneas:
    tail -n 40 logs/power.log
"""
import argparse
import os
import re
import subprocess
import time

PS_DIR = "/sys/class/power_supply"


def read_cmd(args: list[str]) -> str:
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=2).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def read_sysfs(path: str) -> str:
    try:
        with open(path) as fh:
            return fh.read().strip()
    except OSError:
        return "?"


def sample() -> str:
    adc = read_cmd(["vcgencmd", "pmic_read_adc"])
    volts = re.search(r"EXT5V_V volt\(\d+\)=([\d.]+)V", adc)
    core_a = re.search(r"VDD_CORE_A current\(\d+\)=([\d.]+)A", adc)
    temp = read_cmd(["vcgencmd", "measure_temp"]).replace("temp=", "").replace("'C", "")
    throttled = read_cmd(["vcgencmd", "get_throttled"]).replace("throttled=", "")
    load = read_sysfs("/proc/loadavg").split(" ")[0]
    bat = os.path.join(PS_DIR, "x120x-battery")
    ac = os.path.join(PS_DIR, "x120x-ac")
    bat_v = read_sysfs(os.path.join(bat, "voltage_now"))
    try:
        bat_v = f"{int(bat_v) / 1e6:.3f}"
    except ValueError:
        pass
    fields = [
        f"{time.strftime('%H:%M:%S')}.{int(time.time() * 1000) % 1000:03d}",
        f"ext5v={float(volts.group(1)):.3f}" if volts else "ext5v=?",
        f"core={float(core_a.group(1)):.2f}A" if core_a else "core=?",
        f"temp={temp or '?'}",
        f"load={load}",
        f"throttled={throttled or '?'}",
        f"ac={read_sysfs(os.path.join(ac, 'online'))}",
        f"bat={read_sysfs(os.path.join(bat, 'capacity'))}% {bat_v}V {read_sysfs(os.path.join(bat, 'status'))}",
    ]
    return " ".join(fields)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--max-mb", type=float, default=20, help="rota el archivo al superar este tamaño")
    parser.add_argument("--out", default=os.path.join(os.path.dirname(__file__), "..", "logs", "power.log"))
    args = parser.parse_args()
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    print(f"Registrando en {os.path.abspath(args.out)} (Ctrl+C para salir)")
    while True:
        with open(args.out, "a", buffering=1) as fh:
            fh.write(f"--- inicio {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            while fh.tell() < args.max_mb * 1e6:
                fh.write(f"{time.strftime('%Y-%m-%d')} {sample()}\n")
                fh.flush()
                os.fsync(fh.fileno())
                time.sleep(args.interval)
        os.replace(args.out, args.out + ".1")  # conserva el archivo anterior


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
