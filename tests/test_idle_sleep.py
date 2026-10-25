"""Gestos en reposo, pantalla «apagada» por inactividad y retroiluminación (sin tocar la pantalla real)."""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.animation_manager import FaceLibrary  # noqa: E402
from app.display import Backlight  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IDLE_SETS = ("idle_blink", "idle_look", "idle_smile", "idle_breathe", "idle_yawn", "idle_sleepy")


class IdleFacesFilesTest(unittest.TestCase):
    def test_generated_sets(self):
        lib = FaceLibrary(os.path.join(ROOT, "faces"))
        for name in IDLE_SETS:
            self.assertIn(name, lib.frames, name)
            frames = lib.frames[name]
            self.assertGreaterEqual(len(frames), 5)
            self.assertTrue(all(im.size == (800, 480) for im in frames))
            self.assertTrue(len({im.tobytes() for im in frames}) > 1, f"{name} no se mueve")
        self.assertEqual(lib.resolve("idle_inexistente"), "idle")


class BacklightTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dev = os.path.join(self.tmp.name, "brightness")
        with open(self.dev, "w") as fh:
            fh.write("200")
        self.saved = os.path.join(self.tmp.name, "data", "backlight.saved")

    def tearDown(self):
        self.tmp.cleanup()

    def read(self):
        return open(self.dev).read().strip()

    def test_off_on(self):
        bl = Backlight(self.saved, self.dev)
        bl.off()
        self.assertEqual(self.read(), "0")
        self.assertTrue(os.path.exists(self.saved))        # guardado para cierres bruscos
        bl.on()
        self.assertEqual(self.read(), "200")
        self.assertFalse(os.path.exists(self.saved))

    def test_restore_after_crash(self):
        Backlight(self.saved, self.dev).off()               # «se cierra de golpe» con la pantalla apagada
        Backlight(self.saved, self.dev).restore_saved()     # siguiente arranque
        self.assertEqual(self.read(), "200")

    def test_missing_device_is_harmless(self):
        bl = Backlight(self.saved, os.path.join(self.tmp.name, "no_existe"))
        bl.off()
        bl.on()
        self.assertFalse(os.path.exists(self.saved))


@unittest.skipUnless(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"), "sin pantalla")
class GuiIdleSleepTest(unittest.TestCase):
    def setUp(self):
        from app.config_manager import ConfigManager
        from app.controller import VimoController
        from app.gui import VimoGUI
        from app.state_manager import State

        self.State = State
        self.tmp = tempfile.TemporaryDirectory()
        self.dev = os.path.join(self.tmp.name, "brightness")
        with open(self.dev, "w") as fh:
            fh.write("255")
        cfg = ConfigManager(os.path.join(self.tmp.name, "config.json"))
        cfg.update({"gui.fullscreen": False, "memory.path": os.path.join(self.tmp.name, "m.json"),
                    "gui.backlight_path": self.dev, "gui.sleep_after_seconds": 1})
        self.gui = VimoGUI(VimoController(cfg))
        self.gui.backlight.saved_path = os.path.join(self.tmp.name, "backlight.saved")
        self.gui.root.withdraw()
        self.gui._handle(("state", State.IDLE))

    def tearDown(self):
        self.gui._wake("fin de prueba")
        self.gui._cancel_idle_gesture()
        self.gui.animator.stop()
        self.gui.root.destroy()
        self.tmp.cleanup()

    def bright(self):
        return open(self.dev).read().strip()

    def test_idle_gesture_plays_and_returns(self):
        g = self.gui
        self.assertEqual(g.animator._state, "idle")
        self.assertIsNotNone(g._idle_timer)                  # gesto programado
        g.root.after_cancel(g._idle_timer)
        g._idle_timer = None
        g._play_idle_gesture()
        self.assertIn(g.animator._state, IDLE_SETS)
        g._end_idle_gesture()
        self.assertEqual(g.animator._state, "idle")
        g._handle(("state", self.State.LISTENING))           # al hablar, gestos fuera
        self.assertEqual(g.animator._state, "listening")
        self.assertIsNone(g._idle_timer)

    def test_sleep_after_inactivity_and_wake_by_touch(self):
        g = self.gui
        g._last_activity = time.monotonic() - 5
        g._sleep_check()
        self.assertTrue(g._sleeping)
        self.assertEqual(self.bright(), "0")
        self.assertIsNotNone(g._sleep_overlay)
        g._on_user_activity()                                # toque / ratón
        self.assertFalse(g._sleeping)
        self.assertEqual(self.bright(), "255")
        self.assertIsNone(g._sleep_overlay)

    def test_wake_word_wakes_screen(self):
        g = self.gui
        g._last_activity = time.monotonic() - 5
        g._sleep_check()
        self.assertTrue(g._sleeping)
        g._handle(("state", self.State.LISTENING))           # «Hey Vimo»
        self.assertFalse(g._sleeping)
        self.assertEqual(self.bright(), "255")
        self.assertEqual(g.animator._state, "listening")

    def test_activity_event_wakes_immediately(self):
        g = self.gui
        g._last_activity = time.monotonic() - 5
        g._sleep_check()
        self.assertTrue(g._sleeping)
        g._handle(("activity", "wake word"))                 # antes del pitido y de LISTENING
        self.assertFalse(g._sleeping)
        self.assertEqual(self.bright(), "255")

    def test_no_sleep_while_music_or_busy(self):
        g = self.gui
        g._handle(("music", {"active": True, "paused": False, "volume": 50, "genre": None,
                             "title": "X", "channel": "c", "queue": 1}))
        g._last_activity = time.monotonic() - 5
        g._sleep_check()
        self.assertFalse(g._sleeping)
        g._handle(("music", {"active": False, "paused": False, "volume": 50, "genre": None,
                             "title": None, "channel": None, "queue": 0}))
        g._handle(("state", self.State.THINKING))
        g._last_activity = time.monotonic() - 5
        g._sleep_check()
        self.assertFalse(g._sleeping)


if __name__ == "__main__":
    unittest.main()
