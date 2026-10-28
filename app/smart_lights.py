"""Lámparas inteligentes de Smart Life (Tuya) controladas por la red local con tinytuya.

Las órdenes («prende las luces», «apaga la lámpara derecha», «luz al 50», «pon la luz roja»,
«modo lectura») se reconocen aquí y se ejecutan sin pasar por Claude Code ni por internet:
tinytuya habla directamente con cada lámpara por la WiFi usando su `local_key`.

Archivos (en data/lights/, fuera del repositorio):
    tinytuya.json  credenciales del proyecto de platform.tuya.com (solo para descubrir)
    devices.json   id, local_key, IP y versión de cada lámpara (tools/descubrir_luces.py)
    modes.json     modos guardados desde la app (tools/guardar_modo_luz.py)

devices.json y modes.json se releen cuando cambian, sin reiniciar Vimo. Ver docs/luces.md.
"""
from __future__ import annotations

import concurrent.futures
import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from difflib import SequenceMatcher

from .intents import _PREFIX, normalize

log = logging.getLogger(__name__)

ON = "on"
OFF = "off"
BRIGHTNESS = "brightness"   # value: 1-100
BRIGHTER = "brighter"
DIMMER = "dimmer"
COLOUR = "colour"           # value: nombre del color (clave de COLOURS)
WHITE = "white"
TEMP = "temp"               # value: 0 (cálida) - 100 (fría)
MODE = "mode"               # value: nombre del modo; None si no se conoce

# Modos de trabajo de la lámpara (DP work_mode).
BUILTIN_MODES = {"blanco": "white", "color": "colour", "musica": "music"}
# Escenas predefinidas de las lámparas Tuya (DP scene_data_v2), las mismas de la app Smart Life.
# «noche» y «precioso» coinciden con lo leído de lampara_cuarto_der/izq el 25-10-2026; un modo con
# el mismo nombre guardado con tools/guardar_modo_luz.py tiene prioridad.
SCENES: dict[str, str] = {
    "noche": "000e0d0000000000000000c80000",
    "lectura": "010e0d0000000000000003e801f4",
    "trabajo": "020e0d0000000000000003e803e8",
    "ocio": "030e0d0000000000000001f401f4",
    "suave": "04464602007803e803e800000000464602007803e8000a00000000",
    "colorido": "05464601000003e803e800000000464601007803e803e80000000046460100f003e803e800000000"
                "464601003d03e803e80000000046460100ae03e803e800000000464601011303e803e800000000",
    "deslumbrante": "06464601000003e803e800000000464601007803e803e80000000046460100f003e803e800000000",
    "precioso": "07464602000003e803e800000000464602007803e803e80000000046460200f003e803e800000000"
                "464602003d03e803e80000000046460200ae03e803e800000000464602011303e803e800000000",
}
# Otras formas de decir cada modo.
MODE_ALIASES = {
    "buenas noches": "noche", "dormir": "noche", "nocturno": "noche", "leer": "lectura",
    "trabajar": "trabajo", "estudio": "trabajo", "estudiar": "trabajo", "relax": "ocio",
    "relajado": "ocio", "relajacion": "ocio", "descanso": "ocio", "colores": "colorido",
    "arcoiris": "colorido", "arco iris": "colorido", "fiesta": "deslumbrante", "disco": "deslumbrante",
    "hermoso": "precioso", "bonito": "precioso", "precioso": "precioso", "colour": "color",
}

COLOURS: dict[str, tuple[int, int, int]] = {
    "rojo": (255, 0, 0), "naranja": (255, 110, 0), "amarillo": (255, 210, 0), "verde": (0, 255, 0),
    "cian": (0, 255, 255), "azul": (0, 40, 255), "morado": (140, 0, 255), "rosa": (255, 60, 150),
    "magenta": (255, 0, 255),
}
_COLOUR_WORDS = {
    "rojo": "rojo", "roja": "rojo", "naranja": "naranja", "anaranjada": "naranja", "anaranjado": "naranja",
    "amarillo": "amarillo", "amarilla": "amarillo", "verde": "verde", "cian": "cian", "turquesa": "cian",
    "azul": "azul", "morado": "morado", "morada": "morado", "violeta": "morado", "lila": "morado",
    "rosa": "rosa", "rosada": "rosa", "rosado": "rosa", "magenta": "magenta", "fucsia": "magenta",
}

_LIGHT = r"(?:luz|luces|lampara|lamparas|lamparita|lamparitas|foco|focos)"
_ART = r"(?:(?:la|las|el|los|mi|mis|esa|esas)\s+)?(?:(?:dos|ambas|ambos)\s+)?"
_VERB_ON = {"prende", "prendeme", "prender", "prendan", "enciende", "enciendeme", "encender", "activa", "activar",
            "aprende", "prenda", "prendo", "prendes", "enciendo", "enciendes"}
_VERB_OFF = {"apaga", "apagame", "apagar", "apaguen", "desactiva", "desactivar", "apague", "apagas", "apago"}
_VERB_UP = {"sube", "subele", "subir", "aumenta", "aumentale"}
_VERB_DOWN = {"baja", "bajale", "bajar", "disminuye", "reduce", "bajame"}
_VERB_SET = {"pon", "ponme", "ponle", "poner", "ponla", "ponlas", "cambia", "cambiale", "cambiar", "deja", "dejame",
             "ajusta", "configura",
             # Whisper oye a veces «por la luz roja» (log del 25-10-2026), «pom», «bon»…
             "por", "pom", "bon", "pone", "polo", "pola"}
_VERBS = _VERB_ON | _VERB_OFF | _VERB_UP | _VERB_DOWN | _VERB_SET
_HEAD = re.compile(_PREFIX + r"(?:(?P<verb>" + "|".join(sorted(_VERBS, key=len, reverse=True)) + r")\b\s*)?(?P<rest>.*)$")
_END = re.compile(r"(?:\s+(?:por favor|porfa|vimo|bimo|gracias))+$")
# Palabras que pueden acompañar a una orden de luces sin cambiar su sentido.
_FILLER = {"la", "las", "el", "los", "de", "del", "en", "a", "al", "mi", "mis", "lado", "cuarto", "habitacion",
           "recamara", "cuarto", "dos", "ambas", "todas", "todos", "luz", "luces", "lampara", "lamparas",
           "lamparita", "lamparitas", "foco", "focos", "por", "ciento", "porciento", "color", "tono", "un", "una",
           "poco", "poquito", "mas", "brillo", "intensidad", "modo", "y", "tambien", "esa", "esas", "ya"}


@dataclass
class LightCommand:
    action: str
    targets: list[str] = field(default_factory=list)  # nombres Tuya; vacío = todas las lámparas
    value: object = None
    raw_mode: str | None = None                         # lo que se dijo tras «modo» (para avisar)


@dataclass
class Lamp:
    name: str
    label: str
    aliases: list[str]
    id: str = ""
    key: str = ""
    ip: str = ""
    version: float = 3.3


class LightError(Exception):
    pass


def _words(text: str) -> list[str]:
    return text.split()


class LightParser:
    """Reconoce órdenes de luces. Solo se activa con una referencia clara a las luces (luz, lámpara,
    foco, brillo, el apodo de una lámpara o «modo <modo conocido>»), para no capturar «pon la canción
    Luz de luna» ni preguntas normales."""

    def __init__(self, lamps: list[Lamp], modes: list[str]):
        self.lamps = lamps
        # forma hablada normalizada -> nombre canónico del modo
        self.modes = {m: m for m in [*BUILTIN_MODES, *SCENES]}
        self.modes.update(MODE_ALIASES)
        self.modes.update({normalize(m): normalize(m) for m in modes})
        self._alias = []  # (frase normalizada, nombre de la lámpara), las más largas primero
        for lamp in lamps:
            for a in [lamp.name.replace("_", " "), *lamp.aliases]:
                self._alias.append((normalize(a), lamp.name))
        self._alias.sort(key=lambda p: -len(p[0]))

    def _targets(self, text: str) -> tuple[list[str], str]:
        found: list[str] = []
        for phrase, name in self._alias:
            pattern = r"\b" + re.escape(phrase) + r"\b"
            if phrase and re.search(pattern, text):
                if name not in found:
                    found.append(name)
                text = re.sub(pattern, " ", text)
        if re.search(r"\b(?:las\s+dos|los\s+dos|ambas|ambos|todas|todos)\b", text):
            found = []
        return found, re.sub(r"\s+", " ", text).strip()

    def match_mode(self, said: str) -> str | None:
        said = re.sub(r"^(?:de\s+|del\s+|para\s+)?(?:la\s+|el\s+)?", "", said.strip())
        if said in self.modes:
            return self.modes[said]
        for spoken in sorted(self.modes, key=len, reverse=True):
            if said.startswith(spoken + " "):
                return self.modes[spoken]
        best, ratio = None, 0.8
        for spoken, mode in self.modes.items():
            r = SequenceMatcher(None, said, spoken).ratio()
            if r >= ratio:
                best, ratio = mode, r
        return best

    def parse(self, text: str) -> LightCommand | None:
        norm = _END.sub("", normalize(text))
        m = _HEAD.match(norm)
        if not m:
            return None
        verb, rest = m.group("verb"), m.group("rest").strip()
        rest = re.sub(r"^(?:todas\s+|todos\s+)", "", rest)

        # «más luz» / «menos luz»
        mm = re.match(r"^(mas|menos)\s+" + _ART + _LIGHT + r"\b(.*)$", rest) if not verb else None
        if mm:
            targets, _ = self._targets(mm.group(2))
            return LightCommand(BRIGHTER if mm.group(1) == "mas" else DIMMER, targets)

        mode_m = re.match(r"^(?:(?:el|en|a)\s+)?modo\s+(?P<mode>.+)$", rest)
        targets, tail = self._targets(rest)
        light_ref = bool(re.match(_ART + _LIGHT + r"\b", rest) or re.match(r"^(?:el\s+)?(?:brillo|intensidad)\b", rest))
        light_ref = light_ref or (bool(targets) and verb is not None)
        if mode_m and not light_ref:
            # «modo lectura» sin mencionar la luz: solo si es un modo que conocemos.
            if (verb is None or verb in _VERB_SET | _VERB_ON) and self.match_mode(mode_m.group("mode")):
                light_ref = True
        if not light_ref:
            return None
        if verb is None and not re.match(_ART + _LIGHT + r"\b|^(?:el\s+)?(?:brillo|intensidad)\b|^(?:(?:el|en)\s+)?modo\b",
                                         rest):
            return None

        mode_s = re.search(r"\bmodo\s+(?P<mode>.+)$", tail)
        if mode_s:
            said = mode_s.group("mode").strip()
            return LightCommand(MODE, targets, self.match_mode(said), raw_mode=said)
        if verb in _VERB_OFF:
            return LightCommand(OFF, targets)

        pct = re.search(r"\b(\d{1,3})\b", tail)
        if pct:
            return LightCommand(BRIGHTNESS, targets, max(1, min(100, int(pct.group(1)))))
        ext = re.search(r"\b(maximo|tope|minimo)\b", tail)
        if ext:
            return LightCommand(BRIGHTNESS, targets, 10 if ext.group(1) == "minimo" else 100)
        for word in _words(tail):
            if word in _COLOUR_WORDS:
                return LightCommand(COLOUR, targets, _COLOUR_WORDS[word])
        # «calidad»: así transcribe Whisper «cálida» (log del 25-10-2026).
        if re.search(r"\b(?:calida|calido|calidas|calidos|calidita|calidad|amarillenta|tibia)\b", tail):
            return LightCommand(TEMP, targets, 0)
        if re.search(r"\b(?:fria|frio|fresca)\b", tail):
            return LightCommand(TEMP, targets, 100)
        if re.search(r"\bneutr[ao]\b", tail):
            return LightCommand(TEMP, targets, 50)
        if re.search(r"\bblanc[oa]s?\b", tail):
            return LightCommand(WHITE, targets)
        if verb in _VERB_UP or re.search(r"\bmas\b", tail):
            return LightCommand(BRIGHTER, targets)
        if verb in _VERB_DOWN or re.search(r"\bmenos\b", tail):
            return LightCommand(DIMMER, targets)
        if verb in _VERB_ON:
            return LightCommand(ON, targets)
        leftover = [w for w in _words(tail) if w not in _FILLER]
        if verb in _VERB_SET and not leftover:
            return LightCommand(ON, targets)  # «pon la luz»
        return None


def _read_json(path: str, default):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        return default


class SmartLights:
    def __init__(self, cfg: dict, devices_file: str, modes_file: str, device_factory=None):
        self.cfg = cfg
        self.devices_file = devices_file
        self.modes_file = modes_file
        self.timeout = float(cfg["timeout_seconds"])
        self.step = int(cfg["brightness_step"])
        self.lamps = [Lamp(d["name"], d.get("label") or d["name"], list(d.get("aliases", [])))
                      for d in cfg["lamps"]]
        self.modes: dict[str, dict] = {}
        self._factory = device_factory or self._tinytuya_device
        self._devices: dict[str, object] = {}
        self._locks = {lamp.name: threading.Lock() for lamp in self.lamps}
        self._mtimes: tuple = ()
        self._lock = threading.Lock()
        self.error: str | None = None
        self.parser = LightParser(self.lamps, [])
        self.reload()

    # --- archivos ------------------------------------------------------------------------
    def _file_mtimes(self) -> tuple:
        out = []
        for path in (self.devices_file, self.modes_file):
            try:
                out.append(os.path.getmtime(path))
            except OSError:
                out.append(None)
        return tuple(out)

    def reload(self, force: bool = False) -> None:
        with self._lock:
            mtimes = self._file_mtimes()
            if mtimes == self._mtimes and not force:
                return
            self._mtimes = mtimes
            self._devices.clear()
            modes = _read_json(self.modes_file, {})
            self.modes = {normalize(k): v for k, v in modes.items() if isinstance(v, dict)}
            self.parser = LightParser(self.lamps, list(self.modes))
            devices = _read_json(self.devices_file, None)
            if devices is None:
                self.error = "faltan las lámparas: ejecuta tools/descubrir_luces.py (docs/luces.md)"
                return
            by_name = {normalize(d.get("name", "")): d for d in devices if isinstance(d, dict)}
            missing = []
            for lamp in self.lamps:
                d = by_name.get(normalize(lamp.name))
                if not d or not d.get("id") or not d.get("key"):
                    missing.append(lamp.name)
                    continue
                lamp.id, lamp.key, lamp.ip = d["id"], d["key"], d.get("ip") or ""
                lamp.version = float(d.get("version") or 3.3)
                if not lamp.ip:
                    missing.append(f"{lamp.name} (sin IP)")
            self.error = ("no encuentro " + ", ".join(missing)) if missing else None

    def summary(self) -> str:
        if self.error:
            raise LightError(self.error)
        names = ", ".join(f"{lamp.name} ({lamp.ip})" for lamp in self.lamps)
        return f"{names}; {len(self.modes)} modos guardados"

    def parse(self, text: str) -> LightCommand | None:
        self.reload()
        return self.parser.parse(text)

    # --- dispositivos --------------------------------------------------------------------
    def _tinytuya_device(self, lamp: Lamp):
        import tinytuya
        d = tinytuya.BulbDevice(dev_id=lamp.id, address=lamp.ip, local_key=lamp.key, version=lamp.version)
        d.set_socketTimeout(self.timeout)
        d.set_socketRetryLimit(2)
        return d

    def _device(self, lamp: Lamp):
        d = self._devices.get(lamp.name)
        if d is None:
            d = self._devices[lamp.name] = self._factory(lamp)
        return d

    @staticmethod
    def _check(result):
        if isinstance(result, dict) and "Error" in result:
            raise LightError(str(result["Error"]))
        return result

    def _current_brightness(self, d, state: dict) -> float:
        if state.get("mode") == "colour":
            return self._check(d.colour_hsv(state=state))[2] * 100
        return d.get_brightness_percentage(state=state)

    def _apply(self, lamp: Lamp, cmd: LightCommand) -> None:
        with self._locks[lamp.name]:
            d = self._device(lamp)
            if cmd.action == OFF:
                self._check(d.turn_off())
                return
            if cmd.action == ON:
                self._check(d.turn_on())
                return
            state = self._check(d.state())
            if not state.get("is_on"):
                self._check(d.turn_on())
            if cmd.action in (BRIGHTNESS, BRIGHTER, DIMMER):
                if cmd.action == BRIGHTNESS:
                    pct = int(cmd.value)
                else:
                    now = self._current_brightness(d, state) if state.get("is_on") else 0
                    pct = now + (self.step if cmd.action == BRIGHTER else -self.step)
                pct = max(10, min(100, int(round(pct))))
                self._check(d.set_brightness_percentage(pct))
            elif cmd.action == COLOUR:
                self._check(d.set_colour(*COLOURS[cmd.value]))
            elif cmd.action == WHITE:
                self._check(d.set_mode("white"))
            elif cmd.action == TEMP:
                self._check(d.set_colourtemp_percentage(int(cmd.value)))
            elif cmd.action == MODE:
                saved = self.modes.get(cmd.value, {})
                dps = saved.get(lamp.name) or saved.get("*")
                if dps:
                    self._check(d.set_multiple_values(dict(dps)))
                elif cmd.value in SCENES and d.dpset.get("mode") and d.dpset.get("scene"):
                    self._check(d.set_multiple_values({d.dpset["mode"]: "scene",
                                                       d.dpset["scene"]: SCENES[cmd.value]}))
                elif cmd.value in BUILTIN_MODES:
                    self._check(d.set_mode(BUILTIN_MODES[cmd.value]))
                else:
                    raise LightError(f"el modo {cmd.value} no está disponible en {lamp.name}")

    def run(self, cmd: LightCommand) -> str:
        """Ejecuta la orden en las lámparas indicadas (en paralelo) y devuelve la frase para Vimo."""
        self.reload()
        if self.error:
            return f"Las luces aún no están configuradas: {self.error}."
        lamps = [lamp for lamp in self.lamps if not cmd.targets or lamp.name in cmd.targets]
        saved = self.modes.get(cmd.value) if cmd.action == MODE else None
        if saved and not cmd.targets and cmd.value not in SCENES and cmd.value not in BUILTIN_MODES:
            lamps = [lamp for lamp in lamps if lamp.name in saved] or lamps  # modo de una sola lámpara
        if cmd.action == MODE and cmd.value is None:
            known = sorted(set(self.modes) | set(SCENES) | {"blanco", "color"})
            return f"No conozco el modo {cmd.raw_mode}. Tengo: {', '.join(known)}."
        failed: list[tuple[Lamp, str]] = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(lamps), thread_name_prefix="light") as pool:
            futures = {pool.submit(self._apply, lamp, cmd): lamp for lamp in lamps}
            done, pending = concurrent.futures.wait(futures, timeout=self.timeout * 4 + 2)
            for fut in pending:
                failed.append((futures[fut], "no respondió a tiempo"))
            for fut in done:
                exc = fut.exception()
                if exc is not None:
                    failed.append((futures[fut], str(exc) or type(exc).__name__))
                    self._devices.pop(futures[fut].name, None)  # reconectar la próxima vez
        for lamp, msg in failed:
            log.warning("Luz %s: %s", lamp.name, msg)
        ok = [lamp for lamp in lamps if lamp not in [f[0] for f in failed]]
        if not ok:
            return f"No pude conectar con {self._label(lamps)}. Revisa que esté encendida y en la WiFi."
        text = self._reply(cmd, ok)
        if failed:
            text += f" Pero no pude con {self._label([f[0] for f in failed])}."
        return text

    def _label(self, lamps: list[Lamp]) -> str:
        if len(lamps) == len(self.lamps) and len(lamps) > 1:
            return "las dos lámparas" if len(lamps) == 2 else "las lámparas"
        return " y ".join(lamp.label for lamp in lamps)

    def _reply(self, cmd: LightCommand, lamps: list[Lamp]) -> str:
        label = self._label(lamps)
        if cmd.action == ON:
            return f"Listo, prendí {label}."
        if cmd.action == OFF:
            return f"Listo, apagué {label}."
        if cmd.action == BRIGHTNESS:
            return f"Puse {label} al {cmd.value} por ciento."
        if cmd.action == BRIGHTER:
            return "Subí la luz."
        if cmd.action == DIMMER:
            return "Bajé la luz."
        if cmd.action == COLOUR:
            return f"Listo, luz {cmd.value}."
        if cmd.action == WHITE:
            return "Listo, luz blanca."
        if cmd.action == TEMP:
            return "Listo, luz cálida." if cmd.value == 0 else ("Listo, luz fría." if cmd.value == 100 else
                                                                 "Listo, luz neutra.")
        return f"Modo {cmd.value} activado."
