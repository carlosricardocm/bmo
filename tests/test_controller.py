"""Flujo del controlador con componentes simulados (sin audio, cámara ni Claude reales)."""
import concurrent.futures
import os
import sys
import tempfile
import threading
import time
import unittest

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.claude_code_client import ClaudeError, ClaudeResponse  # noqa: E402
from app.config_manager import ConfigManager  # noqa: E402
from app.controller import VimoController  # noqa: E402
from app.state_manager import State  # noqa: E402


class FakeClaude:
    def __init__(self, reply="Hola, soy Vimo.", error=None, delay=0.0):
        self.reply, self.error, self.delay = reply, error, delay
        self.cancelled = threading.Event()
        self.prompts = []

    def submit(self, text, on_progress=None):
        self.prompts.append(text)
        fut = concurrent.futures.Future()

        def run():
            if self.cancelled.wait(self.delay):
                fut.set_exception(ClaudeError("cancelada", "cancelled"))
            elif self.error:
                fut.set_exception(self.error)
            else:
                if on_progress:
                    on_progress("search", {"query": "x"})
                fut.set_result(ClaudeResponse(self.reply, "sid", sources=[{"title": "A", "url": "https://a.com"}]))
        threading.Thread(target=run, daemon=True).start()
        return fut

    def cancel(self):
        self.cancelled.set()

    def reset_session(self):
        pass

    def shutdown(self):
        pass


class FakeTTS:
    voice = object()

    def stream(self, segments, stop, stats=None):
        for s in segments:
            if stop.is_set():
                return
            yield np.zeros(2205, np.int16), 22050

    def set_length_scale(self, v):
        pass


class FakePlayer:
    """Consume el generador en un hilo, simulando tiempo de reproducción."""

    def __init__(self, seconds_per_chunk=0.05):
        self.spc = seconds_per_chunk
        self._stop = threading.Event()
        self.played = 0
        self.on_start = self.on_end = None

    def play_stream(self, chunks, volume=1.0, block=False, on_done=None):
        self._stop.clear()

        def run():
            interrupted = False
            for _ in chunks:
                if self._stop.wait(self.spc):
                    interrupted = True
                    break
                self.played += 1
            if on_done:
                on_done(interrupted)
        threading.Thread(target=run, daemon=True).start()

    def play(self, *a, **k):
        pass

    @property
    def playing(self):
        return False

    def stop(self, wait=True):
        self._stop.set()


class FakeCamera:
    available = True

    def take_photo(self):
        time.sleep(0.05)
        return "/tmp/foto.jpg"

    def close(self):
        pass


class ControllerFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = ConfigManager(os.path.join(self.tmp.name, "config.json"))
        cfg.update({"memory.path": os.path.join(self.tmp.name, "mem.json"), "sounds.thinking_sound": False})
        self.c = VimoController(cfg)
        self.c.state.set(State.IDLE)
        self.c.components["Claude Code"] = (True, "fake")
        self.c.tts = FakeTTS()
        self.c.player = FakePlayer()
        self.states = []
        self.c.state.add_listener(lambda old, new, reason: self.states.append(new))

    def tearDown(self):
        self.tmp.cleanup()

    def wait_idle(self, timeout=5.0):
        t0 = time.monotonic()
        while time.monotonic() - t0 < timeout:
            if not self.c._turn_lock.locked():
                return
            time.sleep(0.02)
        self.fail("el turno no terminó")

    def drain(self):
        out = []
        while not self.c.events.empty():
            out.append(self.c.events.get_nowait())
        return out

    def test_text_turn_states_follow_operations(self):
        self.c.claude = FakeClaude("Hola Ricardo. ¿Qué tal?", delay=0.2)
        self.c.submit_text("hola")
        self.wait_idle()
        self.assertEqual(self.states, [State.THINKING, State.SPEAKING, State.IDLE])
        events = self.drain()
        self.assertIn(("user_text", "hola"), events)
        self.assertTrue(any(e[0] == "response" and e[1].startswith("Hola Ricardo") for e in events))
        self.assertTrue(any(e[0] == "progress" for e in events))
        self.assertEqual(self.c.memory.turns[-1]["user"], "hola")
        self.assertGreaterEqual(self.c.player.played, 2)

    def test_claude_error_shows_error_state(self):
        self.c.claude = FakeClaude(error=ClaudeError("sin conexión", "network"))
        self.c.submit_text("hola")
        self.wait_idle()
        self.assertEqual(self.states[-1], State.ERROR)
        self.assertTrue(any(e[0] == "error" and "sin conexión" in e[1] for e in self.drain()))
        # Desde ERROR se puede volver a preguntar.
        self.c.claude = FakeClaude("ok")
        self.c.submit_text("otra vez")
        self.wait_idle()
        self.assertEqual(self.states[-1], State.IDLE)

    def test_error_state_recovers_to_idle(self):
        import app.controller as ctl
        old, ctl.ERROR_RECOVERY_SECONDS = ctl.ERROR_RECOVERY_SECONDS, 0.2
        try:
            self.c.claude = FakeClaude(error=ClaudeError("sin conexión", "network"))
            self.c.submit_text("hola")
            self.wait_idle()
            self.assertEqual(self.c.state.state, State.ERROR)
            t0 = time.monotonic()
            while self.c.state.state == State.ERROR and time.monotonic() - t0 < 3:
                time.sleep(0.05)
            self.assertEqual(self.c.state.state, State.IDLE)
        finally:
            ctl.ERROR_RECOVERY_SECONDS = old

    def test_broken_music_does_not_lock_turns(self):
        class DeadMusic:  # mpv caído: dice que suena, pero pausar y reanudar fallan
            playing = active = True

            def pause(self):
                raise RuntimeError("El reproductor no está en marcha")

            resume = pause

        self.c.music = DeadMusic()
        self.c.claude = FakeClaude("ok")
        self.c.submit_text("hola")
        self.wait_idle()
        self.assertFalse(self.c._turn_lock.locked())
        self.c.submit_text("otra")  # el siguiente turno se acepta
        self.wait_idle()
        self.assertEqual(len(self.c.claude.prompts), 2)

    def test_failed_output_is_not_kept(self):
        from unittest import mock
        self.c.player = None
        with mock.patch("app.controller.AudioPlayer") as AP:
            AP.return_value.open.side_effect = RuntimeError("No se encontró el dispositivo")
            self.assertFalse(self.c._component("salida de audio", self.c._init_output))
        self.assertIsNone(self.c.player)

    def test_whisper_args_include_audio_ctx(self):
        from app.speech_to_text import WhisperTranscriber
        w = WhisperTranscriber("/w", "s", "c", "m.bin", threads=2, audio_ctx=768)
        self.assertEqual(w._common_args()[-4:], ["-t", "2", "-ac", "768"])
        self.assertNotIn("-ac", WhisperTranscriber("/w", "s", "c", "m.bin")._common_args())

    def test_whisper_request_sends_prompt(self):
        import json as _json
        from unittest import mock
        from app.speech_to_text import WhisperTranscriber
        sent = {}

        def fake_urlopen(req, timeout=None):
            sent["body"] = req.data
            resp = mock.MagicMock()
            resp.__enter__.return_value.read.return_value = _json.dumps({"text": "pon la luz cálida"}).encode()
            return resp
        w = WhisperTranscriber("/w", "s", "c", "m.bin", prompt="Órdenes para Vimo: modo fiesta.")
        with mock.patch("urllib.request.urlopen", fake_urlopen):
            self.assertEqual(w._via_server(b"RIFF"), "pon la luz cálida")
        self.assertIn("Órdenes para Vimo: modo fiesta.".encode(), sent["body"])
        self.assertIn(b'name="prompt"', sent["body"])

    def test_aec_without_visible_devices_falls_back(self):
        from unittest import mock
        import app.controller as ctl
        aec = mock.Mock(running=True)
        aec.start.return_value = "altavoz"
        aec.stop.side_effect = lambda: setattr(aec, "running", False)
        with mock.patch.object(ctl, "EchoCanceller", return_value=aec), \
                mock.patch.object(ctl, "find_source", return_value=None), \
                mock.patch.object(ctl, "refresh_devices") as refresh, \
                mock.patch.object(ctl, "_has_device", return_value=False), \
                mock.patch.object(ctl.time, "sleep"):
            self.assertFalse(self.c._component("cancelación de eco", self.c._init_aec))
        self.assertEqual(refresh.call_count, 5)
        aec.stop.assert_called_once()
        self.assertFalse(self.c.aec_active)                 # el resto usa los dispositivos normales
        self.assertIn("sin cancelación de eco", self.c.components["cancelación de eco"][1])

    def test_interrupt_thinking_cancels(self):
        self.c.claude = FakeClaude(delay=5)
        self.c.submit_text("algo largo")
        time.sleep(0.2)
        self.assertEqual(self.c.state.state, State.THINKING)
        self.c.interrupt()
        self.wait_idle()
        self.assertEqual(self.c.state.state, State.IDLE)
        self.assertNotIn(State.SPEAKING, self.states)

    def test_interrupt_speaking_keeps_conversation(self):
        self.c.claude = FakeClaude(". ".join(f"Frase {i}" for i in range(30)) + ".")
        self.c.player.spc = 0.2
        self.c.submit_text("cuéntame algo")
        t0 = time.monotonic()
        while self.c.state.state != State.SPEAKING and time.monotonic() - t0 < 3:
            time.sleep(0.02)
        self.c.interrupt()
        self.wait_idle()
        self.assertEqual(self.c.state.state, State.IDLE)
        self.assertLess(self.c.player.played, 30)
        self.assertEqual(len(self.c.memory.turns), 1)  # el turno se conserva en el historial

    def test_photo_intent_uses_camera_not_claude(self):
        self.c.claude = FakeClaude()
        self.c.camera = FakeCamera()
        self.c.submit_text("Toma una foto")
        self.wait_idle()
        self.assertEqual(self.c.claude.prompts, [])
        self.assertIn(State.CAPTURING, self.states)
        self.assertEqual(self.states[-1], State.IDLE)

    def test_no_camera_is_reported(self):
        self.c.claude = FakeClaude()
        self.c.submit_text("graba un video")
        self.wait_idle()
        self.assertTrue(any(e[0] == "error" and "cámara" in e[1] for e in self.drain()))

    def test_concurrent_turns_rejected(self):
        self.c.claude = FakeClaude(delay=0.5)
        self.c.submit_text("uno")
        self.c.submit_text("dos")
        self.wait_idle()
        self.assertEqual(self.c.claude.prompts, ["uno"])

    def test_wake_ignored_when_busy(self):
        self.c.claude = FakeClaude(delay=0.5)
        self.c.submit_text("uno")
        time.sleep(0.1)
        self.c._on_wake(0.9)  # no debe iniciar otro turno
        self.wait_idle()
        self.assertEqual(len(self.c.claude.prompts), 1)


if __name__ == "__main__":
    unittest.main()
