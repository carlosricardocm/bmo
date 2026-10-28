"""Órdenes locales explícitas que Vimo ejecuta sin pasar por Claude Code.

La cámara, la música y el reinicio de la conversación solo se activan con órdenes imperativas
claras al inicio de la frase («toma una foto», «pon De música ligera», «sube el volumen»). Las
preguntas sobre esas acciones («¿cómo se graba un vídeo?», «¿qué música te gusta?») no coinciden y
se envían a Claude Code como cualquier otra consulta.

`parse()` devuelve un Intent con el nombre de la orden y, si procede, su argumento (la canción a
buscar o el volumen).
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher

PHOTO = "photo"
RECORD_START = "record_start"
RECORD_STOP = "record_stop"
RESET = "reset"
MUSIC_PLAY = "music_play"
MUSIC_STOP = "music_stop"
MUSIC_PAUSE = "music_pause"
MUSIC_RESUME = "music_resume"
MUSIC_NEXT = "music_next"
MUSIC_NOW = "music_now"
VOLUME_UP = "volume_up"
VOLUME_DOWN = "volume_down"
VOLUME_SET = "volume_set"
STOP = "stop"  # «para», «basta»: detiene la música si suena


@dataclass
class Intent:
    name: str
    arg: str | None = None


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.lower())
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = re.sub(r"[^\w\s]", " ", text)
    return re.sub(r"\s+", " ", text).strip()


# Porcentajes dichos con palabras («al cincuenta por ciento», «al noventa»): Whisper no siempre
# escribe cifras. Solo se convierten tras «al / a / en / hasta», para no tocar títulos de canciones.
_UNITS = {"uno": 1, "un": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5, "seis": 6, "siete": 7,
          "ocho": 8, "nueve": 9}
_TEENS = {"diez": 10, "once": 11, "doce": 12, "trece": 13, "catorce": 14, "quince": 15, "dieciseis": 16,
          "diecisiete": 17, "dieciocho": 18, "diecinueve": 19, "veinte": 20, "cien": 100, "ciento": 100}
_TENS = {"veinti": 20, "treinta": 30, "cuarenta": 40, "cincuenta": 50, "sesenta": 60, "setenta": 70,
         "ochenta": 80, "noventa": 90}
_UNIT_RX = "|".join(_UNITS)
_NUMWORD = re.compile(r"\b(al|a|en|hasta)\s+(?:veinti(" + _UNIT_RX + r")|(" + "|".join(t for t in _TENS if t != "veinti") +
                      r")(?:\s+y\s+(" + _UNIT_RX + r"))?|(" + "|".join(_TEENS) + r"))\b")


def _num(m: re.Match) -> str:
    if m.group(2):
        value = 20 + _UNITS[m.group(2)]
    elif m.group(3):
        value = _TENS[m.group(3)] + (_UNITS[m.group(4)] if m.group(4) else 0)
    else:
        value = _TEENS[m.group(5)]
    return f"{m.group(1)} {value}"


def words_to_digits(norm: str) -> str:
    """«pon la luz al cincuenta por ciento» → «pon la luz al 50 por ciento» (texto ya normalizado)."""
    return _NUMWORD.sub(_num, norm)


_PREFIX = (r"^(?:(?:oye|hey|ey|vimo|bimo|por favor|porfa|ahora|ya|bueno|vale|ok|okay)\s+)*"
           r"(?:(?:puedes|podrias|quiero que|necesito que|me puedes|me podrias)\s+)?(?:me\s+)?")
_END = r"(?:\s+(?:por favor|porfa|vimo|bimo|gracias))*$"
_MUSIC = r"(?:la\s+)?(?:musica|cancion|canciones|rola|tema|reproduccion)"
# «volume», «volúmer»: evaluación con tu voz (28-10-2026).
_VOL = r"(?:volumen|bolumen|volume|volumer|sonido)"
_VERB_SET = r"pon|ponle|poner|sube|subele|super|supe|baja|bajale|ajusta|deja"
_EXTREME = r"(?:el\s+)?(?:maximo|tope|minimo)"
_PERCENT = r"(?:\s+(?:por\s+ciento|porciento))?"
_AMOUNT = r"(?:(?:un\s+poco|un\s+poquito|mas)\s+)?"
_AMOUNT_END = r"(?:\s+(?:un\s+poco|un\s+poquito|mas))?"

_PATTERNS: list[tuple[str, str]] = [
    (RECORD_STOP, _PREFIX + r"(?:deten|detener|detenga|para|parar|pare|termina|terminar|finaliza|finalizar|"
                            r"corta|cortar|deja de|dejar de|apaga|apagar)\s+(?:la\s+|el\s+)?"
                            r"(?:grabacion|grabar|grabando|video|filmacion|filmar)\b"),
    (RECORD_START, _PREFIX + r"(?:graba|grabar|grabame|filma|filmar|filmame)\b(?:\s+(?:un|una)\s+(?:video|clip))?"),
    (RECORD_START, _PREFIX + r"(?:empieza|empezar|inicia|iniciar|comienza|comenzar|arranca|arrancar)\s+"
                             r"(?:a\s+grabar|a\s+filmar|(?:la|una)\s+grabacion|(?:un|el)\s+video)\b"),
    (PHOTO, _PREFIX + r"(?:toma|tomar|tomame|saca|sacar|sacame|haz|hacer|hazme|captura|capturar)\s+"
                      r"(?:me\s+)?(?:una\s+|otra\s+)?(?:foto|fotografia|fotito|imagen|captura)\b"),
    # Whisper oye «Tomó / como una foto» y «Grabó un vídeo» (evaluación con tu voz, 28-10-2026). Son
    # también frases corrientes, así que solo cuentan si no hay nada más.
    (PHOTO, _PREFIX + r"(?:tomo|tome|como)\s+(?:una\s+|otra\s+)?(?:foto|fotografia)" + _END),
    (RECORD_START, _PREFIX + r"(?:grabo|grabe)\s+(?:un|una)\s+(?:video|clip)" + _END),
    (RESET, _PREFIX + r"(?:(?:empecemos|empezar|comencemos|iniciar|inicia|empieza)\s+(?:una\s+)?"
                      r"(?:nueva\s+conversacion|de\s+nuevo|de\s+cero)"
                      r"|nueva\s+conversacion"
                      r"|(?:olvida|olvidar|borra|borrar|reinicia|reiniciar)\s+"
                      r"(?:todo|la\s+conversacion|lo\s+que\s+hablamos|el\s+historial|tu\s+memoria))\b"),
    # --- música ------------------------------------------------------------------------
    (MUSIC_STOP, _PREFIX + r"(?:(?:para|parar|pare|deten|detener|quita|quitar|apaga|apagar|corta|cortar|"
                           r"termina|terminar)\s+(?:ya\s+)?" + _MUSIC +
                           r"|(?:deja|dejar)\s+de\s+(?:poner|reproducir|tocar|sonar)(?:\s+" + _MUSIC + r")?"
                           r"|ya\s+no\s+quiero\s+(?:escuchar\s+|oir\s+)?(?:mas\s+)?" + _MUSIC + r")" + _END),
    (MUSIC_PAUSE, _PREFIX + r"(?:pausa|pausar|pausala|pon\s+(?:en\s+)?pausa|pon\s+la\s+musica\s+en\s+pausa|"
                            r"(?:pausa|pausar)\s+" + _MUSIC + r")" + _END),
    (MUSIC_RESUME, _PREFIX + r"(?:continua|continuar|reanuda|reanudar|quita\s+la\s+pausa|"
                             r"(?:continua|reanuda|sigue|seguir)\s+(?:con\s+)?" + _MUSIC +
                             r"|sigue\s+reproduciendo|sigue\s+sonando|vuelve\s+a\s+poner\s+la\s+musica)" + _END),
    (MUSIC_NEXT, _PREFIX + r"(?:(?:la\s+)?siguiente(?:\s+cancion|\s+tema|\s+rola)?"
                           r"|(?:salta|saltate|saltala|pasa|pasate|cambia|cambiale)(?:\s+(?:de|a\s+la\s+siguiente|esta|"
                           r"este|la))?(?:\s+(?:cancion|tema|rola))?"
                           r"|(?:pon\s+)?otra\s+(?:cancion|rola|tema)|pon\s+otra)" + _END),
    (MUSIC_NOW, _PREFIX + r"(?:que\s+(?:cancion|tema|rola|musica)\s+(?:es\s+esta|es\s+esto|es|esta\s+sonando|suena|"
                          r"estas\s+poniendo|estamos\s+escuchando)|como\s+se\s+llama\s+esta\s+(?:cancion|rola)|"
                          r"quien\s+canta\s+(?:esta\s+cancion|esto|esta)|que\s+esta\s+sonando)" + _END),
    # Volumen. Whisper a veces junta «sube el volumen» en «supervolumen» / «subevolumen» y escribe
    # «bolumen»: por eso `\s*` entre el verbo y _VOL. Los porcentajes llegan como «90» (sin «%»).
    (VOLUME_SET, _PREFIX + r"(?:(?:" + _VERB_SET + r")\s*)?(?:el\s*)?" + _VOL + r"\s*(?:(?:al|a|en|hasta)\s+)?"
                           r"(?P<arg>\d{1,3}|" + _EXTREME + r")" + _PERCENT + _END),
    (VOLUME_SET, _PREFIX + r"(?:" + _VERB_SET + r")(?:le)?\s+(?:(?:al|a|en|hasta)\s+(?:el\s+)?)"
                           r"(?P<arg>\d{1,3}|" + _EXTREME + r")" + _PERCENT + _END),
    (VOLUME_UP, _PREFIX + r"(?:(?:sube|subele|subir|aumenta|aumentale|super|supe|mas)\s*" + _AMOUNT +
                          r"(?:el\s*|al\s*|la\s*)?(?:" + _VOL + r"|musica)" + _AMOUNT_END +
                          r"|(?:subele|mas\s+alto|mas\s+fuerte)" + _AMOUNT_END + r"|sube(?:le)?\s+un\s+po(?:co|quito))" + _END),
    (VOLUME_DOWN, _PREFIX + r"(?:(?:baja|bajale|bajar|disminuye|reduce|menos|baca|vaca)\s*" + _AMOUNT +
                            r"(?:el\s*|al\s*|la\s*)?(?:" + _VOL + r"|musica)" + _AMOUNT_END +
                            r"|(?:bajale|mas\s+bajo|mas\s+despacio|mas\s+quedito)" + _AMOUNT_END +
                            r"|baja(?:le)?\s+un\s+po(?:co|quito))" + _END),
    (STOP, r"^(?:(?:vimo|bimo|oye|ya)\s+)*(?:para|parale|detente|basta|alto|stop|silencio|callate|ya\s+basta|"
           r"ya\s+para)" + _END),
    # «pon / reproduce / toca X»: debe ir al final porque es la orden más amplia.
    # «produce» / «le produce»: Whisper a veces oye así «reproduce» (log del 25-10-2026).
    (MUSIC_PLAY, _PREFIX + r"(?:pon|ponme|ponnos|poner|ponerme|reproduce|reproduceme|reproducir|(?:le\s+)?produce|"
                           r"toca|tocame|tocar|"
                           r"quiero\s+(?:escuchar|oir)|escuchemos|pongamos|"
                           # «por / o la música de Soda Stereo» (evaluación con tu voz, 28-10-2026)
                           r"(?:por|o)(?=\s+(?:la\s+)?musica\s+de\s))\s+(?P<arg>.+)$"),
    (MUSIC_PLAY, _PREFIX + r"(?:pon|ponme|reproduce|toca)(?P<arg>)" + _END),
]
_COMPILED = [(name, re.compile(p)) for name, p in _PATTERNS]

# Qué buscar en YouTube a partir de «pon …»: se quitan muletillas («la canción», «algo de»…).
_QUERY_FILLER = re.compile(
    r"^(?:(?:la|una|el|un|unas|unos|algo\s+de|algo|musica\s+de|musica|canciones\s+de|cancion\s+de|"
    r"cancion|tema|rola|video\s+de|el\s+video\s+de|poco\s+de|poquito\s+de|a)\s+)+")
_QUERY_TAIL = re.compile(r"\s+(?:por\s+favor|porfa|vimo|bimo|en\s+youtube|gracias)$")
# Cosas que no son música aunque empiecen por «pon»: se dejan a Claude o a otras órdenes.
_NOT_MUSIC = re.compile(r"^(?:atencion|cuidado|un\s+recordatorio|una\s+alarma|un\s+temporizador|"
                        r"la\s+(?:hora|fecha)|el\s+volumen|pausa|en\s+pausa)\b")


# Whisper a veces junta o confunde palabras de órdenes cortas dichas con música de fondo
# («Paralabúsica»). Si ninguna expresión coincide, se compara la frase sin espacios con estas
# órdenes; solo para frases cortas y con mucho parecido, para no capturar consultas normales.
_FUZZY: list[tuple[str, str]] = [
    (MUSIC_STOP, "paralamusica"), (MUSIC_STOP, "detenlamusica"), (MUSIC_STOP, "apagalamusica"),
    (MUSIC_STOP, "quitalamusica"), (MUSIC_STOP, "pararlamusica"),
    (MUSIC_PAUSE, "pausalamusica"), (MUSIC_PAUSE, "ponpausa"),
    (MUSIC_RESUME, "continualamusica"), (MUSIC_RESUME, "reanudalamusica"),
    (MUSIC_NEXT, "siguientecancion"), (MUSIC_NEXT, "otracancion"),
    (VOLUME_UP, "subeelvolumen"), (VOLUME_UP, "subelevolumen"),
    (VOLUME_DOWN, "bajaelvolumen"), (VOLUME_DOWN, "bajalevolumen"),
]
_FUZZY_PREFIX = re.compile(r"^(?:(?:oye|hey|ey|vimo|bimo|por favor|porfa|ya|ok)\s+)*")
FUZZY_MIN_RATIO = 0.82


def fuzzy_command(norm: str) -> str | None:
    compact = _FUZZY_PREFIX.sub("", norm).replace(" ", "")
    for tail in ("porfavor", "porfa", "vimo", "bimo", "gracias"):
        if compact.endswith(tail) and len(compact) > len(tail) + 6:
            compact = compact[: -len(tail)]
    if not 8 <= len(compact) <= 22:
        return None
    best, best_ratio = None, FUZZY_MIN_RATIO
    for name, phrase in _FUZZY:
        ratio = SequenceMatcher(None, compact, phrase).ratio()
        if ratio >= best_ratio:
            best, best_ratio = name, ratio
    return best


# Restos de la palabra de activación al principio de la transcripción («¡Hey Bibo! pon…»): Whisper
# recibe a veces el final de «Hey Vimo». «vivo» solo cuenta con «hey» delante («vivo en Madrid»).
_WAKE_PHRASE = re.compile(
    r"^[\s¡!¿?.,]*(?:(?:hey|ey|ei|oye)[\s,]+(?:vimo|bimo|bibo|vivo|beemo|vemo|bmo)|vimo|bimo|bibo)\b[\s¡!¿?.,:]*",
    re.IGNORECASE)


def strip_wake_phrase(text: str) -> str:
    """Quita «Hey Vimo» (y variantes) del principio; devuelve "" si no queda nada más."""
    rest = _WAKE_PHRASE.sub("", text, count=1)
    return rest.strip() if rest != text else text.strip()


def clean_query(raw: str) -> str:
    q = raw.strip()
    q = _QUERY_TAIL.sub("", q)
    stripped = _QUERY_FILLER.sub("", q)
    return (stripped or q).strip()


def parse(text: str) -> Intent | None:
    norm = words_to_digits(normalize(text))
    if not norm:
        return None
    for name, rx in _COMPILED:
        m = rx.match(norm)
        if not m:
            continue
        arg = m.groupdict().get("arg")
        if name == VOLUME_SET and arg and not arg.isdigit():
            arg = "10" if arg.endswith("minimo") else "100"  # el tope real lo pone music.max_volume
        if name == MUSIC_PLAY:
            if arg and _NOT_MUSIC.match(arg):
                continue
            # YouTube no distingue acentos ni mayúsculas: basta la consulta normalizada.
            arg = clean_query(arg) if arg else ""
        return Intent(name, arg)
    name = fuzzy_command(norm)
    return Intent(name) if name else None


def detect(text: str) -> str | None:
    """Compatibilidad: solo el nombre de la orden."""
    intent = parse(text)
    return intent.name if intent else None
