"""Genera variaciones de la cara de reposo («idle») a partir de las caras originales.

Igual que las caras de música: recombina piezas reales de las imágenes originales (ojos cerrados y
boca recta de faces/idle, ojos de punto y sonrisa de faces/listening, boca abierta de
faces/speaking), sin modificar las originales. Cada animación es un gesto breve que Vimo hace de
vez en cuando estando en reposo, y después vuelve a la cara «idle».

    venv/bin/python tools/generar_caras_reposo.py      # escribe faces/idle_*/

    idle_blink    abre los ojos un momento y parpadea
    idle_look     abre los ojos y mira a un lado y al otro
    idle_smile    sonríe un momento con los ojos cerrados
    idle_breathe  respira: la cara sube y baja muy suavemente
    idle_yawn     bosteza
    idle_sleepy   se adormila: «z z z» flotando
"""
from __future__ import annotations

import os
import shutil
import sys

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import generar_caras_musica as base  # noqa: E402  (piezas, montaje y colores comunes)

FACES = base.FACES
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Piezas de la cara de reposo original (coordenadas medidas en faces/idle/idle 01.png).
base.P.update({
    "closed_l": ("idle/idle 01.png", (191, 181, 245, 217)),
    "closed_r": ("idle/idle 01.png", (555, 181, 609, 217)),
    "flat": ("idle/idle 01.png", (346, 293, 454, 311)),
})
for key in ("closed_l", "closed_r", "flat"):
    rel, box = base.P[key]
    base.PIECES[key] = (base.piece(rel, box), box[:2])


def scaled(name: str, factor: float, new_name: str) -> str:
    """Copia de una pieza escalada, centrada donde estaba la original."""
    img, (x, y) = base.PIECES[name]
    w, h = img.size
    small = img.resize((max(1, int(w * factor)), max(1, int(h * factor))), Image.LANCZOS)
    cx, cy = x + w / 2, y + h / 2
    base.PIECES[new_name] = (small, (int(cx - small.width / 2), int(cy - small.height / 2)))
    return new_name


# La sonrisa y la boca abierta, más pequeñas y a la altura de la boca recta de «idle».
for src, factor, name in (("smile", 0.7, "smile_small"), ("open", 0.45, "yawn_s"), ("open", 0.62, "yawn_m"),
                          ("open", 0.8, "yawn_l")):
    scaled(src, factor, name)
    img, (x, y) = base.PIECES[name]
    flat_img, (fx, fy) = base.PIECES["flat"]
    base.PIECES[name] = (img, (x, int(fy + flat_img.height / 2 - img.height / 2) + (6 if src == "smile" else 4)))

IDLE = ["closed_l", "closed_r", "flat"]


def frame(parts, offsets=None, dy=0, rot=0.0, extra=None):
    img = base.compose(base.face_layer(parts, offsets), 0, dy, rot)
    if extra:
        extra(img)
    return img


def zzz(sizes_positions):
    def draw(img: Image.Image) -> None:
        d = ImageDraw.Draw(img)
        for size, (x, y) in sizes_positions:
            font = ImageFont.truetype(FONT, size)
            d.text((x, y), "z", font=font, fill=base.NOTE_GREEN, stroke_width=max(2, size // 12),
                   stroke_fill=(0, 0, 0))
    return draw


def frames_blink():
    eyes_open = ["dot_l", "dot_r", "flat"]
    return [frame(IDLE), frame(eyes_open), frame(eyes_open), frame(IDLE), frame(eyes_open), frame(IDLE)]


def frames_look():
    eyes = ["dot_l", "dot_r", "flat"]
    look = lambda dx: {"dot_l": (dx, 0), "dot_r": (dx, 0)}  # noqa: E731
    return [frame(IDLE), frame(eyes), frame(eyes, look(-26)), frame(eyes, look(-26)), frame(eyes),
            frame(eyes, look(26)), frame(eyes, look(26)), frame(eyes), frame(IDLE)]


def frames_smile():
    smile = ["closed_l", "closed_r", "smile_small"]
    return [frame(IDLE), frame(smile), frame(smile), frame(smile), frame(IDLE)]


def frames_breathe():
    return [frame(IDLE, dy=d) for d in (0, -2, -4, -5, -4, -2, 0, 1)]


def frames_yawn():
    seq = ["flat", "yawn_s", "yawn_m", "yawn_l", "yawn_l", "yawn_m", "yawn_s", "flat"]
    return [frame(["closed_l", "closed_r", m], dy=(-4 if m == "yawn_l" else 0)) for m in seq]


def frames_sleepy():
    out = []
    for i in range(6):
        # «z» cada vez más grandes subiendo en diagonal, lejos del ojo derecho (x 555–609, y 181–217).
        zs = [(40 + 14 * k, (650 + 38 * k, 150 - 52 * k - i * 5)) for k in range(min(3, 1 + i // 2))]
        out.append(frame(IDLE, dy=(i % 2), extra=zzz(zs)))
    return out


SETS = {"idle_blink": frames_blink, "idle_look": frames_look, "idle_smile": frames_smile,
        "idle_breathe": frames_breathe, "idle_yawn": frames_yawn, "idle_sleepy": frames_sleepy}


def main() -> None:
    for name, make in SETS.items():
        folder = os.path.join(FACES, name)
        if os.path.isdir(folder):
            shutil.rmtree(folder)  # solo carpetas generadas por este script (idle_*)
        os.makedirs(folder)
        for i, img in enumerate(make(), 1):
            img.save(os.path.join(folder, f"{name} {i:02d}.png"), optimize=True)
        print(f"{name}: {len(os.listdir(folder))} fotogramas")


if __name__ == "__main__":
    main()
