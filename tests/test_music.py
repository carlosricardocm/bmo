"""Reproductor de música con YouTube y mpv simulados, órdenes de voz y su integración en el controlador."""
import os
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import intents  # noqa: E402
from app.music_player import MusicError, MusicPlayer, Track, title_key  # noqa: E402


def wait_for(cond, timeout=5.0):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return True
        time.sleep(0.02)
    return False


class FakeSource:
    def __init__(self, cache_dir, mixes=None, fail_ids=(), delay=0.0):
        self.cache_dir = cache_dir
        self.timeout = 5
        self.mixes = mixes or {}
        self.fail_ids = set(fail_ids)
        self.delay = delay
        self.downloads = []
        self.cancelled = 0

    def search(self, query, n=5):
        return [Track("aaaaaaaaaa1", f"{query} (Official Video)", "Canal", 200),
                Track("aaaaaaaaaa2", f"{query} directo", "Canal", 3600)]

    def similar(self, track, n=25):
        return list(self.mixes.get(track.id, []))

    def download(self, track):
        time.sleep(self.delay)
        if track.id in self.fail_ids:
            raise MusicError("descarga fallida")
        os.makedirs(self.cache_dir, exist_ok=True)
        path = os.path.join(self.cache_dir, track.id + ".webm")
        with open(path, "wb") as fh:
            fh.write(b"x" * 20_000)
        self.downloads.append(track.id)
        return path

    def cancel_all(self):
        self.cancelled += 1


class FakeBackend:
    def __init__(self):
        self.on_end = None
        self.running = False
        self.loaded = []
        self.paused = False
        self.volume = None

    def start(self, volume):
        self.running, self.volume = True, volume

    def load(self, path):
        self.loaded.append(os.path.basename(path))
        self.paused = False

    def set_pause(self, p):
        self.paused = p

    def set_volume(self, v):
        self.volume = v

    def stop_playback(self):
        pass

    def stop(self):
        self.running = False

    def finish(self):  # simula el final natural de la canción
        self.on_end("eof")


MIX = [Track("aaaaaaaaaa1", "Seed (Official Video)", "Canal", 200),       # la propia canción: se omite
       Track("bbbbbbbbbb1", "Segunda", "B", 180),
       Track("bbbbbbbbbb2", "Segunda (Lyrics)", "C", 181),              # misma canción, otro vídeo
       Track("bbbbbbbbbb3", "Recopilación 3 horas", "D", 10800),        # demasiado larga
       Track("bbbbbbbbbb4", "Tercera", "E", 30),                        # demasiado corta
       Track("bbbbbbbbbb5", "Cuarta", "F", 240),
       Track("bbbbbbbbbb6", "Quinta", "G", 210)]


class MusicPlayerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = os.path.join(self.tmp.name, "cache")
        self.src = FakeSource(self.cache, {"aaaaaaaaaa1": MIX})
        self.be = FakeBackend()
        self.p = MusicPlayer(self.src, self.be, volume=60, step=10)
        self.errors = []
        self.p.on_error = self.errors.append

    def tearDown(self):
        self.p.shutdown()
        self.tmp.cleanup()

    def start_seed(self):
        track = self.p.find("Seed")
        self.assertEqual(track.id, "aaaaaaaaaa1")  # descarta el directo de 1 h
        self.p.play(track)
        self.assertTrue(wait_for(lambda: "bbbbbbbbbb1" in self.src.downloads), "no precargó la siguiente")

    def test_queue_filters_and_dedupes(self):
        self.start_seed()
        self.assertEqual(self.p.current.id, "aaaaaaaaaa1")
        self.assertEqual([t.id for t in self.p.queue], ["bbbbbbbbbb1", "bbbbbbbbbb5", "bbbbbbbbbb6"])

    def test_continues_with_similar_after_eof(self):
        self.start_seed()
        self.be.finish()
        self.assertTrue(wait_for(lambda: self.p.current and self.p.current.id == "bbbbbbbbbb1"))
        self.assertEqual(self.be.loaded[:2], ["aaaaaaaaaa1.webm", "bbbbbbbbbb1.webm"])
        # El archivo de la canción terminada se borra de la caché.
        self.assertTrue(wait_for(lambda: not os.path.exists(os.path.join(self.cache, "aaaaaaaaaa1.webm"))))

    def test_keeps_playing_by_refilling_from_last_song(self):
        self.src.mixes["bbbbbbbbbb6"] = [Track("cccccccccc1", "Nueva tanda", "H", 200)]
        self.start_seed()
        for expected in ("bbbbbbbbbb1", "bbbbbbbbbb5", "bbbbbbbbbb6", "cccccccccc1"):
            self.be.finish()
            self.assertTrue(wait_for(lambda: self.p.current and self.p.current.id == expected), expected)

    def test_next_uses_prefetched_file(self):
        self.start_seed()
        n = len(self.src.downloads)
        self.p.next()
        self.assertTrue(wait_for(lambda: self.p.current.id == "bbbbbbbbbb1"))
        self.assertEqual(self.src.downloads.count("bbbbbbbbbb1"), 1)
        self.assertGreaterEqual(len(self.src.downloads), n)

    def test_failed_download_skips_to_following(self):
        self.src.fail_ids = {"bbbbbbbbbb1"}
        track = self.p.find("Seed")
        self.p.play(track)
        time.sleep(0.2)
        self.be.finish()
        self.assertTrue(wait_for(lambda: self.p.current and self.p.current.id == "bbbbbbbbbb5"))

    def test_gives_up_after_repeated_failures(self):
        self.src.fail_ids = {"bbbbbbbbbb1", "bbbbbbbbbb5", "bbbbbbbbbb6"}
        self.p.play(self.p.find("Seed"))
        time.sleep(0.2)
        self.be.finish()
        self.assertTrue(wait_for(lambda: self.p.current is None))
        self.assertTrue(self.errors)

    def test_max_volume(self):
        p = MusicPlayer(self.src, self.be, volume=100, step=10, max_volume=70)
        self.assertEqual(p.volume, 70)
        self.assertEqual(p.volume_up(), 70)
        self.assertEqual(p.volume_down(), 60)

    def test_volume_pause_stop(self):
        self.start_seed()
        self.assertEqual(self.p.volume_up(), 70)
        self.assertEqual(self.be.volume, 70)
        self.assertEqual(self.p.set_volume(250), 100)
        self.assertEqual(self.p.set_volume(-5), 0)
        self.p.pause()
        self.assertTrue(self.be.paused and self.p.paused and not self.p.playing and self.p.active)
        self.p.resume()
        self.assertFalse(self.be.paused)
        self.p.stop()
        self.assertFalse(self.p.active)
        self.assertEqual(self.p.queue, [])
        self.assertEqual(os.listdir(self.cache), [])
        self.assertGreaterEqual(self.src.cancelled, 1)

    def test_eof_after_stop_is_ignored(self):
        self.start_seed()
        self.p.stop()
        self.be.finish()
        time.sleep(0.2)
        self.assertIsNone(self.p.current)

    def test_title_key(self):
        self.assertEqual(title_key("Zoé - Soñé (Official Video)"), title_key("Zoe - Sone [HD]"))


class MusicIntentTest(unittest.TestCase):
    def test_play(self):
        cases = {
            "Pon De música ligera de Soda Stereo": "de musica ligera de soda stereo",
            "Vimo, ponme la canción Labios rotos de Zoé por favor": "labios rotos de zoe",
            "reproduce algo de Caifanes": "caifanes",
            "¿Puedes poner música de Café Tacvba?": "cafe tacvba",
            "quiero escuchar a los Beatles": "los beatles",
        }
        for text, query in cases.items():
            self.assertEqual(intents.parse(text), intents.Intent(intents.MUSIC_PLAY, query), text)

    def test_controls(self):
        cases = {
            "Sube el volumen": intents.VOLUME_UP, "bájale un poco": intents.VOLUME_DOWN,
            "más alto": intents.VOLUME_UP, "Siguiente canción": intents.MUSIC_NEXT,
            "salta esta canción": intents.MUSIC_NEXT, "pon otra": intents.MUSIC_NEXT,
            "Pausa": intents.MUSIC_PAUSE, "pon pausa": intents.MUSIC_PAUSE,
            "continúa": intents.MUSIC_RESUME, "Para la música": intents.MUSIC_STOP,
            "deja de poner música": intents.MUSIC_STOP, "para": intents.STOP, "basta": intents.STOP,
            "¿Qué canción es esta?": intents.MUSIC_NOW, "para el video": intents.RECORD_STOP,
        }
        for text, name in cases.items():
            self.assertEqual(intents.detect(text), name, text)
        self.assertEqual(intents.parse("Pon el volumen al 40%"), intents.Intent(intents.VOLUME_SET, "40"))

    def test_volume_phrasings(self):
        # Las dos primeras son transcripciones reales del 25-10-2026 que antes iban a Claude.
        cases = {
            "Supervolumen al 90%": ("volume_set", "90"), "sube a 90": ("volume_set", "90"),
            "baja a 30": ("volume_set", "30"), "volumen 50": ("volume_set", "50"),
            "sube el volumen al máximo": ("volume_set", "100"), "baja el volumen al mínimo": ("volume_set", "10"),
            "Sube un poquito el volumen": ("volume_up", None), "baja un poco el volumen": ("volume_down", None),
            "Supervolumen": ("volume_up", None), "sube más el volumen": ("volume_up", None),
        }
        for text, (name, arg) in cases.items():
            self.assertEqual(intents.parse(text), intents.Intent(name, arg), text)
        self.assertEqual(intents.parse("pon 50 cent"), intents.Intent(intents.MUSIC_PLAY, "50 cent"))
        for text in ("¿Qué es el volumen de un cubo?", "baja la persiana", "sube la foto a internet"):
            self.assertIsNone(intents.parse(text), text)

    def test_misheard_reproduce_and_wake_residue(self):
        # Transcripciones reales del 24/25-10-2026.
        self.assertEqual(intents.parse("le produce el territorio de Deblaze"),
                         intents.Intent(intents.MUSIC_PLAY, "territorio de deblaze"))
        self.assertEqual(intents.parse("Me produce José José"), intents.Intent(intents.MUSIC_PLAY, "jose jose"))
        self.assertIsNone(intents.parse("¿Qué produce la fotosíntesis?"))
        self.assertEqual(intents.strip_wake_phrase("¡Hey Bibo!"), "")
        self.assertEqual(intents.strip_wake_phrase("Hey Vimo, pon Soda Stereo"), "pon Soda Stereo")
        self.assertEqual(intents.strip_wake_phrase("Vivo en Madrid"), "Vivo en Madrid")
        self.assertEqual(intents.strip_wake_phrase("¿Qué es un vimo?"), "¿Qué es un vimo?")

    def test_not_music(self):
        for text in ("¿Qué música te gusta?", "¿Cómo subo el volumen del celular?", "pon atención",
                     "Ponme un temporizador de 5 minutos", "¿Para qué sirve la música?", "Háblame de Soda Stereo"):
            self.assertIsNone(intents.parse(text), text)

    def test_fuzzy_controls(self):
        # Transcripciones reales o probables de Whisper con música de fondo.
        cases = {"Paralabúsica": intents.MUSIC_STOP, "Hey Vimo, paralamúsica": intents.MUSIC_STOP,
                 "pala la música": intents.MUSIC_STOP, "Subelevolumen": intents.VOLUME_UP,
                 "Bajael volumen": intents.VOLUME_DOWN, "pausalamúsica": intents.MUSIC_PAUSE}
        for text, name in cases.items():
            self.assertEqual(intents.detect(text), name, text)
        for text in ("parlamento europeo", "Paralelogramo", "para la mesa", "¿Cuál es la causa?",
                     "Pásame la sal", "la capital de Francia"):
            self.assertIsNone(intents.parse(text), text)


class ControllerMusicTest(unittest.TestCase):
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
        self.src = FakeSource(os.path.join(self.tmp.name, "cache"), {"aaaaaaaaaa1": MIX})
        self.be = FakeBackend()
        self.c.music = MusicPlayer(self.src, self.be, volume=50)
        self.c.music.on_change = self.c._on_music_change

    def tearDown(self):
        self.c.music.shutdown()
        self.tmp.cleanup()

    def turn(self, text):
        self.c.submit_text(text)
        self.assertTrue(wait_for(lambda: not self.c._turn_lock.locked()), "el turno no terminó")

    def test_play_then_question_pauses_and_resumes(self):
        self.turn("pon Seed")
        self.assertTrue(wait_for(lambda: self.c.music.playing))
        self.assertEqual(self.c.claude.prompts, [])          # la música no pasa por Claude
        paused_during = []
        orig = self.c._ask_claude

        def spy(text, timings):
            paused_during.append(self.c.music.paused)
            return orig(text, timings)
        self.c._ask_claude = spy
        self.turn("¿Qué hora es?")
        self.assertEqual(paused_during, [True])
        self.assertTrue(self.c.music.playing)                # reanudada al terminar

    def test_volume_targets_music_when_playing(self):
        self.turn("pon Seed")
        self.assertTrue(wait_for(lambda: self.c.music.playing))
        self.turn("sube el volumen")
        self.assertEqual(self.c.music.volume, 60)
        self.assertEqual(self.be.volume, 60)
        self.assertTrue(self.c.music.playing)

    def test_volume_at_cap_is_explained(self):
        self.c.music.max_volume = 70
        self.c.music.set_volume(70)
        self.turn("pon Seed")
        self.assertTrue(wait_for(lambda: self.c.music.playing))
        said = []
        orig = self.c._speak
        self.c._speak = lambda text, timings=None: (said.append(text), orig(text, timings or {}))
        self.turn("sube el volumen")
        self.turn("Supervolumen al 90%")
        self.assertEqual(self.c.music.volume, 70)
        self.assertEqual(len(said), 2)
        self.assertTrue(all("máximo" in t for t in said), said)
        self.turn("baja el volumen")
        self.assertEqual(self.c.music.volume, 60)
        self.assertIn("60", said[-1])

    def test_stop_and_pause_are_not_undone(self):
        self.turn("pon Seed")
        self.assertTrue(wait_for(lambda: self.c.music.playing))
        self.turn("pausa")
        self.assertTrue(self.c.music.paused)                 # no se reanuda al acabar el turno
        self.turn("continúa")
        self.assertTrue(self.c.music.playing)
        self.turn("para")
        self.assertFalse(self.c.music.active)

    def test_noise_tracker_paused_while_music(self):
        self.turn("pon Seed")
        self.assertTrue(wait_for(lambda: self.c.music.playing))
        self.assertTrue(self.c.noise.paused)
        self.c.music.stop()
        self.assertFalse(self.c.noise.paused)


if __name__ == "__main__":
    unittest.main()


# ---------------------------------------------------------------------------------------
# Géneros
# ---------------------------------------------------------------------------------------
from app.music_genres import match_genre  # noqa: E402
from app.music_player import is_compilation, same_song  # noqa: E402


class GenreMatchTest(unittest.TestCase):
    def genre_of(self, text):
        intent = intents.parse(text)
        self.assertIsNotNone(intent, text)
        self.assertEqual(intent.name, intents.MUSIC_PLAY, text)
        return match_genre(intent.arg)

    def test_genres(self):
        cases = {
            "reproduce rock": "rock", "reproduce techno": "techno", "reproduce house": "house",
            "pon música electrónica": "electronica", "pon algo de reggaetón": "reggaeton",
            "pon un poco de deep house": "deep house", "pon rock en español": "rock en espanol",
            "pon música clásica": "clasica", "pon jazz": "jazz", "pon hip hop": "hip hop",
            "pon techno melódico": "melodic techno", "pon música para bailar": "para bailar",
        }
        for text, key in cases.items():
            req = self.genre_of(text)
            self.assertIsNotNone(req, text)
            self.assertEqual(req.genre.key, key, text)

    def test_modifiers_and_decades(self):
        req = self.genre_of("ponme rock de los 80")
        self.assertEqual((req.genre.key, req.modifier), ("rock", "de los 80"))
        self.assertIn("rock 80s song", req.queries)
        self.assertIn("90s hits song", self.genre_of("pon música de los 90").queries)
        self.assertIn("2000s hits song", self.genre_of("pon éxitos de los 2000").queries)

    def test_electronic_queries_avoid_dj_sets(self):
        self.assertTrue(all("mix" in q or "track" in q for q in self.genre_of("reproduce techno").queries))

    def test_songs_and_artists_are_not_genres(self):
        for text in ("pon rock de Caifanes", "pon House of the Rising Sun", "pon De música ligera",
                     "pon Soda Stereo", "pon Labios rotos de Zoé"):
            self.assertIsNone(self.genre_of(text), text)

    def test_compilations_and_explainers(self):
        for title in ("TECHNO MIX 2026 | INSOMNIA", "Melodic Techno DJ Mix", "Best Of HARD TECHNO 2025 | FULL SET",
                      "Classic Rock Songs Playlist", "What is a Jazz standard?", "Lo mejor de la salsa",
                      "3 Hours of Deep House", "Reaccionando a Soda Stereo"):
            self.assertTrue(is_compilation(title), title)
        for title in ("Argy & Omnya - Aria (Extended Mix)", "Rafa Barrios - Distraido (Original Mix)",
                      "Calma (Remix - Official Video)", "Soda Stereo - De Música Ligera"):
            self.assertFalse(is_compilation(title), title)

    def test_same_song(self):
        self.assertTrue(same_song("Take Five", "The Dave Brubeck Quartet - Take Five"))
        self.assertTrue(same_song("Johann Pachelbel - Canon in D Major", "Canon in D (Pachelbel's Canon)"))
        self.assertTrue(same_song("Debussy: Clair de lune", "C.Debussy - Clair de Lune, piano"))
        self.assertFalse(same_song("Zoé - Soñé (MTV Unplugged)", "Zoé - Labios Rotos (MTV Unplugged)"))
        self.assertFalse(same_song("Anima - Moon (Original Mix)", "Volaris - Set You Free (Extended Mix)"))


def _t(n, title, ch, dur=300):
    return Track(f"g{n:010d}", title, ch, dur)


class GenreSource(FakeSource):
    """Cada búsqueda devuelve canciones distintas según la consulta; la mezcla, otras similares."""

    def __init__(self, cache_dir):
        super().__init__(cache_dir)
        self.queries = []
        self.counter = 0

    def search(self, query, n=5):
        self.queries.append(query)
        out = []
        for i in range(6):
            self.counter += 1
            out.append(_t(self.counter, f"Artista{self.counter} - Tema {self.counter} (Original Mix)", f"Canal{self.counter % 9}"))
        out.append(_t(9999, "TECHNO MIX 2026 | 3 horas", "DJ", 10800))
        return out

    def similar(self, track, n=25):
        self.counter += 1
        return [_t(5000 + self.counter, f"Mezcla - Similar {self.counter}", "Mezcla")]


class GenrePlayerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.src = GenreSource(os.path.join(self.tmp.name, "cache"))
        self.be = FakeBackend()
        self.p = MusicPlayer(self.src, self.be)

    def tearDown(self):
        self.p.shutdown()
        self.tmp.cleanup()

    def test_genre_mode_queue_and_refill(self):
        req = match_genre("techno")
        tracks = self.p.find_genre(req)
        self.assertTrue(tracks)
        self.assertFalse(any(is_compilation(t.title) for t in tracks))
        self.assertTrue(all(any(q in sq for sq in req.queries) for q in self.src.queries))
        self.p.play(tracks[0], genre=req, seeds=tracks[1:])
        self.assertEqual(self.p.status()["genre"], "techno")
        self.assertEqual(len(self.p.queue), len(tracks) - 1)
        # Al agotarse la cola, se rellena con más búsquedas del género y con la mezcla.
        with self.p._lock:
            self.p.queue = self.p.queue[:1]
        self.be.finish()
        self.assertTrue(wait_for(lambda: len(self.p.queue) > 3))
        titles = [t.title for t in self.p.queue]
        self.assertTrue(any(t.startswith("Mezcla") for t in titles))
        self.assertTrue(any("Original Mix" in t for t in titles))
        self.assertEqual(len(set(t.id for t in self.p.queue)), len(self.p.queue))

    def test_stop_leaves_genre_mode(self):
        req = match_genre("house")
        tracks = self.p.find_genre(req)
        self.p.play(tracks[0], genre=req, seeds=tracks[1:])
        self.p.stop()
        self.assertIsNone(self.p.status()["genre"])

    def test_song_mode_has_no_genre(self):
        self.src.mixes = {}
        track = self.p.find("algo")
        self.p.play(track)
        self.assertIsNone(self.p.status()["genre"])


class ControllerGenreTest(ControllerMusicTest):
    def test_genre_command_uses_genre_mode(self):
        self.c.music = MusicPlayer(GenreSource(os.path.join(self.tmp.name, "c2")), self.be)
        self.c.music.on_change = self.c._on_music_change
        self.turn("reproduce techno")
        self.assertTrue(wait_for(lambda: self.c.music.playing))
        self.assertEqual(self.c.music.status()["genre"], "techno")
        self.assertEqual(self.c.claude.prompts, [])
