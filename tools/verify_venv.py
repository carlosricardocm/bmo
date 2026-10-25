"""Comprueba que los archivos instalados en el venv coinciden en tamaño con su RECORD.

Útil tras un corte de energía: ext4 puede dejar archivos recién escritos con longitud cero.
Uso: venv/bin/python tools/verify_venv.py
"""
import csv
import glob
import os
import sys
import sysconfig


def main() -> int:
    site = sysconfig.get_paths()["purelib"]
    bad = {}
    for rec in glob.glob(os.path.join(site, "*.dist-info", "RECORD")):
        dist = os.path.basename(os.path.dirname(rec))
        with open(rec, newline="") as fh:
            for row in csv.reader(fh):
                if len(row) < 3 or not row[2]:
                    continue
                path = os.path.normpath(os.path.join(site, row[0]))
                if os.path.exists(path) and os.path.getsize(path) != int(row[2]):
                    bad.setdefault(dist, []).append(row[0])
    for dist, files in sorted(bad.items()):
        print(f"CORRUPTO {dist}: {len(files)} archivos (p. ej. {files[0]})")
    print(f"Paquetes corruptos: {len(bad)}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
