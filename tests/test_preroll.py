"""Pre-buffer de la consulta: lo dicho justo tras «Hey Vimo» (durante el pitido) no se pierde."""
import os
import sys
import threading
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.speech_to_text import MicHistory, NoiseTracker, UtteranceRecorder  # noqa: E402

CFG = {"energy_threshold": 0.012, "noise_multiplier": 2.5, "silence_seconds": 0.4, "max_seconds": 5.0,
       "no_speech_timeout_seconds": 3.0, "min_speech_seconds": 0.2}
VOICE = (np.sin(np.arange(1280) / 5) * 8000).astype(np.int16)
SILENCE = np.zeros(1280, np.int16)


class PrerollTest(unittest.TestCase):
    def feed_silence_later(self, hist, n=20):
        def run():
            time.sleep(0.05)
            for _ in range(n):
                hist(SILENCE)
                time.sleep(0.005)
        threading.Thread(target=run, daemon=True).start()

    def record(self, hist, since):
        rec = UtteranceRecorder(mic=None, noise=NoiseTracker(), cfg=CFG)
        return rec.record(threading.Event(), hist, since)

    def test_speech_during_ack_is_kept(self):
        hist = MicHistory()
        hist(VOICE)                      # «…Vimo»: antes de la detección, no entra
        since = time.monotonic()
        for _ in range(6):               # «reproduce…» mientras suena el pitido
            hist(VOICE)
        self.feed_silence_later(hist)
        utt = self.record(hist, since)
        self.assertEqual(utt.reason, "silence")
        self.assertGreaterEqual(utt.speech_seconds, 6 * 0.08 - 1e-6)

    def test_beep_dropped_without_aec(self):
        hist = MicHistory()
        since = time.monotonic()
        hist.keep_while_playing, hist.playing = False, True
        for _ in range(4):               # el pitido (sin cancelación de eco)
            hist(VOICE)
        hist.playing = False
        self.feed_silence_later(hist, n=50)
        utt = self.record(hist, since)
        self.assertFalse(utt.has_speech)  # el pitido no cuenta como voz


if __name__ == "__main__":
    unittest.main()
