"""Retroiluminación de la pantalla para el modo «pantalla apagada» de Vimo.

Se baja el brillo a 0 en /sys/class/backlight/*/brightness (escribible por el grupo «video»): la
pantalla queda negra pero sigue encendida, así que la pantalla táctil sigue respondiendo y un toque
despierta a Vimo.

Seguridad ante cierres bruscos: antes de apagar se guarda el brillo original en data/backlight.saved;
al arrancar Vimo (y al terminar start_agent.sh) se restaura si quedó guardado. Si no hay permiso o no
hay retroiluminación, se registra y la GUI usa solo el fondo negro.
"""
from __future__ import annotations

import glob
import logging
import os
import shutil
import subprocess

log = logging.getLogger(__name__)


class Backlight:
    def __init__(self, saved_path: str, brightness_path: str | None = None):
        self.saved_path = saved_path
        self.path = brightness_path or self._find()
        self._off = False

    @staticmethod
    def _find() -> str | None:
        found = sorted(glob.glob("/sys/class/backlight/*/brightness"))
        return found[0] if found else None

    @property
    def available(self) -> bool:
        return bool(self.path and os.access(self.path, os.W_OK))

    def _read(self) -> int | None:
        try:
            with open(self.path) as fh:
                return int(fh.read().strip())
        except (OSError, ValueError, TypeError):
            return None

    def _write(self, value: int) -> bool:
        try:
            with open(self.path, "w") as fh:
                fh.write(str(int(value)))
            return True
        except (OSError, TypeError) as exc:
            log.warning("No se pudo cambiar el brillo (%s): %s", self.path, exc)
            return False

    def restore_saved(self) -> None:
        """Si un cierre brusco dejó la pantalla apagada, vuelve a encenderla."""
        try:
            with open(self.saved_path) as fh:
                path, value = fh.read().split("\n")[:2]
        except (OSError, ValueError):
            return
        self.path = path or self.path
        if self._write(int(value)):
            log.info("Brillo restaurado a %s tras un cierre anterior", value)
        self._forget()

    def off(self) -> None:
        if self._off or not self.available:
            return
        current = self._read()
        if not current:
            return
        try:
            os.makedirs(os.path.dirname(self.saved_path), exist_ok=True)
            with open(self.saved_path, "w") as fh:
                fh.write(f"{self.path}\n{current}\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:
            log.warning("No se pudo guardar el brillo actual: %s", exc)
            return
        if self._write(0):
            self._off = True

    def on(self) -> None:
        if self._off:
            self.restore_saved()
            self._off = False
        power_on_outputs()

    def _forget(self) -> None:
        try:
            os.unlink(self.saved_path)
        except OSError:
            pass


def power_on_outputs() -> None:
    """Por si el apagado automático del sistema (swayidle → wlopm --off) apagó la pantalla."""
    if shutil.which("wlopm") and os.environ.get("WAYLAND_DISPLAY"):
        try:
            subprocess.Popen(["wlopm", "--on", "*"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            pass
