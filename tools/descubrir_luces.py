"""Descubre las lámparas de Smart Life y guarda data/lights/devices.json.

1. Pide a la nube de Tuya (credenciales en data/lights/tinytuya.json) el id, la local_key y las
   funciones de cada dispositivo.
2. Escucha en la red local los anuncios de los dispositivos Tuya para saber su IP y versión.

Uso:  venv/bin/python tools/descubrir_luces.py [--sin-escaneo]
Vuelve a ejecutarlo si emparejas de nuevo una lámpara en la app (cambia su local_key) o si
cambia su IP. Vimo relee devices.json solo, sin reiniciar.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LIGHTS_DIR = os.path.join(ROOT, "data", "lights")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--sin-escaneo", action="store_true", help="no buscar IPs en la red local")
    args = ap.parse_args()
    import tinytuya

    creds = os.path.join(LIGHTS_DIR, "tinytuya.json")
    if not os.path.exists(creds):
        print(f"Falta {creds} (ver docs/luces.md)")
        return 1
    cloud = tinytuya.Cloud(configFile=creds)
    if cloud.error:
        print("Error con la nube de Tuya:", cloud.error)
        return 1
    devices = cloud.getdevices(verbose=False, include_map=True)
    if isinstance(devices, dict):
        print("Error con la nube de Tuya:", devices)
        return 1

    found = {}
    if not args.sin_escaneo:
        print("Buscando dispositivos en la red local (unos 20 s)…")
        found = tinytuya.deviceScan(verbose=False, maxretry=None, color=False, poll=False, byID=True)
    for d in devices:
        local = found.get(d["id"], {})
        if local:
            d["ip"] = local.get("ip") or d.get("ip")
            d["version"] = local.get("version") or d.get("version")

    out = os.path.join(LIGHTS_DIR, "devices.json")
    tmp = out + ".tmp"
    with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w", encoding="utf-8") as fh:
        json.dump(devices, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, out)

    print(f"\nGuardado {out}:")
    for d in devices:
        functions = []
        for dp in (d.get("mapping") or {}).values():
            code = dp.get("code")
            values = dp.get("values") or {}
            if isinstance(values, dict) and values.get("range"):
                code += f" {values['range']}"
            functions.append(code)
        print(f"- {d.get('name')}: {d.get('category')} ip={d.get('ip') or '?'} versión={d.get('version') or '?'}")
        if functions:
            print("    funciones:", ", ".join(functions))
    return 0


if __name__ == "__main__":
    sys.exit(main())
