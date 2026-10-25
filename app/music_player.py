"""Reproductor de música de Vimo: canciones de YouTube y continuación con canciones similares.

Piezas (separadas para poder probarlas con simulaciones):

YouTubeSource
    yt-dlp (el del venv, actualizado) como subproceso: búsqueda (`ytsearchN:`), la «mezcla» de
    YouTube de una canción (`watch?v=ID&list=RDID`), que da las canciones similares, y descarga
    del audio a una caché temporal. Se descarga en lugar de reproducir la URL directa porque
    YouTube responde HTTP 403 cuando mpv/ffmpeg abre el stream (comprobado en esta Raspberry Pi).

MpvBackend
    Un proceso `mpv --idle --no-video` controlado por su socket IPC (JSON): cargar archivo,
    pausa, volumen y aviso de fin de canción.

MusicPlayer
    Cola de reproducción: al pedir una canción la busca, la reproduce y rellena la cola con su
    mezcla (sin repetir y descartando vídeos demasiado cortos o largos). Mientras suena una canción
    se descarga la siguiente. Cuando la cola se agota, se rellena con la mezcla de la última
    canción, así que la música continúa hasta que el usuario la detiene. Los archivos de la caché
    se borran al terminar de sonar y al parar.
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import shutil
import signal
import socket
import subprocess
import threading
import time
import unicodedata
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:  # pragma: no cover
    from .music_genres import GenreRequest

log = logging.getLogger(__name__)


class MusicError(RuntimeError):
    pass


@dataclass
class Track:
    id: str
    title: str
    channel: str = ""
    duration: float | None = None

    @property
    def url(self) -> str:
        return f"https://www.youtube.com/watch?v={self.id}"

    def label(self) -> str:
        return f"{self.title} — {self.channel}" if self.channel else self.title


_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_NOISE = re.compile(r"\((?:[^)]*(?:official|oficial|video|audio|lyric|letra|visualizer|hd|4k|remaster)[^)]*)\)"
                    r"|\[(?:[^\]]*)\]", re.I)


# Recopilaciones, sesiones de DJ y similares: no son «una canción». Se conservan «(Original Mix)»,
# «(Extended Mix)» y los remixes, que en la música electrónica son pistas normales.
_COMPILATION = re.compile(
    r"\b(?:dj\s*mix|mix\s*(?:19|20)\d\d|(?:19|20)\d\d\s*mix|mixed\s+by|full\s*(?:set|album|concert|concierto)|"
    r"(?:dj|live|full)\s+set|playlist|compilation|compilacion|recopilacion|mega\s*mix|non\s*stop|"
    r"enganchados?|lo\s+mejor\s+de|grandes\s+exitos|best\s+of|top\s+\d+|\d+\s*(?:hours?|horas|hrs?)\b|"
    r"1\s*hora|live\s+@|radio\s+show|podcast|tutorial|how\s+to|karaoke|para\s+dormir\s+\d|"
    # vídeos que hablan de música en vez de serlo
    r"what\s+is|que\s+es\s+(?:el|la|un|una)\b|explained|explicad[oa]|lesson|leccion|clase\s+de|review|"
    r"reaction|reaccion|reaccionando|analisis|documental|documentary|interview|entrevista)")


def is_compilation(title: str) -> bool:
    t = unicodedata.normalize("NFKD", (title or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    return bool(_COMPILATION.search(t))


def title_key(title: str) -> str:
    """Clave para detectar la misma canción con distinto vídeo (oficial, letra, en vivo…)."""
    t = _NOISE.sub(" ", title or "")
    t = unicodedata.normalize("NFKD", t.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"\b(official|oficial|video|audio|lyrics?|letra|hd|4k|remaster(ed)?|mv)\b", " ", t)
    return re.sub(r"[^a-z0-9]+", " ", t).strip()


def song_name(title: str) -> str:
    """Nombre de la canción sin artista ni añadidos: «Beethoven - Für Elise | Piano» → «fur elise»."""
    t = re.split(r"\s+[-–—]\s+|:\s+", title or "", maxsplit=1)
    t = t[1] if len(t) == 2 else t[0]
    t = re.split(r"\s*[|(\[]|\s+(?:ft\.?|feat\.?|featuring)\s+", t, maxsplit=1)[0]
    return title_key(t.strip(" '\"«»"))


def _contained(a: str, b: str) -> bool:
    wa, wb = set(a.split()), set(b.split())
    small, big = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    return len(small) >= 2 and small <= big


def same_song(a: str, b: str) -> bool:
    """¿Son la misma canción con distinto vídeo? Compara el título limpio y el nombre de la canción;
    cuenta como igual si las palabras del más corto (al menos dos) están todas en el otro:
    «Take Five» ≈ «The Dave Brubeck Quartet - Take Five», «Canon in D» ≈ «Pachelbel - Canon In D Major»."""
    ka, kb = title_key(a), title_key(b)
    sa, sb = song_name(a), song_name(b)
    if ka == kb or (sa and sa == sb):
        return True
    return _contained(ka, kb) or (bool(sa) and bool(sb) and _contained(sa, sb))


# ---------------------------------------------------------------------------------------
# Fuente: YouTube con yt-dlp
# ---------------------------------------------------------------------------------------
class YouTubeSource:
    def __init__(self, ytdlp: str, cache_dir: str, js_runtime: str | None = "node", timeout: float = 90,
                 cpu_cores: list[int] | None = None, nice: int = 0):
        self.ytdlp = ytdlp
        self.cpu_cores = set(cpu_cores or [])
        self.nice = nice
        self.cache_dir = cache_dir
        self.js_runtime = js_runtime
        self.timeout = timeout
        self._procs: set[subprocess.Popen] = set()
        self._lock = threading.Lock()

    def check(self) -> str:
        if not (os.path.isfile(self.ytdlp) and os.access(self.ytdlp, os.X_OK)) and not shutil.which(self.ytdlp):
            raise MusicError(f"No se encuentra yt-dlp en {self.ytdlp} (venv/bin/pip install -U 'yt-dlp[default]')")
        return self._run(["--version"], timeout=30).strip()

    def _base(self) -> list[str]:
        cmd = [self.ytdlp, "--ignore-config", "--no-warnings"]
        if self.js_runtime:
            cmd += ["--js-runtimes", self.js_runtime]
        return cmd

    def _run(self, args: list[str], timeout: float | None = None) -> str:
        proc = subprocess.Popen(self._base() + args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                stdin=subprocess.DEVNULL, text=True, start_new_session=True,
                                preexec_fn=self._limit_cpu)
        with self._lock:
            self._procs.add(proc)
        try:
            out, err = proc.communicate(timeout=timeout or self.timeout)
        except subprocess.TimeoutExpired:
            self._kill(proc)
            raise MusicError("YouTube tardó demasiado en responder")
        finally:
            with self._lock:
                self._procs.discard(proc)
        if proc.returncode != 0:
            if proc.returncode < 0:
                raise MusicError("cancelado")
            msg = (err or "").strip().splitlines()[-1:] or ["error desconocido"]
            raise MusicError(f"yt-dlp: {msg[0][:200]}")
        return out

    def _limit_cpu(self) -> None:
        """En el proceso hijo: núcleos y prioridad limitados (menos picos de consumo)."""
        try:
            if self.cpu_cores:
                valid = self.cpu_cores & set(range(os.cpu_count() or 1))
                if valid:
                    os.sched_setaffinity(0, valid)
            if self.nice:
                os.nice(self.nice)
        except OSError:
            pass

    @staticmethod
    def _kill(proc: subprocess.Popen) -> None:
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            proc.wait(timeout=3)
        except (ProcessLookupError, subprocess.TimeoutExpired, PermissionError):
            try:
                proc.kill()
            except ProcessLookupError:
                pass

    def cancel_all(self) -> None:
        with self._lock:
            procs = list(self._procs)
        for p in procs:
            self._kill(p)

    @staticmethod
    def _parse(lines: str) -> list[Track]:
        tracks = []
        for line in lines.splitlines():
            try:
                d = json.loads(line)
            except ValueError:
                continue
            vid = d.get("id") or ""
            if not _ID.match(vid) or d.get("live_status") in ("is_live", "is_upcoming"):
                continue
            tracks.append(Track(vid, d.get("title") or vid, d.get("channel") or d.get("uploader") or "",
                                d.get("duration")))
        return tracks

    def search(self, query: str, n: int = 5) -> list[Track]:
        query = re.sub(r"[\x00-\x1f]", " ", query).strip()[:200]
        if not query:
            raise MusicError("No sé qué canción buscar")
        return self._parse(self._run(["-j", "--flat-playlist", f"ytsearch{n}:{query}"]))

    def similar(self, track: Track, n: int = 25) -> list[Track]:
        url = f"https://www.youtube.com/watch?v={track.id}&list=RD{track.id}"
        return self._parse(self._run(["-j", "--flat-playlist", "--playlist-end", str(n), url]))

    def download(self, track: Track) -> str:
        os.makedirs(self.cache_dir, exist_ok=True)
        out = self._run(["-q", "--no-progress", "--no-playlist", "-f", "bestaudio/best",
                         "-o", os.path.join(self.cache_dir, "%(id)s.%(ext)s"),
                         "--print", "after_move:filepath", track.url])
        path = out.strip().splitlines()[-1] if out.strip() else ""
        if not path or not os.path.isfile(path) or os.path.getsize(path) < 10_000:
            raise MusicError(f"La descarga de «{track.title}» falló")
        return path


# ---------------------------------------------------------------------------------------
# Motor: mpv por IPC
# ---------------------------------------------------------------------------------------
class MpvBackend:
    def __init__(self, socket_path: str, extra_args: list[str] | None = None, mpv: str = "mpv"):
        self.socket_path = socket_path
        self.extra_args = extra_args or []
        self.mpv = mpv
        self._proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None
        self._send_lock = threading.Lock()
        self._req = 0
        self._pending: dict[int, dict] = {}
        self._cond = threading.Condition()
        self.on_end: Callable[[str], None] | None = None   # razón: eof | error | stop | quit
        self._reader: threading.Thread | None = None

    def check(self) -> str:
        path = shutil.which(self.mpv)
        if not path:
            raise MusicError("mpv no está instalado (sudo apt install mpv)")
        # Solo informativo: en un arranque cargado (Whisper y Piper calentando) `mpv --version` llegó a
        # tardar más de 10 s, y eso dejaba la música desactivada toda la sesión.
        try:
            out = subprocess.run([path, "--version"], stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                 timeout=30).stdout
        except subprocess.TimeoutExpired:
            log.warning("mpv --version no respondió; se usa igualmente %s", path)
            return "mpv"
        return out.splitlines()[0] if out else "mpv"

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, volume: int) -> None:
        if self.running:
            return
        if os.path.exists(self.socket_path):
            os.unlink(self.socket_path)
        cmd = [self.mpv, "--idle=yes", "--no-video", "--no-terminal", "--force-window=no",
               f"--input-ipc-server={self.socket_path}", f"--volume={int(volume)}", "--volume-max=100",
               "--keep-open=no", "--gapless-audio=yes"] + self.extra_args
        self._proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                      stderr=subprocess.DEVNULL, start_new_session=True)
        for _ in range(100):
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(self.socket_path)
                self._sock = s
                break
            except OSError:
                time.sleep(0.05)
        else:
            self.stop()
            raise MusicError("mpv no arrancó")
        self._reader = threading.Thread(target=self._read_loop, name="mpv-ipc", daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        buf = b""
        sock = self._sock
        while sock is not None:
            try:
                data = sock.recv(65536)
            except OSError:
                break
            if not data:
                break
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                if "request_id" in msg:
                    with self._cond:
                        self._pending[msg["request_id"]] = msg
                        self._cond.notify_all()
                elif msg.get("event") == "end-file" and self.on_end:
                    try:
                        self.on_end(msg.get("reason", ""))
                    except Exception:
                        log.exception("Error en fin de canción")

    def command(self, *args, timeout: float = 5.0):
        if not self.running or self._sock is None:
            raise MusicError("El reproductor no está en marcha")
        with self._send_lock:
            self._req += 1
            rid = self._req
            self._sock.sendall((json.dumps({"command": list(args), "request_id": rid}) + "\n").encode())
        deadline = time.monotonic() + timeout
        with self._cond:
            while rid not in self._pending:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise MusicError(f"mpv no respondió a {args[0]}")
                self._cond.wait(left)
            msg = self._pending.pop(rid)
        if msg.get("error") not in ("success", None):
            raise MusicError(f"mpv: {msg['error']}")
        return msg.get("data")

    def load(self, path: str) -> None:
        self.command("loadfile", path, "replace")
        self.command("set_property", "pause", False)

    def set_pause(self, paused: bool) -> None:
        self.command("set_property", "pause", paused)

    def set_volume(self, volume: int) -> None:
        self.command("set_property", "volume", int(volume))

    def stop_playback(self) -> None:
        if self.running:
            self.command("stop")

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        sock, self._sock = self._sock, None
        if sock:
            try:
                sock.sendall(b'{"command": ["quit"]}\n')
            except OSError:
                pass
            sock.close()
        if proc and proc.poll() is None:
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError:
                pass


# ---------------------------------------------------------------------------------------
# Reproductor con cola de canciones similares
# ---------------------------------------------------------------------------------------
class MusicPlayer:
    def __init__(self, source: YouTubeSource, backend: MpvBackend, volume: int = 60, step: int = 10,
                 min_duration: float = 60, max_duration: float = 900, mix_size: int = 25,
                 max_volume: int = 100):
        self.source = source
        self.backend = backend
        # Tope del volumen: muy alta, la música tapa la voz y la cancelación de eco la suprime
        # junto con el eco (ver docs/eco.md).
        self.max_volume = max(1, min(100, int(max_volume)))
        self.volume = max(0, min(self.max_volume, int(volume)))
        self.step = step
        self.min_duration = min_duration
        self.max_duration = max_duration
        self.mix_size = mix_size
        self.current: Track | None = None
        self.queue: list[Track] = []
        self.paused = False
        self._history_ids: set[str] = set()
        self._history_titles: set[str] = set()
        self._files: dict[str, str] = {}          # id -> archivo descargado
        self._prefetching: set[str] = set()
        self._lock = threading.RLock()
        self._generation = 0                      # invalida trabajos de una sesión anterior al parar
        self._failures = 0
        self.genre: GenreRequest | None = None    # modo género: «pon techno»
        self._genre_round = 0
        self.on_change: Callable[[dict], None] | None = None
        self.on_error: Callable[[str], None] | None = None
        backend.on_end = self._on_end

    # Estado ----------------------------------------------------------------------------
    @property
    def active(self) -> bool:
        """Hay música en curso (sonando o en pausa)."""
        return self.current is not None

    @property
    def playing(self) -> bool:
        return self.current is not None and not self.paused

    def status(self) -> dict:
        with self._lock:
            return {"active": self.active, "paused": self.paused, "volume": self.volume,
                    "genre": self.genre.label if self.genre else None,
                    "title": self.current.title if self.current else None,
                    "channel": self.current.channel if self.current else None,
                    "queue": len(self.queue)}

    def _notify(self) -> None:
        if self.on_change:
            try:
                self.on_change(self.status())
            except Exception:
                log.exception("Error notificando el estado de la música")

    # Selección de canciones ------------------------------------------------------------
    def _acceptable(self, t: Track) -> bool:
        if t.id in self._history_ids or title_key(t.title) in self._history_titles:
            return False
        if t.duration is not None and not (self.min_duration <= t.duration <= self.max_duration):
            return False
        return not is_compilation(t.title)

    def _remember(self, t: Track) -> None:
        self._history_ids.add(t.id)
        self._history_titles.add(title_key(t.title))

    def _genre_search(self, queries: list[str], per_query: int = 15) -> list[Track]:
        """Búsquedas del género en paralelo, mezcladas por turnos (una de cada búsqueda)."""
        results: dict[int, list[Track]] = {}

        def run(i: int, q: str) -> None:
            try:
                results[i] = self.source.search(q, per_query)
            except MusicError as exc:
                log.warning("Búsqueda de género «%s» fallida: %s", q, exc)
        threads = [threading.Thread(target=run, args=(i, q), daemon=True) for i, q in enumerate(queries)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(self.source.timeout)
        lists = [results.get(i, []) for i in range(len(queries))]
        merged: list[Track] = []
        for row in range(max((len(x) for x in lists), default=0)):
            for lst in lists:
                if row < len(lst):
                    merged.append(lst[row])
        return merged

    def _diverse(self, candidates: list[Track], per_channel: int = 2,
                 existing: list[Track] | None = None) -> list[Track]:
        """Filtra, quita repeticiones (también la misma canción con otro título) y limita por canal."""
        out: list[Track] = []
        ids: set[str] = set()
        channels: dict[str, int] = {}
        reference = [t.title for t in (existing or [])] + ([self.current.title] if self.current else [])
        for t in candidates:
            ch = (t.channel or "").lower()
            if (not self._acceptable(t) or t.id in ids or channels.get(ch, 0) >= per_channel
                    or any(same_song(t.title, other) for other in reference)):
                continue
            ids.add(t.id)
            channels[ch] = channels.get(ch, 0) + 1
            reference.append(t.title)
            out.append(t)
        return out

    def find_genre(self, request: GenreRequest) -> list[Track]:
        """Canciones para empezar un género: variadas (máx. 2 por canal) y en orden aleatorio."""
        queries = list(request.queries)
        if len(queries) > 3:  # como mucho 3 búsquedas a la vez; variedad entre peticiones
            queries = random.sample(queries, 3)
        tracks = self._diverse(self._genre_search(queries))
        if not tracks:
            raise MusicError(f"No encontré canciones de {request.label} en YouTube")
        head = tracks[:12]
        random.shuffle(head)  # que «pon techno» no empiece siempre por la misma
        return head + tracks[12:]

    def _refill(self, seed: Track) -> None:
        if self.genre is not None:
            self._refill_genre(seed)
            return
        try:
            candidates = self.source.similar(seed, self.mix_size)
        except MusicError as exc:
            log.warning("No se pudo obtener la mezcla de «%s»: %s", seed.title, exc)
            candidates = []
            if seed.channel:
                try:
                    candidates = self.source.search(f"{seed.channel} canciones", 10)
                except MusicError:
                    pass
        with self._lock:
            known = {t.id for t in self.queue} | {title_key(t.title) for t in self.queue}
            for t in candidates:
                if self._acceptable(t) and t.id not in known and title_key(t.title) not in known:
                    self.queue.append(t)
                    known |= {t.id, title_key(t.title)}
        log.info("Cola de música: %d canciones similares a «%s»", len(self.queue), seed.title)

    def _refill_genre(self, seed: Track) -> None:
        """Modo género: alterna canciones nuevas del género con la mezcla de la última canción,
        para seguir dentro del género sin repetir siempre los mismos resultados."""
        genre = self.genre
        self._genre_round += 1
        queries = genre.queries[self._genre_round % len(genre.queries):] + genre.queries[:self._genre_round % len(genre.queries)]
        found = self._genre_search(queries[:2], per_query=20)
        try:
            mix = self.source.similar(seed, 12)
        except MusicError:
            mix = []
        interleaved: list[Track] = []
        for i in range(max(len(found), len(mix))):
            if i < len(found):
                interleaved.append(found[i])
            if i < len(mix):
                interleaved.append(mix[i])
        with self._lock:
            if self.genre is not genre:
                return
            fresh = self._diverse(interleaved, per_channel=3, existing=self.queue)
            head = fresh[:8]
            random.shuffle(head)
            self.queue.extend(head + fresh[8:])
        log.info("Cola de %s: %d canciones", genre.label, len(self.queue))

    # API pública -----------------------------------------------------------------------
    def find(self, query: str) -> Track:
        """Busca la canción (rápido, ~1–2 s) sin empezar a reproducirla."""
        results = self.source.search(query, 5)
        for t in results:
            if t.duration is None or self.min_duration <= t.duration <= self.max_duration:
                return t
        if results:
            return results[0]
        raise MusicError(f"No encontré «{query}» en YouTube")

    def play(self, track: Track, genre: GenreRequest | None = None, seeds: list[Track] | None = None) -> None:
        """Descarga y reproduce `track` y prepara la cola de similares. Bloquea hasta que suena.
        Con `genre`, la cola empieza por `seeds` y se rellena con canciones de ese género."""
        with self._lock:
            self._generation += 1
            gen = self._generation
            self._clear_queue()
            self._history_ids.clear()
            self._history_titles.clear()
            self._failures = 0
            self.genre = genre
            self._genre_round = 0
            self._remember(track)
            if seeds:
                self.queue.extend(t for t in seeds if t.id != track.id)
        path = self.source.download(track)
        with self._lock:
            if gen != self._generation:
                self._discard(path)
                return
            self._files[track.id] = path
            self._start(track)
            # La canción anterior y lo precargado para la cola antigua ya no se usan: antes se
            # quedaban en data/music_cache hasta parar la música (cientos de MB en una tarde).
            for tid in [t for t in self._files if t != track.id]:
                self._discard(self._files.pop(tid))
        threading.Thread(target=self._after_start, args=(gen, track), name="music-fill", daemon=True).start()

    def _start(self, track: Track) -> None:
        if not self.backend.running:
            self.backend.start(self.volume)
        self.current = track
        self.paused = False
        self._remember(track)
        self.backend.load(self._files[track.id])
        log.info("Reproduciendo: %s", track.label())
        self._notify()

    def _after_start(self, gen: int, track: Track) -> None:
        if len(self.queue) < 3:
            self._refill(track)
        self._prefetch(gen)
        self._notify()

    def _prefetch(self, gen: int) -> None:
        with self._lock:
            if gen != self._generation or not self.queue:
                return
            nxt = self.queue[0]
            if nxt.id in self._files or nxt.id in self._prefetching:
                return
            self._prefetching.add(nxt.id)
        try:
            path = self.source.download(nxt)
            with self._lock:
                if gen == self._generation and nxt in self.queue:
                    self._files[nxt.id] = path
                else:
                    self._discard(path)
        except MusicError as exc:
            log.warning("No se pudo precargar «%s»: %s", nxt.title, exc)
            with self._lock:
                if nxt in self.queue:
                    self.queue.remove(nxt)  # se descarta y se probará la siguiente
        finally:
            with self._lock:
                self._prefetching.discard(nxt.id)

    def next(self) -> None:
        """Salta a la siguiente canción similar."""
        threading.Thread(target=self._advance, args=(self._generation,), name="music-next", daemon=True).start()

    def _on_end(self, reason: str) -> None:
        if reason == "eof":
            threading.Thread(target=self._advance, args=(self._generation,), name="music-next", daemon=True).start()
        elif reason == "error":
            log.warning("mpv no pudo reproducir la canción; se pasa a la siguiente")
            threading.Thread(target=self._advance, args=(self._generation,), name="music-next", daemon=True).start()

    def _advance(self, gen: int) -> None:
        with self._lock:
            if gen != self._generation or self.current is None:
                return
            finished = self.current
            self._discard(self._files.pop(finished.id, None))
        while True:
            with self._lock:
                if gen != self._generation:
                    return
                if not self.queue:
                    seed = finished
                else:
                    seed = None
            if seed is not None:
                self._refill(seed)
                with self._lock:
                    if not self.queue:
                        self._fail("No encuentro más canciones similares")
                        return
            with self._lock:
                nxt = self.queue[0]
            ready = self._wait_prefetch(nxt, gen)
            if gen != self._generation:
                return
            if not ready:
                try:
                    ready = self.source.download(nxt)
                except MusicError as exc:
                    log.warning("Descarga fallida de «%s»: %s", nxt.title, exc)
                    with self._lock:
                        if nxt in self.queue:
                            self.queue.remove(nxt)
                        self._failures += 1
                        if self._failures >= 3:
                            self._fail("YouTube no deja descargar las canciones ahora mismo")
                            return
                    continue
            with self._lock:
                if gen != self._generation:
                    self._discard(ready)
                    return
                if nxt in self.queue:
                    self.queue.remove(nxt)
                self._files[nxt.id] = ready
                self._failures = 0
                try:
                    self._start(nxt)
                except MusicError as exc:
                    self._fail(str(exc))
                    return
            break
        if len(self.queue) < 3:
            self._refill(nxt)
        self._prefetch(gen)
        self._notify()

    def _wait_prefetch(self, track: Track, gen: int) -> str | None:
        """Si `track` se está precargando, espera a que termine (evita descargarlo dos veces)."""
        deadline = time.monotonic() + self.source.timeout
        while time.monotonic() < deadline and gen == self._generation:
            with self._lock:
                if track.id in self._files:
                    return self._files[track.id]
                if track.id not in self._prefetching:
                    return None
            time.sleep(0.2)
        return None

    def _fail(self, message: str) -> None:
        log.error("Música detenida: %s", message)
        self.stop()
        if self.on_error:
            self.on_error(message)

    def pause(self) -> None:
        with self._lock:
            if self.current and not self.paused:
                self.backend.set_pause(True)
                self.paused = True
                self._notify()

    def resume(self) -> None:
        with self._lock:
            if self.current and self.paused:
                self.backend.set_pause(False)
                self.paused = False
                self._notify()

    def duck(self, ratio: float | None) -> None:
        """Baja temporalmente el volumen real de mpv (ratio 0–1) sin cambiar `self.volume`;
        `duck(None)` lo restaura."""
        with self._lock:
            if not self.backend.running:
                return
            target = self.volume if ratio is None else int(self.volume * ratio)
            try:
                self.backend.set_volume(target)
            except MusicError:
                pass

    def set_volume(self, volume: int) -> int:
        with self._lock:
            self.volume = max(0, min(self.max_volume, int(volume)))
            log.info("Volumen de la música: %d %%", self.volume)
            if self.backend.running:
                self.backend.set_volume(self.volume)
            self._notify()
            return self.volume

    def volume_up(self) -> int:
        return self.set_volume(self.volume + self.step)

    def volume_down(self) -> int:
        return self.set_volume(self.volume - self.step)

    def stop(self) -> None:
        """Detiene la música, vacía la cola y borra la caché."""
        with self._lock:
            self._generation += 1
            was_active = self.current is not None
            self.current = None
            self.paused = False
            self.genre = None
            self._clear_queue()
        self.source.cancel_all()
        try:
            self.backend.stop_playback()
        except MusicError:
            pass
        self._clear_cache()
        if was_active:
            log.info("Música detenida")
        self._notify()

    def shutdown(self) -> None:
        self.stop()
        self.backend.stop()

    # Caché --------------------------------------------------------------------------------
    def _clear_queue(self) -> None:
        self.queue.clear()

    def _discard(self, path: str | None) -> None:
        if path and os.path.isfile(path):
            try:
                os.unlink(path)
            except OSError:
                pass

    def _clear_cache(self) -> None:
        with self._lock:
            for path in list(self._files.values()):
                self._discard(path)
            self._files.clear()
        directory = self.source.cache_dir
        if os.path.isdir(directory):
            for name in os.listdir(directory):
                self._discard(os.path.join(directory, name))
