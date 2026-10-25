"""Ajustes de la wake word: bloques seguidos, VAD, aviso temprano y atenuación de la música."""
import os
import sys
import tempfile
import threading
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.wake_word import TriggerLogic, WakeWordDetector  # noqa: E402
from tests.test_audio_search import FakeOWW  # noqa: E402
from tests.test_music import FakeBackend, FakeSource, MIX, wait_for  # noqa: E402


def run(logic, scores, vads=None, step=0.08):
    fired = []
    for i, s in enumerate(scores):
        hist = list(vads[max(0, i - 10):i + 1]) if vads is not None else None
        if logic.feed(s, i * step, hist):
            fired.append(i)
    return fired


class TriggerLogicTest(unittest.TestCase):
    def test_threshold_and_cooldown(self):
        logic = TriggerLogic(0.5, cooldown=1.0)
        self.assertEqual(run(logic, [0.1, 0.6, 0.7, 0.1, 0.6] + [0.0] * 20 + [0.9]), [1, 25])

    def test_patience_requires_consecutive_blocks(self):
        logic = TriggerLogic(0.5, patience=2)
        self.assertEqual(run(logic, [0.6, 0.1, 0.6, 0.1]), [])     # picos aislados: no
        logic = TriggerLogic(0.5, patience=2)
        self.assertEqual(run(logic, [0.1, 0.6, 0.7, 0.1]), [2])    # dos seguidos: sí

    def test_vad_gate(self):
        scores = [0.0] * 8 + [0.9]
        speech = [0.9] * 9
        silence = [0.02] * 9
        self.assertEqual(run(TriggerLogic(0.5, vad_threshold=0.5), scores, speech), [8])
        logic = TriggerLogic(0.5, vad_threshold=0.5)
        self.assertEqual(run(logic, scores, silence), [])
        self.assertEqual(logic.vad_rejected, 1)
        self.assertEqual(run(TriggerLogic(0.5, vad_threshold=0.0), scores, silence), [8])  # VAD apagado


class DetectorPartialTest(unittest.TestCase):
    def make(self, scores, **kw):
        d = WakeWordDetector("x.onnx", "m.onnx", "e.onnx", threshold=0.5, cooldown_seconds=0,
                             resume_delay_seconds=0, partial_threshold=0.15, **kw)
        d.model = FakeOWW(scores)
        d.name = "hey_vimo"
        return d

    def test_partial_then_detection(self):
        d = self.make([0.05, 0.2, 0.3, 0.8])
        partial, hits = [], []
        d.on_partial, d.on_detect = partial.append, hits.append
        for _ in range(4):
            d.process(np.zeros(1280, np.int16))
        self.assertEqual(len(partial), 1)          # limitado a uno cada 0,5 s
        self.assertEqual(len(hits), 1)

    def test_patience_in_detector(self):
        d = self.make([0.6, 0.1, 0.6, 0.7], patience_frames=2)
        hits = []
        d.on_detect = hits.append
        for _ in range(4):
            d.process(np.zeros(1280, np.int16))
        self.assertEqual(len(hits), 1)

    def test_threshold_setter_kept(self):
        d = self.make([])
        d.threshold = 0.3
        self.assertEqual(d.logic.threshold, 0.3)


class DuckingTest(unittest.TestCase):
    def setUp(self):
        from app.config_manager import ConfigManager
        from app.controller import VimoController
        from app.music_player import MusicPlayer
        from app.state_manager import State

        self.tmp = tempfile.TemporaryDirectory()
        cfg = ConfigManager(os.path.join(self.tmp.name, "config.json"))
        cfg.update({"memory.path": os.path.join(self.tmp.name, "m.json"), "music.duck_seconds": 0.6})
        self.c = VimoController(cfg)
        self.c.state.set(State.IDLE)
        self.be = FakeBackend()
        self.c.music = MusicPlayer(FakeSource(os.path.join(self.tmp.name, "c"), {"aaaaaaaaaa1": MIX}), self.be,
                                   volume=80)
        self.c.music.play(self.c.music.find("x"))

    def tearDown(self):
        self.c.music.shutdown()
        self.tmp.cleanup()

    def test_duck_and_restore(self):
        self.c._on_wake_partial(0.2)
        self.assertTrue(wait_for(lambda: self.be.volume == 24, timeout=2))  # 80 × 0,3 (en otro hilo)
        self.assertEqual(self.c.music.volume, 80)          # el volumen elegido no cambia
        self.c._on_wake_partial(0.3)                       # otro aviso alarga la atenuación
        self.assertEqual(self.be.volume, 24)
        self.assertTrue(wait_for(lambda: self.be.volume == 80, timeout=3))

    def test_no_duck_when_paused(self):
        self.c.music.pause()
        self.c._on_wake_partial(0.2)
        self.assertNotEqual(self.be.volume, 24)


if __name__ == "__main__":
    unittest.main()
