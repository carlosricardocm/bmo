# Reproductor de música (YouTube)

Vimo puede poner una canción de YouTube y seguir con **canciones similares** hasta que le digas que
pare, o poner un **género musical** («reproduce rock», «reproduce techno», «reproduce house»). Lo gestiona Vimo directamente (`app/music_player.py`), sin pasar por Claude Code.

> Fuente elegida por el usuario: YouTube por streaming. Las condiciones de uso de YouTube no
> permiten acceder a su contenido fuera de su reproductor. Este uso es personal y es decisión del
> usuario. Cuando YouTube cambia algo, yt-dlp puede dejar de funcionar hasta que se actualiza
> (`venv/bin/pip install -U "yt-dlp[default]"`).

## Órdenes de voz

| Di… | Qué hace |
|---|---|
| «pon *De música ligera* de Soda Stereo», «ponme algo de Caifanes», «reproduce *Eres*», «quiero escuchar a Zoé», «¿puedes poner música de Café Tacvba?» | Busca en YouTube, anuncia la canción y la reproduce; después sigue con similares |
| «reproduce rock», «reproduce techno», «pon house», «pon música electrónica», «ponme rock de los 80», «pon música de los 90», «pon algo para bailar» | Reproduce ese **género** (ver la lista abajo) y sigue con más canciones del mismo género |
| «siguiente canción», «salta esta canción», «otra canción», «pon otra» | Pasa a la siguiente similar (o del mismo género) |
| «sube el volumen», «más alto», «súbele» / «baja el volumen», «bájale un poco» | ±10 % (`music.volume_step`) |
| «pon el volumen al 40» | Volumen exacto (0–100) |
| «pausa», «pon pausa» / «continúa», «sigue con la música» | Pausa / reanuda |
| «para la música», «deja de poner música», o solo «para» / «basta» | Detiene la música y vacía la cola |
| «¿qué canción es esta?», «¿quién canta esto?» | Dice el título y el canal |

Todas se pueden decir tras «Hey Vimo» con la música sonando: la wake word sigue activa. Las
preguntas sobre música («¿qué música te gusta?», «háblame de Soda Stereo») las responde Claude.

**Volumen**: con música en curso, «volumen» se refiere a la música (la voz de Vimo no cambia). Sin
música, cambia el volumen general del altavoz (PipeWire, `wpctl`) y Vimo lo confirma en voz alta.

## Géneros

Vimo distingue un género de una canción o un artista: «pon rock» es un género; «pon rock de
Caifanes», «pon House of the Rising Sun» o «pon Soda Stereo» son búsquedas de canción o artista.
Un género puede llevar un modificador: década («de los 80», «de los 2000»), «en español», «clásico»,
«viejito», «romántico», «suave», «para bailar», «instrumental»… («pon cumbias viejitas»,
«pon salsa romántica»). «Pon música de los 90» sin género pone éxitos de esa década.

| Tipo | Géneros reconocidos |
|---|---|
| Electrónica | techno, techno melódico, minimal, hard techno, house, deep house, tech house, house progresivo, afro house, trance, drum and bass, dubstep, EDM, electrónica, synthwave, lo-fi, ambient |
| Latinos | reguetón, salsa, cumbia, bachata, merengue, banda, corridos, regional mexicano, norteño, mariachi / rancheras, boleros, tango, bossa nova, flamenco, rock en español, pop latino, baladas, trap latino |
| Otros | rock, rock clásico, rock alternativo, pop, metal, punk, grunge, indie, hip hop, rap, trap, reggae, ska, jazz, blues, soul, funk, disco, R&B, country, K-pop, gospel, música clásica, ópera |
| Estados de ánimo | relajante, para bailar / fiesta, romántica, para estudiar / concentrarse, para dormir |

La lista y las búsquedas están en `app/music_genres.py`; añadir un género es añadir una línea.

Cómo se eligen las canciones de un género, según pruebas reales en YouTube:

- **Electrónica**: buscar «techno» o «house» devuelve casi solo sesiones de DJ de 1–4 horas. Vimo
  busca «techno original mix», «extended mix»…, que dan pistas sueltas de 4–9 minutos.
- **Latinos**: «<género> canciones», «éxitos», «video oficial».
- **Jazz y música clásica**: buscar por artistas u obras representativas (Louis Armstrong, Ella
  Fitzgerald, Chet Baker… / Chopin, Debussy, Bach…) en vez de por el nombre del género. Con el
  nombre del género salían 2–4 canciones; así salen 15–20.
- Se lanzan hasta 3 búsquedas en paralelo, se descartan **recopilaciones y sesiones** («Mix 2026»,
  «Full Set», «Playlist», «3 horas»…) y **vídeos sobre música** («What is…», tutoriales, reacciones,
  entrevistas). Se conservan «(Original Mix)», «(Extended Mix)» y los remixes, que son canciones.
- Se evita la misma canción en otro vídeo («Take Five» ≈ «Dave Brubeck Quartet - Take Five»), con
  un máximo de 2 canciones por canal y un orden inicial aleatorio para que cada «pon techno» sea
  distinto.
- Al agotarse la cola se **alternan** canciones nuevas del género (otras variantes de búsqueda) con
  la mezcla de YouTube de la última canción. Así sigue en el género sin repetir siempre lo mismo.

Medido (22-10-2026): techno 23 canciones iniciales, house 21, rock 25, reguetón 24, jazz 15–20,
clásica 7–22, cada búsqueda en 1,7–2,6 s. «Reproduce techno» suena en 10,8 s; cambiar a
«reproduce house» con música sonando tarda 11,7 s.

## Caras mientras suena música

Con música sonando y Vimo en reposo, la cara cambia a una de estas animaciones:

| Animación | Carpeta | Qué hace |
|---|---|---|
| Bailando | `faces/music_dance` | ojos felices «^ ^» y sonrisa; la cara se balancea y rebota |
| Cantando | `faces/music_sing` | ojos felices y las bocas de «speaking» alternadas; notas que salen de la boca |
| Disfrutando | `faces/music_vibe` | ojos que miran a los lados, cabeza inclinada, notas flotando |
| Emocionado | `faces/music_wow` | ojos brillantes de «capturing» y boca abierta, dando saltitos |

- Cambia de animación con cada canción nueva y cada `faces.music_switch_seconds` (20 s), sin repetir
  la anterior.
- Al hablar con Vimo (escuchando, pensando, hablando) vuelven sus caras normales, porque la música
  está en pausa. Con la música en pausa o parada, cara de reposo.
- Las caras se generan con `tools/generar_caras_musica.py` recombinando las **piezas reales de las
  caras originales** (ojos de *listening* y *capturing*, bocas de *listening* y *speaking*) y notas
  musicales con el mismo trazo negro y el verde de la nariz de *capturing*. Las imágenes originales
  no se modifican. Para regenerarlas o retocarlas, edita el script y vuelve a ejecutarlo.
- Velocidad de cada animación: `faces.frame_ms` (`music_dance` 190 ms, `music_sing` 230,
  `music_vibe` 420, `music_wow` 220). Lista de animaciones: `faces.music_animations`.

## En la pantalla

Mientras hay música aparece una barra sobre los botones con el género (si lo hay), el título, el
volumen y los botones
**Pausa/Reanudar**, **Siguiente**, **Vol −**, **Vol +** y **Parar**. Desaparece al detener la música.

## Cómo funciona

1. **Búsqueda** (`yt-dlp ytsearch5:`, ~1,5 s): se elige el primer resultado de entre 1 y 15 minutos.
   Así se descartan recopilaciones de horas y fragmentos.
2. Vimo dice «Vale, pongo …» mientras **descarga el audio** (Opus, ~3–4 MB) a `data/music_cache/`.
   YouTube responde HTTP 403 si mpv abre la URL del stream directamente; descargando con yt-dlp
   funciona (comprobado).
3. **mpv** (`--idle --no-video`, controlado por su socket IPC) reproduce el archivo por PipeWire.
4. **Canciones similares**: la «mezcla» de YouTube de esa canción (`watch?v=ID&list=RDID`, 25
   canciones). Se descartan las ya sonadas, la misma canción en otro vídeo (oficial, letra, en
   vivo…) y las demasiado cortas o largas.
5. Mientras suena una canción se **precarga la siguiente**: al acabar o al pedir «siguiente», el
   cambio es inmediato.
6. Cuando quedan pocas en la cola, se rellena con la mezcla de la **última** canción. La música
   continúa indefinidamente hasta que digas «para».
7. Cada archivo se borra al terminar de sonar. Al parar, al cerrar Vimo y al arrancar se vacía la
   caché.

Si una descarga falla se salta a la siguiente. Tras 3 fallos seguidos la música se detiene con un
aviso.

### Convivencia con la conversación

- Al hablar con Vimo (wake word, **Hablar** o texto) la música se **pausa** y se reanuda al
  terminar la respuesta (`music.pause_while_talking`). No se reanuda si la orden fue «pausa» o «para».
- Con música sonando, Vimo no actualiza su estimación del ruido ambiente; si lo hiciera, el umbral
  de voz subiría.
- Para oír «Hey Vimo» con música, Vimo usa **cancelación de eco** (resta del micrófono lo que suena
  por el altavoz). Sin ella, a volumen alto la wake word no se detectaba nunca. Ver [eco.md](eco.md).

## Configuración (`config.json` → `music`)

| Clave | Defecto | |
|---|---|---|
| `enabled` | `true` | |
| `volume` | 60 | volumen inicial de mpv (0–100); se guarda al cambiarlo |
| `volume_step` | 10 | paso de «sube/baja el volumen» y de los botones |
| `min_duration_seconds` / `max_duration_seconds` | 60 / 900 | filtro de duración |
| `mix_size` | 25 | canciones que se piden a cada mezcla |
| `ytdlp` | `venv/bin/yt-dlp` | yt-dlp actualizado del venv (el de APT es de 2025 y falla) |
| `js_runtime` | `node` | motor JavaScript para los retos de YouTube (Node.js 22 instalado) |
| `cache_dir` | `data/music_cache` | caché temporal |
| `mpv_args` | `["--ao=pipewire"]` | salida de audio de mpv |
| `pause_while_talking` | `true` | |
| `download_timeout_seconds` | 90 | |

## Rendimiento medido (Raspberry Pi 5, 21–22/10/2026)

| Paso | Tiempo |
|---|---|
| Búsqueda | 1,4–1,5 s |
| Desde «pon …» hasta que suena | 10,5–14 s (búsqueda + descarga; Vimo anuncia la canción mientras tanto) |
| «Siguiente» con la canción precargada | 0,2 s |
| Volumen, pausa, parar | < 0,1 s |

## Prueba rápida

```bash
venv/bin/python tools/probar.py musica "Soda Stereo De música ligera"
```

## Problemas

| Síntoma | Solución |
|---|---|
| «yt-dlp: … Sign in to confirm you're not a bot» u otros errores de YouTube | `venv/bin/pip install -U "yt-dlp[default]"`. Si persiste, YouTube está limitando esta IP: espera. |
| «No se encuentra yt-dlp» | `./setup.sh` (instala la versión actual en el venv) |
| «mpv no está instalado» | `sudo apt install mpv` |
| No suena pero dice que sí | Prueba `mpv --ao=pipewire <archivo>`; revisa la salida predeterminada de PipeWire (`wpctl status`) |
| Tarda en empezar | Es la descarga de la primera canción; las siguientes se precargan. |
