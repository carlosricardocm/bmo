"""Pruebas de configuración, estados, memoria, órdenes locales y adaptación de texto para voz."""
import json
import os
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import intents  # noqa: E402
from app.config_manager import DEFAULTS, ConfigManager  # noqa: E402
from app.conversation_memory import ConversationMemory  # noqa: E402
from app.state_manager import InvalidTransition, State, StateManager  # noqa: E402
from app.text_to_speech import prepare_for_speech, split_sentences  # noqa: E402


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "config.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_creates_defaults(self):
        cfg = ConfigManager(self.path)
        self.assertTrue(os.path.exists(self.path))
        self.assertEqual(cfg.get("wake_word.chunk_size"), 1280)
        self.assertEqual(cfg.get("wake_word.engine"), "livekit")

    def test_merges_and_validates(self):
        with open(self.path, "w") as fh:
            json.dump({"wake_word": {"threshold": 5, "chunk_size": "x"}, "stt": {"language": "en"},
                       "video": {"profile": "nope"}, "nuevo": 1}, fh)
        cfg = ConfigManager(self.path)
        self.assertEqual(cfg.get("wake_word.threshold"), DEFAULTS["wake_word"]["threshold"])
        self.assertEqual(cfg.get("wake_word.chunk_size"), 1280)
        self.assertEqual(cfg.get("stt.language"), "en")
        self.assertEqual(cfg.get("video.profile"), "720p30")  # primer perfil válido
        self.assertEqual(cfg.get("wake_word.cooldown_seconds"), 2.0)  # completado con defaults
        self.assertTrue(any("nuevo" in w for w in cfg.warnings))

    def test_corrupt_file_is_backed_up(self):
        with open(self.path, "w") as fh:
            fh.write("{ esto no es json")
        cfg = ConfigManager(self.path)
        backups = [f for f in os.listdir(self.tmp.name) if ".corrupt-" in f]
        self.assertEqual(len(backups), 1)
        self.assertEqual(cfg.get("agent.name"), "Vimo")

    def test_dangerous_claude_args_removed(self):
        with open(self.path, "w") as fh:
            json.dump({"claude": {"extra_args": ["--dangerously-skip-permissions", "--permission-mode=bypassPermissions",
                                                 "--model", "sonnet"]}}, fh)
        cfg = ConfigManager(self.path)
        self.assertEqual(cfg.get("claude.extra_args"), ["--model", "sonnet"])

    def test_claude_args_whitelist(self):
        with open(self.path, "w") as fh:
            json.dump({"claude": {"extra_args": ["--allowedTools", "Bash", "--tools", "default", "--add-dir", "/",
                                                 "--effort=high", "--settings", "x.json", "--model", "opus"]}}, fh)
        cfg = ConfigManager(self.path)
        self.assertEqual(cfg.get("claude.extra_args"), ["--effort=high", "--model", "opus"])

    def test_audio_ctx_must_cover_listening(self):
        with open(self.path, "w") as fh:
            json.dump({"stt": {"audio_ctx": 512}, "listening": {"max_seconds": 12.0}}, fh)
        self.assertEqual(ConfigManager(self.path).get("stt.audio_ctx"), 0)   # 512 ≈ 10 s no basta
        with open(self.path, "w") as fh:
            json.dump({"stt": {"audio_ctx": 768}, "listening": {"max_seconds": 12.0}}, fh)
        self.assertEqual(ConfigManager(self.path).get("stt.audio_ctx"), 768)

    def test_memory_enabled_later_keeps_history(self):
        from app.conversation_memory import ConversationMemory
        mpath = os.path.join(os.path.dirname(self.path), "mem.json")
        m = ConversationMemory(mpath, True)
        m.add_turn("hola", "qué tal")
        later = ConversationMemory(mpath, False)       # arrancó con la memoria desactivada
        later.add_turn("otra", "respuesta")
        later.set_enabled(True)
        later.save()
        users = [t["user"] for t in ConversationMemory(mpath, True).turns]
        self.assertEqual(users, ["hola", "otra"])

    def test_update_persists_atomically(self):
        cfg = ConfigManager(self.path)
        cfg.update({"wake_word.threshold": 0.7})
        self.assertEqual(ConfigManager(self.path).get("wake_word.threshold"), 0.7)
        self.assertFalse([f for f in os.listdir(self.tmp.name) if f.startswith(".config-")])


class StateTest(unittest.TestCase):
    def test_normal_cycle(self):
        sm = StateManager()
        seen = []
        sm.add_listener(lambda old, new, reason: seen.append(new))
        for st in (State.IDLE, State.LISTENING, State.THINKING, State.SPEAKING, State.IDLE):
            sm.set(st)
        self.assertEqual(seen, [State.IDLE, State.LISTENING, State.THINKING, State.SPEAKING, State.IDLE])

    def test_invalid_transition(self):
        sm = StateManager()
        with self.assertRaises(InvalidTransition):
            sm.set(State.SPEAKING)
        self.assertFalse(sm.set(State.SPEAKING, strict=False))
        self.assertEqual(sm.state, State.WARMUP)

    def test_transition_if_is_atomic(self):
        sm = StateManager(State.IDLE)
        wins = []
        barrier = threading.Barrier(8)

        def worker():
            barrier.wait()
            if sm.transition_if({State.IDLE}, State.LISTENING):
                wins.append(1)
        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(wins), 1)

    def test_listener_errors_do_not_break(self):
        sm = StateManager()
        sm.add_listener(lambda *a: 1 / 0)
        self.assertTrue(sm.set(State.IDLE))


class MemoryTest(unittest.TestCase):
    def test_limit_reset_and_session(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "m.json")
            m = ConversationMemory(path, True, max_turns=3)
            for i in range(5):
                m.add_turn(f"u{i}", f"a{i}")
            m.set_session("abc", 5)
            m2 = ConversationMemory(path, True, max_turns=3)
            self.assertEqual([t["user"] for t in m2.turns], ["u2", "u3", "u4"])
            self.assertEqual(m2.resumable_session(), ("abc", 5))
            m2.reset()
            self.assertEqual(ConversationMemory(path, True).turns, [])

    def test_disabled_does_not_write(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "m.json")
            m = ConversationMemory(path, False)
            m.add_turn("hola", "adiós")
            self.assertFalse(os.path.exists(path))
            self.assertEqual(m.resumable_session(), (None, 0))

    def test_secrets_not_stored(self):
        with tempfile.TemporaryDirectory() as d:
            m = ConversationMemory(os.path.join(d, "m.json"), True)
            m.add_turn("mi api_key es sk-ant-123456789012345", "vale")
            self.assertNotIn("sk-ant", json.dumps(m.turns))

    def test_corrupt_memory(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "m.json")
            with open(path, "w") as fh:
                fh.write("[[[")
            self.assertEqual(ConversationMemory(path, True).turns, [])


class IntentTest(unittest.TestCase):
    def test_commands(self):
        cases = {
            "Toma una foto": intents.PHOTO,
            "Vimo, sácame una fotografía por favor": intents.PHOTO,
            "¿Puedes tomar una foto?": intents.PHOTO,
            "Graba un vídeo": intents.RECORD_START,
            "empieza a grabar": intents.RECORD_START,
            "Detén la grabación": intents.RECORD_STOP,
            "deja de grabar": intents.RECORD_STOP,
            "Para el video": intents.RECORD_STOP,
            "Nueva conversación": intents.RESET,
            "olvida todo": intents.RESET,
        }
        for text, expected in cases.items():
            self.assertEqual(intents.detect(text), expected, text)

    def test_questions_are_not_commands(self):
        for text in ("¿Cómo se graba un vídeo en la Raspberry?", "¿Qué foto es mejor?",
                     "Háblame de la grabación de discos", "¿Por qué no puedo tomar fotos?", ""):
            self.assertIsNone(intents.detect(text), text)


class SpeechTextTest(unittest.TestCase):
    def test_markdown_removed(self):
        md = ("## Resumen\n\n**Hola** Ricardo. Mira [la web](https://www.raspberrypi.com/news/x?y=1).\n"
              "- punto uno\n- punto dos\n\n```python\nprint('hola')\n```\nRuta: /home/bmo/proyectos/vimo/app/gui.py 😀")
        out = prepare_for_speech(md)
        for bad in ("#", "**", "```", "print(", "https://", "/home/bmo", "😀", "- punto"):
            self.assertNotIn(bad, out)
        self.assertIn("la web", out)
        self.assertIn("gui.py", out)
        self.assertIn("código en la pantalla", out)

    def test_bare_url_becomes_domain(self):
        self.assertIn("ejemplo.com", prepare_for_speech("Ver https://www.ejemplo.com/a/b/c?d=1"))

    def test_table(self):
        out = prepare_for_speech("| a | b |\n|---|---|\n| 1 | 2 |")
        self.assertNotIn("|", out)
        self.assertNotIn("---", out)

    def test_truncation(self):
        out = prepare_for_speech("Frase corta. " * 200, max_chars=200)
        self.assertLess(len(out), 260)
        self.assertTrue(out.endswith("pantalla."))

    def test_split_keeps_order(self):
        text = "Uno. Dos? Tres! " + "palabra " * 80
        segs = split_sentences(text, max_len=100)
        self.assertEqual(segs[:3], ["Uno.", "Dos?", "Tres!"])
        self.assertTrue(all(len(s) <= 100 for s in segs))
        self.assertEqual(" ".join(segs).split(), text.split())


if __name__ == "__main__":
    unittest.main()
