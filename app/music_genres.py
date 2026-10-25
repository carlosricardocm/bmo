"""Géneros musicales: reconocer «pon rock / techno / house…» y cómo buscarlos en YouTube.

Las búsquedas de cada tipo se eligieron con pruebas reales en YouTube (22-10-2026):
- Electrónica: buscar «techno» o «house» devuelve casi solo sesiones de DJ de 1–4 h; con
  «<género> original mix» / «extended mix» salen pistas sueltas (7–8 de cada 12).
- Géneros latinos: «<género> canciones» da canciones (salsa: 10 de 12).
- Resto: «<género> song» / «<género> canciones».

`match_genre("rock de los 80")` → GenreRequest(genre=rock, modifier="de los 80").
Un texto que no es un género conocido (o que lleva algo más que un modificador, como «rock de
Caifanes») devuelve None y se trata como una canción o un artista.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

ELECTRONIC = ("{g} original mix", "{g} extended mix", "{g} track")
LATIN = ("{g} canciones", "{g} exitos", "{g} video oficial")
GENERIC = ("{g} song", "{g} canciones", "{g} official video")


@dataclass(frozen=True)
class Genre:
    key: str
    label: str                       # cómo lo dice Vimo
    aliases: tuple[str, ...]         # formas normalizadas (sin acentos, minúsculas)
    queries: tuple[str, ...]         # plantillas de búsqueda; {g} = término de búsqueda
    term: str = ""                   # término para YouTube (por defecto la clave)

    def search_terms(self, modifier: str = "") -> list[str]:
        base = f"{self.term or self.key} {modifier}".strip()
        return [q.format(g=base) for q in self.queries]


def _g(key, label=None, aliases=(), queries=GENERIC, term=""):
    return Genre(key, label or key, tuple(dict.fromkeys((key,) + tuple(aliases))), tuple(queries), term)


GENRES: list[Genre] = [
    # Electrónica
    _g("techno", aliases=("tecno",), queries=ELECTRONIC),
    _g("melodic techno", "techno melódico", ("techno melodico",), ELECTRONIC),
    _g("minimal techno", "techno minimal", ("techno minimal", "minimal"), ELECTRONIC),
    _g("hard techno", aliases=("techno duro",), queries=ELECTRONIC),
    _g("house", aliases=("house music", "jaus"), queries=ELECTRONIC, term="house music"),
    _g("deep house", aliases=("dip house",), queries=ELECTRONIC),
    _g("tech house", aliases=("tec house",), queries=ELECTRONIC),
    _g("progressive house", "house progresivo", ("house progresivo",), ELECTRONIC),
    _g("afro house", aliases=(), queries=ELECTRONIC),
    _g("trance", aliases=("tranc",), queries=ELECTRONIC),
    _g("drum and bass", aliases=("drum n bass", "drum y bass", "drumandbass", "dnb"), queries=ELECTRONIC),
    _g("dubstep", aliases=("dub step",), queries=ELECTRONIC),
    _g("edm", "música electrónica de baile", ("e d m", "electro dance"), ELECTRONIC),
    _g("electronica", "música electrónica", ("electronic", "musica electronica", "electro"), ELECTRONIC,
       term="electronic music"),
    _g("synthwave", aliases=("synth wave", "retrowave"), queries=GENERIC),
    _g("lofi", "lo-fi", ("lo fi", "lofi hip hop", "lo fi hip hop"), ("lofi beat", "lofi song", "lofi hip hop track")),
    _g("ambient", "música ambiental", ("ambiental", "musica ambiental"), ("ambient track", "ambient music piece")),
    # Latinos
    _g("reggaeton", "reguetón", ("regueton", "reggeaton", "reguetton", "perreo"), LATIN),
    _g("salsa", aliases=("salsa romantica",), queries=LATIN),
    _g("cumbia", aliases=("cumbias",), queries=LATIN),
    _g("bachata", aliases=("bachatas",), queries=LATIN),
    _g("merengue", aliases=("merengues",), queries=LATIN),
    _g("banda", "banda sinaloense", ("banda sinaloense", "musica de banda"), LATIN),
    _g("corridos", aliases=("corridos tumbados", "corridos belicos"), queries=LATIN),
    _g("regional mexicano", aliases=("musica regional mexicana", "regional"), queries=LATIN),
    _g("norteno", "norteño", ("nortena", "nortenas", "musica nortena", "nortenos"), LATIN, term="norteño"),
    _g("mariachi", aliases=("rancheras", "ranchera", "musica ranchera"), queries=LATIN),
    _g("boleros", aliases=("bolero",), queries=LATIN),
    _g("tango", aliases=("tangos",), queries=LATIN),
    _g("bossa nova", aliases=("bossanova",), queries=LATIN),
    _g("flamenco", aliases=(), queries=LATIN),
    _g("rock en espanol", "rock en español", ("rock latino", "rock nacional"), LATIN, term="rock en español"),
    _g("pop latino", aliases=(), queries=LATIN),
    _g("baladas", aliases=("balada", "baladas romanticas", "baladas en espanol"), queries=LATIN),
    _g("trap latino", aliases=(), queries=LATIN),
    # Resto
    _g("rock", aliases=("rocanrol", "rock and roll", "rock n roll"), queries=GENERIC),
    _g("rock clasico", "rock clásico", ("classic rock",), GENERIC, term="classic rock"),
    _g("rock alternativo", aliases=("alternativo", "alternative rock", "alternative"), queries=GENERIC,
       term="alternative rock"),
    _g("pop", aliases=("musica pop",), queries=GENERIC),
    _g("metal", aliases=("heavy metal", "metal pesado"), queries=GENERIC),
    _g("punk", aliases=("punk rock",), queries=GENERIC),
    _g("grunge", aliases=(), queries=GENERIC),
    _g("indie", aliases=("indie rock", "indie pop"), queries=GENERIC),
    _g("hip hop", aliases=("hiphop", "hip-hop"), queries=GENERIC),
    _g("rap", aliases=(), queries=GENERIC),
    _g("trap", aliases=(), queries=GENERIC),
    _g("reggae", aliases=("regae", "rege"), queries=GENERIC),
    _g("ska", aliases=(), queries=GENERIC),
    # Jazz y clásica: buscar por artistas u obras representativas da muchas más canciones sueltas
    # que buscar el género (prueba real: «jazz song» 2/12 frente a «Louis Armstrong song» 8/10).
    _g("jazz", aliases=("yas", "jaz"), queries=("Louis Armstrong song", "Ella Fitzgerald song", "Frank Sinatra song",
                                                "Nina Simone song", "Miles Davis", "Chet Baker song",
                                                "Dave Brubeck Take Five", "Billie Holiday song")),
    _g("blues", aliases=(), queries=GENERIC),
    _g("soul", aliases=(), queries=GENERIC),
    _g("funk", aliases=(), queries=GENERIC),
    _g("disco", "música disco", ("musica disco", "disco music"), GENERIC, term="disco music"),
    _g("r&b", "R&B", ("rnb", "r and b", "r b", "rhythm and blues"), GENERIC, term="r&b"),
    _g("country", aliases=("musica country",), queries=GENERIC),
    _g("k-pop", "K-pop", ("kpop", "k pop"), GENERIC, term="kpop"),
    _g("gospel", aliases=(), queries=GENERIC),
    _g("clasica", "música clásica", ("musica clasica", "classical", "clasico"),
       ("Chopin nocturne", "Debussy Clair de Lune", "Beethoven Fur Elise", "Mozart Eine kleine Nachtmusik",
        "Bach cello suite prelude", "Satie Gymnopedie", "Vivaldi Four Seasons", "Tchaikovsky waltz",
        "Pachelbel Canon in D", "Beethoven Moonlight Sonata"), term="classical"),
    _g("opera", "ópera", ("arias de opera",), ("Pavarotti Nessun dorma", "Maria Callas aria", "famous opera aria",
                                               "Andrea Bocelli aria", "Carmen Habanera aria")),
    # Estados de ánimo
    _g("relajante", "música relajante", ("para relajarse", "para relajarme", "tranquila", "chill", "chillout"),
       ("chill song", "relaxing song", "chillout track")),
    _g("para bailar", "música para bailar", ("bailable", "de fiesta", "fiesta", "para la fiesta"),
       ("dance hit song", "canciones para bailar", "party song")),
    _g("romantica", "música romántica", ("romantico", "romanticas", "de amor", "canciones de amor"),
       ("romantic song", "cancion romantica", "love song")),
    _g("para estudiar", "música para estudiar", ("para concentrarme", "para trabajar", "para leer"),
       ("study music track", "focus music track", "lofi study beat")),
    _g("para dormir", "música para dormir", ("para descansar",), ("sleep music track", "calm piano piece")),
]

_MODIFIER = re.compile(
    r"^(?:de\s+los\s+(?:(?:19|20)?\d0s?|sesenta|setenta|ochenta|noventa|dos\s+mil)s?|(?:los\s+)?\d{2}s|\d{4}s?|"
    r"(?:de\s+los\s+)?(?:sesentas|setentas|ochentas|noventas|dosmiles)|"
    r"en\s+(?:espanol|ingles)|clasic[oa]s?|viej(?:it)?[oa]s?|de\s+antes|actual(?:es)?|nuev[oa]s?|"
    r"del\s+momento|de\s+moda|romantic[oa]s?|suave|lent[oa]s?|rapid[oa]|movid[oa]s?|"
    r"alternativ[oa]|mexican[oa]|argentin[oa]|latin[oa]|instrumental|acustic[oa]|pesad[oa]|"
    r"melodic[oa]|underground|old\s+school|para\s+(?:bailar|correr|entrenar|el\s+gym|relajarme|relajarse|"
    r"estudiar|trabajar|dormir|la\s+fiesta|la\s+manana|la\s+noche))$")

# Plurales de las décadas («los 80s») a palabras que YouTube entiende igual.
_DECADE_WORDS = {"sesenta": "60s", "setenta": "70s", "ochenta": "80s", "noventa": "90s",
                 "sesentas": "60s", "setentas": "70s", "ochentas": "80s", "noventas": "90s",
                 "dos mil": "2000s", "dosmiles": "2000s"}

_ALIASES = sorted(((alias, g) for g in GENRES for alias in g.aliases), key=lambda x: -len(x[0]))


@dataclass
class GenreRequest:
    genre: Genre
    modifier: str = ""
    queries: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.genre.label} {self.modifier}".strip()


def _youtube_modifier(mod: str) -> str:
    for word, repl in _DECADE_WORDS.items():
        mod = re.sub(rf"\b{word}\b", repl, mod)
    mod = re.sub(r"\b((?:19|20)?\d0)\b", r"\1s", mod)  # «80» → «80s», «2000» → «2000s»
    return re.sub(r"^(?:de\s+)?los\s+", "", mod).strip()


_DECADE_ONLY = re.compile(r"^(?:(?:exitos|canciones|musica)\s+)?(?:de\s+)?(?:los\s+)?"
                          r"((?:19|20)?\d0s?|sesentas?|setentas?|ochentas?|noventas?|dos\s+mil|dosmiles)$")


def match_genre(query: str) -> GenreRequest | None:
    """`query` ya normalizada (minúsculas, sin acentos), p. ej. la que entrega intents.parse()."""
    q = re.sub(r"\s+", " ", query.strip())
    m = _DECADE_ONLY.match(q)
    if m:  # «música de los 90»: éxitos de esa década
        decade = _youtube_modifier(m.group(1))
        genre = Genre(f"exitos {decade}", f"éxitos de los {decade}", (q,), GENERIC, term=f"{decade} hits")
        return GenreRequest(genre, "", genre.search_terms())
    for alias, genre in _ALIASES:
        if q == alias or q.startswith(alias + " "):
            rest = q[len(alias):].strip()
            if rest and not _MODIFIER.match(rest):
                continue
            yt_mod = _youtube_modifier(rest) if rest else ""
            return GenreRequest(genre, rest, genre.search_terms(yt_mod))
    return None
