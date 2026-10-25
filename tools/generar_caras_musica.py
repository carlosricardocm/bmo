"""Genera las caras animadas de música a partir de las caras originales de Be More Agent.

No dibuja una cara nueva: recorta las piezas reales de las imágenes originales (ojos y bocas de
faces/listening, faces/speaking y faces/capturing) y las recombina con movimiento, más notas
musicales dibujadas con el mismo trazo y colores (verde de la nariz de «capturing» y contorno
negro). Las imágenes originales no se modifican.

    venv/bin/python tools/generar_caras_musica.py      # escribe faces/music_*/

Animaciones:
    music_dance  bailando: ojos felices ^ ^, sonrisa; la cara se balancea y rebota
    music_sing   cantando: ojos felices, bocas de «speaking» alternadas, notas que salen de la boca
    music_vibe   disfrutando: ojos de punto que miran a los lados, cabeza inclinada, notas flotando
    music_wow    emocionado: ojos brillantes de «capturing», boca abierta, saltitos
"""
from __future__ import annotations

import math
import os
import shutil

import numpy as np
from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FACES = os.path.join(ROOT, "faces")
W, H = 800, 480
CENTER = (400, 240)
INK = (0, 0, 0, 255)
SS = 3  # supermuestreo para dibujar las notas con bordes suaves


def load(rel: str) -> Image.Image:
    return Image.open(os.path.join(FACES, rel)).convert("RGB")


BASE = load("idle/idle 01.png")
BG = BASE.getpixel((5, 5))                       # (189, 255, 203)
NOTE_GREEN = load("capturing/capturing 01.png").getpixel((400, 304))  # verde de la nariz


def piece(rel: str, box: tuple[int, int, int, int]) -> Image.Image:
    """Recorte RGBA de una pieza: la opacidad sale de cuánto se aparta cada píxel del fondo,
    así el antialiasing original se conserva al moverla sobre el mismo fondo."""
    im = load(rel).crop(box)
    a = np.asarray(im).astype(int)
    diff = np.abs(a - np.array(BG)).max(axis=2)
    alpha = np.clip(diff * 255 // 60, 0, 255).astype(np.uint8)
    rgba = np.dstack([np.asarray(im), alpha])
    return Image.fromarray(rgba, "RGBA")


# Coordenadas (x0, y0, x1, y1) medidas en las imágenes originales de 800×480.
P = {
    "dot_l": ("listening/listen 01.png", (195, 167, 241, 213)),
    "dot_r": ("listening/listen 01.png", (559, 167, 605, 213)),
    "happy_l": ("listening/listen 02.png", (195, 167, 241, 204)),
    "happy_r": ("listening/listen 02.png", (559, 167, 605, 204)),
    "smile": ("listening/listen 01.png", (327, 235, 473, 317)),
    "open": ("speaking/speaking 02.png", (307, 235, 469, 322)),
    "open_smile": ("speaking/speaking 03.png", (327, 234, 473, 317)),
    "star_l": ("capturing/capturing 01.png", (126, 83, 293, 250)),
    "star_r": ("capturing/capturing 01.png", (519, 83, 686, 250)),
}
PIECES = {k: (piece(rel, box), box[:2]) for k, (rel, box) in P.items()}


def face_layer(parts: list[str], offsets: dict[str, tuple[int, int]] | None = None) -> Image.Image:
    """Cara transparente con las piezas en su posición original (± desplazamiento por pieza)."""
    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    for name in parts:
        img, (x, y) = PIECES[name]
        dx, dy = (offsets or {}).get(name, (0, 0))
        layer.alpha_composite(img, (x + dx, y + dy))
    return layer


def note_layer(notes: list[tuple[str, float, float, float, float]]) -> Image.Image:
    """Notas musicales: (tipo '8' = corchea ♪ | '16' = dos corcheas ♫, x, y, tamaño, giro°)."""
    big = Image.new("RGBA", (W * SS, H * SS), (0, 0, 0, 0))
    for kind, x, y, size, angle in notes:
        tile = _note(kind, size * 1.35)
        tile = tile.rotate(angle, resample=Image.BICUBIC, expand=True)
        big.alpha_composite(tile, (int(x * SS - tile.width / 2), int(y * SS - tile.height / 2)))
    return big.resize((W, H), Image.LANCZOS)


def _note(kind: str, size: float) -> Image.Image:
    s = size * SS
    lw = int(5 * SS)                                   # mismo grosor que los trazos originales
    w = int(s * (1.9 if kind == "16" else 1.3))
    h = int(s * 1.7)
    img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    hw, hh = s * 0.52, s * 0.38                        # cabeza de la nota

    def head(cx, cy):
        d.ellipse((cx - hw / 2, cy - hh / 2, cx + hw / 2, cy + hh / 2), fill=NOTE_GREEN + (255,),
                  outline=INK, width=lw)

    def stem(x, y_bottom, y_top):
        d.line((x, y_bottom, x, y_top), fill=INK, width=lw)

    if kind == "8":
        cx, cy = s * 0.4, h - hh / 2 - lw
        head(cx, cy)
        sx, top = cx + hw / 2 - lw / 2, s * 0.15
        stem(sx, cy, top)
        # Banderita: trazo continuo que nace en la punta de la plica y cae hacia la derecha.
        flag = [(sx, top), (sx + s * 0.22, top + s * 0.16), (sx + s * 0.42, top + s * 0.38),
                (sx + s * 0.44, top + s * 0.62), (sx + s * 0.32, top + s * 0.86)]
        d.line(flag, fill=INK, width=lw, joint="curve")
    else:
        c1, c2 = (s * 0.4, h - hh / 2 - lw), (s * 1.35, h - hh / 2 - lw - s * 0.2)
        head(*c1)
        head(*c2)
        x1, x2 = c1[0] + hw / 2 - lw / 2, c2[0] + hw / 2 - lw / 2
        top1, top2 = s * 0.25, s * 0.05
        stem(x1, c1[1], top1)
        stem(x2, c2[1], top2)
        d.polygon([(x1 - lw / 2, top1), (x2 + lw / 2, top2), (x2 + lw / 2, top2 + lw * 2.2),
                   (x1 - lw / 2, top1 + lw * 2.2)], fill=INK)
    return img


def compose(face: Image.Image, dx=0, dy=0, rot=0.0, notes=()) -> Image.Image:
    moved = face.rotate(rot, resample=Image.BICUBIC, center=CENTER, translate=(dx, dy))
    out = Image.new("RGBA", (W, H), BG + (255,))
    out.alpha_composite(moved)
    if notes:
        out.alpha_composite(note_layer(list(notes)))
    return out.convert("RGB")


def frames_dance():
    face = face_layer(["happy_l", "happy_r", "smile"])
    moves = [(-26, 0, -5), (0, -16, 0), (26, 0, 5), (0, -16, 0)]
    notes = [[("8", 110, 150, 44, 12), ("16", 690, 110, 38, -8)],
             [("8", 115, 130, 44, 0), ("16", 690, 130, 38, 0)],
             [("8", 110, 110, 44, -10), ("16", 690, 150, 38, 10)],
             [("8", 115, 130, 44, 0), ("16", 690, 130, 38, 0)]]
    return [compose(face, dx, dy, r, n) for (dx, dy, r), n in zip(moves, notes)]


def frames_sing():
    eyes = ["happy_l", "happy_r"]
    mouths = ["open", "open_smile", "open", "smile"]
    out = []
    for i, m in enumerate(mouths):
        face = face_layer(eyes + [m])
        # Las notas salen de la comisura de la boca y suben por debajo/al lado del ojo derecho
        # (ojo: x 559–605, y 167–213), sin tocarlo.
        notes = [("8", 495 + i * 42, 345 - i * 22, 30 + i * 3, 10 - i * 6),
                 ("16", 660 + i * 18, 330 - i * 40, 30, -6 + i * 4)]
        if i >= 2:
            notes.append(("8", 290 - (i - 2) * 40, 350 - (i - 2) * 30, 28, -12))
        out.append(compose(face, 0, -4 if i % 2 else 0, 0, notes))
    return out


def frames_vibe():
    look = [(-12, 0), (0, 0), (12, 0), (0, 0)]
    tilt = [-6, -2, 6, 2]
    out = []
    for i, ((lx, ly), r) in enumerate(zip(look, tilt)):
        face = face_layer(["dot_l", "dot_r", "smile"], {"dot_l": (lx, ly), "dot_r": (lx, ly)})
        drift = i * 10
        notes = [("8", 130, 330 - drift, 40, 8 - i * 4), ("8", 680, 300 - drift, 34, -6 + i * 4),
                 ("16", 700, 110 + (drift if i < 2 else 30 - drift), 34, 6)]
        out.append(compose(face, 0, 0, r, notes))
    return out


def frames_wow():
    face_a = face_layer(["star_l", "star_r", "open_smile"])
    face_b = face_layer(["star_l", "star_r", "open"])
    seq = [(face_a, 0), (face_b, -18), (face_a, 0), (face_b, -10)]
    out = []
    for i, (face, dy) in enumerate(seq):
        notes = [("16", 70 if i % 2 else 80, 380 - i * 6, 40, -10 + i * 6),
                 ("8", 735, 370 - (i % 2) * 20, 40, 10 - i * 5)]
        out.append(compose(face, 0, dy, 0, notes))
    return out


SETS = {"music_dance": frames_dance, "music_sing": frames_sing, "music_vibe": frames_vibe,
        "music_wow": frames_wow}


def main() -> None:
    for name, make in SETS.items():
        folder = os.path.join(FACES, name)
        if os.path.isdir(folder):
            shutil.rmtree(folder)  # solo carpetas generadas por este script (music_*)
        os.makedirs(folder)
        for i, frame in enumerate(make(), 1):
            frame.save(os.path.join(folder, f"{name} {i:02d}.png"), optimize=True)
        print(f"{name}: {len(os.listdir(folder))} fotogramas")


if __name__ == "__main__":
    main()
