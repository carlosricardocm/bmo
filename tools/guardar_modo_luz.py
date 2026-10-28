"""Guarda el estado actual de las lámparas como un modo que Vimo podrá poner por voz.

Pon las lámparas como quieras desde la app Smart Life (color, brillo, una escena propia…) y
ejecuta, por ejemplo:

    venv/bin/python tools/guardar_modo_luz.py cine
    venv/bin/python tools/guardar_modo_luz.py cine --lampara lampara_cuarto_izq

Después basta decir «Hey Vimo, modo cine». Un modo con el nombre de una escena de serie
(«lectura», «noche»…) la sustituye. `--borrar cine` elimina el modo.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.config_manager import ConfigManager, resolve_path  # noqa: E402
from app.intents import normalize  # noqa: E402
from app.smart_lights import SmartLights  # noqa: E402

# Qué DPs definen cada modo de trabajo (nombres de tinytuya.BulbDevice.dpset).
KEYS_BY_MODE = {"white": ("mode", "brightness", "colourtemp"), "colour": ("mode", "colour"),
                "scene": ("mode", "scene"), "music": ("mode",)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("nombre", help="cómo lo dirás: «modo <nombre>»")
    ap.add_argument("--lampara", action="append", help="solo esta lámpara (se puede repetir)")
    ap.add_argument("--borrar", action="store_true", help="eliminar el modo")
    args = ap.parse_args()

    cfg = ConfigManager().section("lights")
    lights = SmartLights(cfg, resolve_path(cfg["devices_file"]), resolve_path(cfg["modes_file"]))
    if lights.error:
        print("Error:", lights.error)
        return 1
    path = lights.modes_file
    try:
        with open(path, encoding="utf-8") as fh:
            modes = json.load(fh)
    except FileNotFoundError:
        modes = {}
    name = normalize(args.nombre)
    if args.borrar:
        print("Borrado." if modes.pop(name, None) is not None else "No existía ese modo.")
    else:
        entry = {}
        for lamp in lights.lamps:
            if args.lampara and lamp.name not in args.lampara:
                continue
            d = lights._device(lamp)
            state = d.state()
            if "Error" in state:
                print(f"{lamp.name}: no responde ({state['Error']})")
                continue
            if not state.get("is_on"):
                print(f"{lamp.name}: está apagada; se guarda como «apagada» en este modo")
                entry[lamp.name] = {d.dpset["switch"]: False}
                continue
            keys = KEYS_BY_MODE.get(state.get("mode"), ("mode",))
            dps = {d.dpset["switch"]: True}
            dps.update({d.dpset[k]: state[k] for k in keys if d.dpset.get(k) and state.get(k) is not None})
            entry[lamp.name] = dps
            print(f"{lamp.name}: {state.get('mode')} {dps}")
        if not entry:
            print("No se guardó nada.")
            return 1
        modes[name] = entry
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(modes, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)
    print(f"Modos guardados en {path}: {', '.join(sorted(modes)) or '(ninguno)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
