# Registro de pruebas y limitaciones conocidas

Fecha: 21-10-2026 · Raspberry Pi 5 · Debian 13 trixie · Python 3.13.5 · Claude Code 2.1.307.

## 1. Pruebas automatizadas (simulaciones, sin hardware)

```bash
venv/bin/python -m unittest discover -s tests -v     # 129 pruebas, OK (~15 s)
```

| Archivo | Qué cubre |
|---|---|
| `tests/test_core.py` | configuración (valores por defecto, validación, archivo dañado, argumentos peligrosos, guardado atómico), máquina de estados (ciclo, transiciones inválidas, atomicidad con 8 hilos), memoria (límite, desactivada, secretos, archivo dañado), órdenes locales (y preguntas que no deben activarlas), adaptación de texto para voz |
| `tests/test_audio_search.py` | remuestreo 48→16 kHz y 22,05→44,1 kHz, bloques, WAV, sonidos originales legibles, VAD (voz + silencio, sin voz, duración máxima, clics), ruido ambiente, filtro de alucinaciones, wake word con modelo simulado (umbral, enfriamiento, suspensión, retardo de reanudación, desactivación), validación del ONNX real, caras (orden natural, escala, alternativas), DuckDuckGo (parser HTML, anuncios, redirecciones, anti-bots, alternativa, caché, pausa persistente, validación) |
| `tests/test_claude_client.py` | con un `claude` falso que emite stream-json: versión y sesión, reutilización de sesión (`--session-id` → `--resume`), texto solo por stdin, `--tools ""`, `dontAsk`, `--allowedTools`, entorno limpio, extracción de fuentes, errores de autenticación y límite de uso, tiempo límite que mata el proceso, cancelación, sesión perdida, límite de cola |
| `tests/test_music.py` | música con YouTube/mpv simulados: filtro de duración y duplicados, continuación con similares al acabar y al agotarse la cola, «siguiente» con precarga, fallos de descarga, volumen/pausa/parar, órdenes de voz (y preguntas que no lo son), pausa durante la conversación y reanudación, volumen de la música frente al del sistema |
| `tests/test_controller.py` | flujo completo con componentes simulados: estados `THINKING → SPEAKING → IDLE` ligados a operaciones reales, error → `ERROR` y recuperación, cancelación durante «pensando», interrupción durante «hablando» conservando la conversación, foto por orden local sin pasar por Claude, cámara ausente, turnos concurrentes rechazados, wake word ignorada si está ocupado |

Estas pruebas **no** validan el comportamiento físico.

## 2. Verificado en la Raspberry Pi real

| # | Criterio | Resultado | Evidencia |
|---|---|---|---|
| 1 | La aplicación inicia | ✓ | GUI en la pantalla DSI 800×480, arranque en 2,0 s (con voz desactivada a propósito). Captura de pantalla durante el desarrollo |
| 2 | Caras y sonidos originales | ✓ caras / ◐ sonidos | `faces/` idéntico a brenpoly/be-more-agent@5fb7558; `sounds/sonidos en Ingles/` idéntico al original. Sonidos cargados y legibles; reproducido `okay.wav` por el altavoz USB (se oyó) |
| 3 | Estados visuales reales | ✓ (software) | transiciones por operación real (pruebas del controlador); WARMUP → IDLE en la GUI real |
| 4 | OpenWakeWord carga el modelo personalizado | ✓ | `hey_vimo.onnx` y `hey_vimo_colab.onnx`, entrada [1,16,96], 0,2 s |
| 5 | Wake word con el micrófono real | ◐ | el detector procesó 160 bloques en vivo del iTalk-02 (7,2 ms/bloque); **no se probó a decir «Hey Vimo»**. Con grabaciones del usuario: 38/80 detecciones, 0/26 falsas (docs/wake_word.md) |
| 7 | Claude Code responde | ✓ | `claude -p` stream-json, ~2–5 s por turno sin búsqueda |
| 8 | Contexto entre turnos | ✓ | «Me llamo Ricardo» → «¿Cómo me llamo?» → «Te llamas Ricardo» |
| 9 | DuckDuckGo devuelve fuentes reales | ✓ | turno real: 2 búsquedas, 5 fuentes reales extraídas; con bloqueo, Claude lo indicó sin inventar |
| 15 | GUI responde durante operaciones lentas | ✓ (diseño + pruebas) | todo el trabajo va fuera del hilo de Tk; eventos por cola |
| 16 | Cierre libera recursos | ✓ | SIGTERM → «Vimo cerrado correctamente», sin procesos hijos |
| 17 | Errores comprensibles | ✓ (simulados) / ◐ real | voz ausente: «Funcionando con limitaciones: voz (Piper)» en la GUI real; errores de Claude simulados |
| — | Pruebas manuales del usuario (niveles 1–5: micrófono, wake word, Claude, sonido, voz, Whisper, foto, vídeo, Vimo completo) | ✓ (informado por el usuario) | 22-10-2026 |
| — | Orientación de la cámara | ✓ | 22-10: la cámara está de lado (no boca abajo como se creyó el 21-10). `camera.rotation` = 270 → vídeo real 1080×1920 y foto 1748×2328, ambos derechos (comprobado viendo los fotogramas); vídeo previo reparado sin recodificar (339 fotogramas, 11,3 s) |
| — | Música de extremo a extremo (GUI + controlador reales) | ✓ | «pon De música ligera…» → suena en 10,5 s; 24 similares; «sube el volumen» 60→70; «¿qué canción es esta?»; pregunta a Claude con la música en pausa y reanudada después; «siguiente canción» en 0,2 s (Zoé); «para la música»; caché vacía y sin procesos huérfanos |
| — | Música por género (GUI + controlador reales) | ✓ | «reproduce techno» → «Rafa Barrios - Distraido (Original Mix)» en 10,8 s, 21 de techno en cola; «siguiente canción» sigue en techno (Argy & Omnya - Aria); «reproduce house» con música sonando cambia de género en 11,7 s; «para» |
| — | Caras de música (GUI real) | ✓ | «reproduce rock» → cara *cantando* (fotogramas distintos en capturas a 250 ms: 7664 píxeles cambian); a los 6 s pasa a *disfrutando*; «¿Qué hora es?» → cara *pensando* con la música en pausa; al terminar vuelve la animación; «para» → cara de reposo |
| — | Ajustes de la wake word | ✓ | evaluación con 394 grabaciones (53 % actual → 64 % con umbral 0,15; el VAD no quita detecciones); herramienta de grabación probada (16 kHz mono); arranque con VAD 0,50 a 7,3 ms/bloque; cerrar durante el arranque a los 2/4/7 s → código 0, sin abort (antes: abort 134) |
| — | Gestos en reposo y pantalla apagada (GUI real) | ✓ | gestos vistos: mirar, sonreír, parpadear, bostezar; reposo → captura negra (brillo medio 0,0) y retroiluminación real 255 → 0; toque → se despierta y vuelve a 255; «Hey Vimo» → se despierta en 0,1 s (antes 2,2 s, esperaba al pitido); al cerrar, brillo 255 y nada pendiente de restaurar |
| — | `setup.sh` idempotente | ✓ | segunda ejecución sin instalar nada; `config.json` intacto (mismo MD5) |
| — | `main.py --check` | ✓ | todos los componentes correctos |

## 3. Pendiente (no ejecutado en hardware)

| # | Criterio | Motivo |
|---|---|---|
| 5 | Decir «Hey Vimo» al micrófono real | requiere a una persona delante |
| 6 | Whisper.cpp transcribe una consulta real | prueba suspendida por los apagados (carga de CPU) |
| 10 | Piper reproduce la respuesta | carga de la voz y reproducción suspendidas por los apagados |
| 11 | Interrumpir la reproducción real | depende de 10 (lógica probada con simulación) |
| 12 | Foto real con la IMX519 | suspendida por los apagados |
| 13–14 | Grabación real y MP4 reproducible | suspendida por los apagados (codificación por software) |

Comandos para completarlas: [whisper.md](whisper.md#rendimiento), [camara_video.md](camara_video.md#estado-de-la-verificación),
y la sección 4 para el orden recomendado.

## 4. Cómo probar el agente paso a paso

De menor a mayor consumo eléctrico. Con problemas de alimentación, avanza de uno en uno con el
registrador activo (`nohup python3 tools/power_logger.py --interval 0.3 >/dev/null 2>&1 &`).

| Nivel | Comando | Qué valida |
|---|---|---|
| 0 | `venv/bin/python main.py --check` y `venv/bin/python -m unittest discover -s tests` | instalación y lógica |
| 1 | `venv/bin/python tools/probar.py microfono` | micrófono y nivel de ruido |
| 1 | `venv/bin/python tools/probar.py wake` | decir «Hey Vimo» al micrófono real |
| 1 | `venv/bin/python tools/probar.py claude` · `busqueda` | Claude Code y DuckDuckGo |
| 1 | `./start_agent.sh --windowed --config config_prueba.json` | GUI sin voz ni efectos: botón **Texto** → escribir preguntas; **Hablar** → Whisper bajo demanda |
| 2 | `venv/bin/python tools/probar.py sonido` | altavoz (sonido corto) |
| 3 | `venv/bin/python tools/probar.py voz` | Piper: carga de la voz y reproducción |
| 3 | `venv/bin/python tools/probar.py escuchar` | VAD + Whisper con tu voz |
| 4 | `venv/bin/python tools/probar.py foto` · `video 5` | cámara, MP4 verificado con ffprobe |
| 5 | `./start_agent.sh` | flujo completo por voz |

## 5. Limitaciones conocidas

- **Alimentación**: la Raspberry Pi se apaga en picos de consumo; la batería de la X120x cae a
  ~3,19 V. Hay que resolverlo antes de un uso normal ([diagnostico.md](diagnostico.md)).
- **Wake word**: el modelo actual detecta aproximadamente la mitad de las muestras grabadas;
  conviene reentrenarlo.
- **DuckDuckGo** bloquea temporalmente tras muchas consultas seguidas (no hay API oficial).
- **Sin barge-in por voz**: mientras Vimo habla, la wake word está suspendida para no activarse con
  su propia voz. Para interrumpir se usan **Parar** o **Hablar**.
- **Sin análisis visual**: Vimo no interpreta imágenes.
- **Vídeo por software**: la Pi 5 no codifica H.264 por hardware; el 4K es exigente.
- **Latencias**: no se prometen cifras; las medidas reales quedan en `logs/vimo.log`
  («Tiempos del turno»: grabación, STT, Claude, primera frase de TTS, total).
