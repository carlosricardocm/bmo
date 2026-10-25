"""Carga de las caras (faces/<estado>/*.png) y animación sobre un Canvas de Tkinter.

- El orden de los fotogramas es determinista (orden "natural": 'speaking 2' < 'speaking 10').
- Si falta la carpeta de un estado se usa una alternativa de FALLBACKS; si no hay ninguna
  imagen, la GUI muestra solo el indicador de estado.
- Hay un único temporizador `after` activo: cambiar de estado cancela el anterior, de modo que
  no se acumulan temporizadores ni se mantienen referencias a fotogramas antiguos.
- Las imágenes se escalan al tamaño del lienzo manteniendo la proporción; el resultado se
  cachea por tamaño y la caché se descarta cuando cambia el tamaño.
"""
from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING

from PIL import Image

if TYPE_CHECKING:  # pragma: no cover
    import tkinter as tk

log = logging.getLogger(__name__)

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

# Alternativas visuales si falta un estado (en orden de preferencia).
FALLBACKS = {
    "warmup": ["idle"],
    "idle": ["warmup"],
    "listening": ["idle"],
    "thinking": ["idle"],
    "speaking": ["idle"],
    "capturing": ["thinking", "idle"],
    "error": ["idle"],
}


def natural_key(name: str) -> list:
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", name)]


class FaceLibrary:
    """Imágenes originales cargadas en memoria (sin modificar los archivos)."""

    def __init__(self, directory: str):
        self.directory = directory
        self.frames: dict[str, list[Image.Image]] = {}
        self.files: dict[str, list[str]] = {}
        self.load()

    def load(self) -> None:
        if not os.path.isdir(self.directory):
            log.error("No existe la carpeta de caras: %s", self.directory)
            return
        for entry in sorted(os.listdir(self.directory), key=natural_key):
            folder = os.path.join(self.directory, entry)
            if not os.path.isdir(folder):
                continue
            names = sorted(
                (f for f in os.listdir(folder) if f.lower().endswith(IMAGE_EXTS)), key=natural_key
            )
            images = []
            for name in names:
                path = os.path.join(folder, name)
                try:
                    with Image.open(path) as img:
                        images.append(img.convert("RGBA"))
                except OSError as exc:
                    log.warning("No se pudo cargar %s: %s", path, exc)
            if images:
                self.frames[entry] = images
                self.files[entry] = names
        log.info("Caras cargadas: %s", {k: len(v) for k, v in self.frames.items()})

    def resolve(self, state: str) -> str | None:
        """Nombre de la carpeta que se usará para `state` (o None si no hay imágenes)."""
        if state in self.frames:
            return state
        alternatives = FALLBACKS.get(state, ["idle"] if state.startswith(("music", "idle_")) else [])
        for alt in alternatives:
            if alt in self.frames:
                return alt
        return next(iter(self.frames), None)


def fit_size(src: tuple[int, int], box: tuple[int, int]) -> tuple[int, int]:
    """Tamaño máximo dentro de `box` conservando la proporción de `src`."""
    sw, sh = src
    bw, bh = box
    if sw <= 0 or sh <= 0 or bw <= 0 or bh <= 0:
        return (1, 1)
    scale = min(bw / sw, bh / sh)
    return (max(1, int(sw * scale)), max(1, int(sh * scale)))


class FaceAnimator:
    """Muestra la animación del estado actual en un Canvas. Usar solo desde el hilo de Tk."""

    def __init__(self, canvas: "tk.Canvas", library: FaceLibrary, frame_ms: dict[str, int], background: str):
        from PIL import ImageTk  # importado aquí para que FaceLibrary sea usable sin Tk

        self._ImageTk = ImageTk
        self.canvas = canvas
        self.library = library
        self.frame_ms = frame_ms
        self.background = background
        self._state = "warmup"
        self._index = 0
        self._after_id: str | None = None
        self._cache: dict[tuple[str, int], "ImageTk.PhotoImage"] = {}
        self._cache_size: tuple[int, int] = (0, 0)
        self._item = None
        self._text_item = None
        canvas.bind("<Configure>", self._on_resize)

    def set_state(self, state: str) -> None:
        if state == self._state and self._after_id is not None:
            return
        self._state = state
        self._index = 0
        self._schedule(immediate=True)

    def stop(self) -> None:
        if self._after_id is not None:
            try:
                self.canvas.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None

    def _on_resize(self, _event=None) -> None:
        size = (self.canvas.winfo_width(), self.canvas.winfo_height())
        if size != self._cache_size:
            self._cache.clear()
            self._cache_size = size
            self._draw()

    def _schedule(self, immediate: bool = False) -> None:
        self.stop()
        self._draw()
        folder = self.library.resolve(self._state)
        count = len(self.library.frames.get(folder, [])) if folder else 0
        if count > 1:
            delay = self.frame_ms.get(self._state, self.frame_ms.get("default", 500))
            self._after_id = self.canvas.after(delay, self._advance)

    def _advance(self) -> None:
        self._after_id = None
        self._index += 1
        self._schedule()

    def _photo(self, folder: str, index: int):
        key = (folder, index)
        if key not in self._cache:
            img = self.library.frames[folder][index]
            size = fit_size(img.size, self._cache_size)
            self._cache[key] = self._ImageTk.PhotoImage(img.resize(size, Image.LANCZOS))
        return self._cache[key]

    def _draw(self) -> None:
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        if w < 2 or h < 2:
            return
        self._cache_size = (w, h)
        folder = self.library.resolve(self._state)
        if folder is None:
            # Sin imágenes: indicador textual, la aplicación sigue funcionando.
            if self._item is not None:
                self.canvas.delete(self._item)
                self._item = None
            if self._text_item is None:
                self._text_item = self.canvas.create_text(w // 2, h // 2, fill="white", font=("Sans", 28))
            self.canvas.coords(self._text_item, w // 2, h // 2)
            self.canvas.itemconfigure(self._text_item, text=self._state.upper())
            return
        frames = self.library.frames[folder]
        photo = self._photo(folder, self._index % len(frames))
        if self._item is None:
            self._item = self.canvas.create_image(w // 2, h // 2, image=photo, anchor="center")
        else:
            self.canvas.coords(self._item, w // 2, h // 2)
            self.canvas.itemconfigure(self._item, image=photo)
        # Mantener la referencia evita que Tk libere la imagen mostrada.
        self.canvas._vimo_photo = photo
