"""Órdenes de luces de Smart Life (Tuya) con lámparas simuladas y su integración en el controlador."""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import intents  # noqa: E402
from app import smart_lights as sl  # noqa: E402
from app.config_manager import DEFAULTS  # noqa: E402


class FakeBulb:
    def __init__(self, lamp, fail=False):
        self.lamp = lamp
        self.fail = fail
        self.dpset = {"switch": "20", "mode": "21", "scene": "25"}
        self.dps = {"20": False, "21": "white", "22": 500}
        self.calls = []

    def _ok(self, name, *args):
        self.calls.append((name, *args))
        if self.fail:
            return {"Error": "Network Error: Device Unreachable"}
        return {"dps": dict(self.dps)}

    def turn_on(self):
        r = self._ok("on")
        self.dps["20"] = True
        return r

    def turn_off(self):
        r = self._ok("off")
        self.dps["20"] = False
        return r

    def state(self):
        if self.fail:
            return {"Error": "Network Error: Device Unreachable"}
        return {"is_on": self.dps["20"], "mode": self.dps["21"]}

    def get_brightness_percentage(self, state=None):
        return self.dps["22"] / 10

    def colour_hsv(self, state=None):
        return (0.0, 1.0, 0.5)

    def set_brightness_percentage(self, pct):
        self.dps["22"] = pct * 10
        return self._ok("brightness", pct)

    def set_colour(self, r, g, b):
        self.dps["21"] = "colour"
        return self._ok("colour", r, g, b)

    def set_mode(self, mode):
        self.dps["21"] = mode
        return self._ok("mode", mode)

    def set_colourtemp_percentage(self, pct):
        return self._ok("temp", pct)

    def set_multiple_values(self, dps):
        self.dps.update(dps)
        return self._ok("multi", dps)


def lamp_cfg():
    return json.loads(json.dumps(DEFAULTS["lights"]))


class LightsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.devices = os.path.join(self.tmp.name, "devices.json")
        self.modes = os.path.join(self.tmp.name, "modes.json")
        with open(self.devices, "w") as fh:
            json.dump([{"name": "lampara_cuarto_izq", "id": "a", "key": "k", "ip": "10.0.0.1", "version": "3.5"},
                       {"name": "lampara_cuarto_der", "id": "b", "key": "k", "ip": "10.0.0.2", "version": "3.5"},
                       {"name": "sala", "id": "c", "key": "k"}], fh)
        self.bulbs = {}
        self.failing = set()

        def factory(lamp):
            b = self.bulbs[lamp.name] = FakeBulb(lamp, lamp.name in self.failing)
            return b
        self.lights = sl.SmartLights(lamp_cfg(), self.devices, self.modes, device_factory=factory)

    def tearDown(self):
        self.tmp.cleanup()

    def say(self, text):
        cmd = self.lights.parse(text)
        self.assertIsNotNone(cmd, text)
        return self.lights.run(cmd)

    def test_phrases(self):
        izq, der = "lampara_cuarto_izq", "lampara_cuarto_der"
        cases = {
            "Prende las luces": (sl.ON, [], None),
            "Hey Vimo, apaga la luz por favor": (sl.OFF, [], None),
            "apaga la lámpara derecha": (sl.OFF, [der], None),
            "prende la de la izquierda": (sl.ON, [izq], None),
            "apaga las dos lámparas": (sl.OFF, [], None),
            "pon la luz al 50%": (sl.BRIGHTNESS, [], 50),
            "brillo al máximo": (sl.BRIGHTNESS, [], 100),
            "sube la luz": (sl.BRIGHTER, [], None),
            "menos luz": (sl.DIMMER, [], None),
            "pon la luz roja": (sl.COLOUR, [], "rojo"),
            "Por la luz roja.": (sl.COLOUR, [], "rojo"),        # Whisper, log del 25-10-2026
            "Aprende las luces": (sl.ON, [], None),
            "por favor prende la luz": (sl.ON, [], None),
            "cambia la lámpara izquierda a azul": (sl.COLOUR, [izq], "azul"),
            "luz cálida": (sl.TEMP, [], 0),
            "Pon la luz calidad.": (sl.TEMP, [], 0),          # Whisper, log del 25-10-2026
            "¡Polo luz calidad!": (sl.TEMP, [], 0),
            "pon la luz blanca": (sl.WHITE, [], None),
            "modo lectura": (sl.MODE, [], "lectura"),
            "pon el modo fiesta": (sl.MODE, [], "deslumbrante"),
            "pon la lámpara derecha en modo noche": (sl.MODE, [der], "noche"),
            "pon la luz en modo zumba": (sl.MODE, [], None),
        }
        for text, (action, targets, value) in cases.items():
            cmd = self.lights.parse(text)
            self.assertIsNotNone(cmd, text)
            self.assertEqual((cmd.action, cmd.targets, cmd.value), (action, targets, value), text)
        for text in ("pon la canción luz de luna", "pon Luz Casal", "apaga la música", "sube el volumen",
                     "¿qué es la luz?", "pon modo avión", "prende la tele", "pon rojo", "modo zumba",
                     "¿cómo funciona una lámpara?", "por la luz de la luna", "¿por qué la luz es amarilla?"):
            self.assertIsNone(self.lights.parse(text), text)
        # Las de música siguen siendo de música.
        self.assertEqual(intents.detect("pon la canción luz de luna"), intents.MUSIC_PLAY)

    def test_on_off_both_and_one(self):
        self.assertEqual(self.say("prende las luces"), "Listo, prendí las dos lámparas.")
        self.assertTrue(all(b.dps["20"] for b in self.bulbs.values()))
        self.assertEqual(self.say("apaga la lámpara derecha"), "Listo, apagué la lámpara derecha.")
        self.assertFalse(self.bulbs["lampara_cuarto_der"].dps["20"])
        self.assertTrue(self.bulbs["lampara_cuarto_izq"].dps["20"])

    def test_brightness_turns_on_and_steps(self):
        self.say("pon la luz al 40")
        b = self.bulbs["lampara_cuarto_izq"]
        self.assertTrue(b.dps["20"])
        self.assertEqual(b.dps["22"], 400)
        self.say("sube la luz")
        self.assertEqual(b.dps["22"], 600)
        self.say("luz al mínimo")
        self.say("baja la luz")
        self.assertEqual(b.dps["22"], 100)  # no baja de 10 % (apagar es otra orden)

    def test_scene_and_saved_mode(self):
        self.say("modo lectura")
        b = self.bulbs["lampara_cuarto_izq"]
        self.assertEqual(b.dps["21"], "scene")
        self.assertEqual(b.dps["25"], sl.SCENES["lectura"])
        with open(self.modes, "w") as fh:
            json.dump({"cine": {"lampara_cuarto_izq": {"20": True, "21": "colour", "24": "00f003e80064"}}}, fh)
        os.utime(self.modes, (time.time() + 5, time.time() + 5))  # mtime distinto aunque sea el mismo segundo
        self.assertEqual(self.say("pon el modo cine"), "Modo cine activado.")
        b = self.bulbs["lampara_cuarto_izq"]  # al cambiar modes.json se reconecta
        self.assertEqual(b.dps["24"], "00f003e80064")
        self.assertNotEqual(self.bulbs["lampara_cuarto_der"].dps.get("24"), "00f003e80064")
        self.assertIn("No conozco el modo zumba", self.say("pon la luz en modo zumba"))

    def test_unreachable_lamp_is_reported(self):
        self.failing.add("lampara_cuarto_der")
        text = self.say("apaga las luces")
        self.assertEqual(text, "Listo, apagué la lámpara izquierda. Pero no pude con la lámpara derecha.")
        self.failing.add("lampara_cuarto_izq")
        self.lights._devices.clear()
        self.assertIn("No pude conectar con las dos lámparas", self.say("prende las luces"))

    def test_missing_devices_file_explains(self):
        os.remove(self.devices)
        os.utime(self.tmp.name)
        lights = sl.SmartLights(lamp_cfg(), self.devices, self.modes)
        self.assertIn("descubrir_luces", lights.run(lights.parse("prende la luz")))


class ControllerLightsTest(unittest.TestCase):
    def setUp(self):
        from app.config_manager import ConfigManager
        from app.controller import VimoController
        from app.state_manager import State
        from tests.test_controller import FakeClaude, FakePlayer, FakeTTS

        self.tmp = tempfile.TemporaryDirectory()
        cfg = ConfigManager(os.path.join(self.tmp.name, "config.json"))
        cfg.update({"memory.path": os.path.join(self.tmp.name, "mem.json"), "sounds.thinking_sound": False})
        self.c = VimoController(cfg)
        self.c.state.set(State.IDLE)
        self.c.components["Claude Code"] = (True, "fake")
        self.c.tts, self.c.player, self.c.claude = FakeTTS(), FakePlayer(), FakeClaude("Respuesta.")
        devices = os.path.join(self.tmp.name, "devices.json")
        with open(devices, "w") as fh:
            json.dump([{"name": "lampara_cuarto_izq", "id": "a", "key": "k", "ip": "10.0.0.1"},
                       {"name": "lampara_cuarto_der", "id": "b", "key": "k", "ip": "10.0.0.2"}], fh)
        self.bulbs = []
        self.c.lights = sl.SmartLights(lamp_cfg(), devices, os.path.join(self.tmp.name, "modes.json"),
                                       device_factory=lambda lamp: self.bulbs.append(FakeBulb(lamp)) or self.bulbs[-1])

    def tearDown(self):
        self.tmp.cleanup()

    def test_light_order_is_local_and_spoken(self):
        said = []
        self.c._speak = lambda text, timings=None: said.append(text)
        self.c.submit_text("prende las luces")
        t0 = time.monotonic()
        while self.c._turn_lock.locked() and time.monotonic() - t0 < 5:
            time.sleep(0.02)
        self.assertEqual(said, ["Listo, prendí las dos lámparas."])
        self.assertEqual(self.c.claude.prompts, [])
        self.assertTrue(all(b.dps["20"] for b in self.bulbs))


if __name__ == "__main__":
    unittest.main()
