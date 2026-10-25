"""Caras de música: animaciones generadas y elección de cara en la GUI según estado y música."""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.animation_manager import FaceLibrary  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MUSIC_SETS = ("music_dance", "music_sing", "music_vibe", "music_wow")


class MusicFacesFilesTest(unittest.TestCase):
    def test_generated_sets_exist_with_original_size(self):
        lib = FaceLibrary(os.path.join(ROOT, "faces"))
        for name in MUSIC_SETS:
            self.assertIn(name, lib.frames, name)
            self.assertGreaterEqual(len(lib.frames[name]), 4)
            self.assertTrue(all(im.size == (800, 480) for im in lib.frames[name]))
            # Animación real: los fotogramas no son idénticos.
            self.assertNotEqual(lib.frames[name][0].tobytes(), lib.frames[name][1].tobytes())

    def test_missing_music_set_falls_back_to_idle(self):
        lib = FaceLibrary(os.path.join(ROOT, "faces"))
        self.assertEqual(lib.resolve("music_inexistente"), "idle")


@unittest.skipUnless(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"), "sin pantalla")
class GuiMusicFaceTest(unittest.TestCase):
    def setUp(self):
        from app.config_manager import ConfigManager
        from app.controller import VimoController
        from app.gui import VimoGUI

        self.tmp = tempfile.TemporaryDirectory()
        cfg = ConfigManager(os.path.join(self.tmp.name, "config.json"))
        cfg.update({"gui.fullscreen": False, "memory.path": os.path.join(self.tmp.name, "m.json")})
        self.gui = VimoGUI(VimoController(cfg))
        self.gui.root.withdraw()

    def tearDown(self):
        self.gui._cancel_music_switch()
        self.gui.animator.stop()
        self.gui.root.destroy()
        self.tmp.cleanup()

    def music(self, title="Canción A", paused=False, active=True):
        return ("music", {"active": active, "paused": paused, "volume": 60, "genre": None,
                          "title": title if active else None, "channel": "c", "queue": 3})

    def face(self):
        return self.gui.animator._state

    def test_face_follows_state_and_music(self):
        from app.state_manager import State
        g = self.gui
        g._handle(("state", State.IDLE))
        self.assertEqual(self.face(), "idle")
        g._handle(self.music("Canción A"))
        self.assertIn(self.face(), MUSIC_SETS)
        first = self.face()
        # Al hablar con Vimo vuelven sus caras normales.
        g._handle(("state", State.LISTENING))
        self.assertEqual(self.face(), "listening")
        g._handle(("state", State.IDLE))
        self.assertEqual(self.face(), first)                 # misma animación para la misma canción
        g._handle(self.music("Canción B"))                   # canción nueva → otra animación
        self.assertIn(self.face(), MUSIC_SETS)
        self.assertNotEqual(self.face(), first)
        g._handle(self.music("Canción B", paused=True))      # en pausa → cara de reposo
        self.assertEqual(self.face(), "idle")
        g._handle(self.music(active=False))                  # parada
        self.assertEqual(self.face(), "idle")
        self.assertIsNone(g._music_switch_id)

    def test_rotation_timer(self):
        from app.state_manager import State
        g = self.gui
        g._handle(("state", State.IDLE))
        g._handle(self.music("X"))
        self.assertIsNotNone(g._music_switch_id)
        before = self.face()
        g._rotate_music_face()
        self.assertNotEqual(self.face(), before)
        self.assertIsNotNone(g._music_switch_id)


if __name__ == "__main__":
    unittest.main()
