"""Cancelación de eco: configuración generada, ~/.asoundrc y búsqueda del micrófono (sin PipeWire real)."""
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import echo_cancel  # noqa: E402


class EchoCancelTest(unittest.TestCase):
    def test_asoundrc_added_once_and_keeps_existing(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".asoundrc")
            with open(path, "w") as fh:
                fh.write("pcm.mio { type hw card 0 }")
            self.assertTrue(echo_cancel.ensure_alsa_pcms(path))
            self.assertFalse(echo_cancel.ensure_alsa_pcms(path))          # idempotente
            text = open(path).read()
            self.assertIn("pcm.mio { type hw card 0 }", text)              # no toca lo existente
            self.assertEqual(text.count("pcm.vimo_aec_in"), 1)
            self.assertIn('capture_node "vimo_aec_source"', text)
            self.assertIn('playback_node "vimo_aec_sink"', text)

    def test_generated_config(self):
        with tempfile.TemporaryDirectory() as d:
            ec = echo_cancel.EchoCanceller(os.path.join(d, "aec.conf"), "mic_real", None,
                                           {"webrtc.noise_suppression": False, "webrtc.high_pass_filter": True})
            ec._write_conf("altavoz_real", "mic_real")
            conf = open(os.path.join(d, "aec.conf")).read()
            for expected in ('library.name = aec/libspa-aec-webrtc', 'webrtc.noise_suppression = false',
                             'webrtc.high_pass_filter = true', 'target.object = "mic_real"',
                             'target.object = "altavoz_real"', 'node.name = "vimo_aec_sink"',
                             'node.name = "vimo_aec_source"'):
                self.assertIn(expected, conf)

    def test_find_source_by_name(self):
        nodes = {"alsa_input.usb-Clip-on_USB_microphone_iTalk-02-00.mono-fallback":
                 {"media.class": "Audio/Source", "node.description": "iTalk-02 Mono"},
                 "vimo_aec_source": {"media.class": "Audio/Source", "node.description": "Vimo AEC"},
                 "bluez_output.x": {"media.class": "Audio/Sink", "node.description": "BT-017"}}
        with mock.patch.object(echo_cancel, "_nodes", return_value=nodes):
            self.assertEqual(echo_cancel.find_source("iTalk"),
                             "alsa_input.usb-Clip-on_USB_microphone_iTalk-02-00.mono-fallback")
            self.assertIsNone(echo_cancel.find_source("vimo"))              # nunca el propio filtro
            self.assertIsNone(echo_cancel.find_source("BT-017"))            # solo micrófonos


if __name__ == "__main__":
    unittest.main()
