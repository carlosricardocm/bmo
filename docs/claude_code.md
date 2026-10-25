# Claude Code como motor conversacional

Vimo usa la instalación existente de Claude Code (verificado: **2.1.307**,
`/usr/lib/node_modules/@anthropic-ai/claude-code/bin/claude.exe`, autenticación `claude.ai`).
No instala otra copia, no modifica `~/.claude` ni `~/.claude.json` y no usa otro LLM como respaldo.
Si Claude Code no está disponible, Vimo lo dice y no inventa respuestas.

Documentación oficial: <https://code.claude.com/docs>.

## Cómo se invoca

Cada turno lanza un proceso (sin shell, argumentos separados) desde `data/claude_workspace/`:

```
claude -p --output-format stream-json --verbose
       --tools ""                                   # sin herramientas integradas
       --permission-mode dontAsk                    # lo no permitido se deniega, sin preguntar
       --setting-sources "" --disable-slash-commands
       --system-prompt "<personalidad + reglas de voz y de búsqueda>"
       --mcp-config data/mcp_config.json --strict-mcp-config
       --allowedTools mcp__vimo__web_search
       --session-id <uuid>        (primer turno)   |   --resume <uuid>   (siguientes)
```

- El **texto del usuario se envía por stdin** precedido de la fecha y hora locales reales. Nunca va
  en la línea de comandos, así que un texto que empiece por `-` o contenga `;` no tiene efecto.
- `stream-json` permite separar los mensajes intermedios (llamadas a herramientas, resultados) de la
  respuesta final (`{"type":"result"}`). Solo la respuesta final se lee en voz alta; las llamadas a
  `web_search` se muestran como progreso y sus resultados alimentan la lista de fuentes de la GUI.
- Las variables `CLAUDECODE*`/`CLAUDE_CODE_*` heredadas de una sesión de Claude Code se eliminan del
  entorno del subproceso.

Todas estas opciones se comprobaron con `claude --help` en la versión instalada. Otras versiones
pueden cambiarlas: si una actualización rompe la integración, `main.py --check` y `logs/vimo.log`
muestran el error.

## Sesiones y contexto

- Un UUID por conversación. Primer turno con `--session-id`, siguientes con `--resume` (verificado:
  el contexto se conserva entre turnos).
- El UUID y el número de turnos se guardan en `data/conversation_memory.json`. Si la memoria está
  activada, la conversación continúa tras reiniciar Vimo.
- **Nueva conversación** (botón o «nueva conversación») crea otro UUID. La sesión anterior no se
  borra del disco.
- Tras `claude.session_max_turns` turnos (40 por defecto) se abre una sesión nueva para acotar el
  contexto y el coste.
- Si Claude Code ya no encuentra la sesión, Vimo crea una nueva y continúa.
- Un único hilo procesa las peticiones: nunca hay dos procesos sobre la misma sesión. Se admite una
  en curso y otra en cola; el resto se rechaza con un mensaje.
- Las sesiones de Vimo quedan en `~/.claude/projects/` bajo la ruta de `data/claude_workspace`,
  separadas de tus otros proyectos.

## Tiempos límite, cancelación y errores

- `claude.timeout_seconds` (120 s): al vencer se termina el **grupo de procesos** completo
  (`SIGTERM`, luego `SIGKILL`), incluido el servidor MCP. No quedan procesos huérfanos.
- **Parar** cancela la petición en curso de la misma forma.
- Errores reconocidos: autenticación (`/login`, 401), límite de uso (429, «usage limit»), red
  (`ENOTFOUND`, `ECONNREFUSED`…), tiempo límite y sesión perdida. Cada uno muestra un mensaje claro.
- Se registran duración, código de salida, número de búsquedas y denegaciones de permisos
  (`logs/vimo.log`). No se registra el contenido de las credenciales.

## Permisos

- Sin Bash, sin lectura/escritura de archivos, sin WebFetch: `--tools ""`.
- Solo la herramienta `mcp__vimo__web_search` está permitida (`--allowedTools`), con
  `--strict-mcp-config` para ignorar otros servidores MCP del usuario.
- `config.json` → `claude.extra_args` es una lista blanca: solo admite `--model`, `--fallback-model`
  y `--effort` (con su valor, `--opción valor` o `--opción=valor`). Cualquier otra opción (`--tools`,
  `--allowedTools`, `--add-dir`, `--settings`, `--mcp-config`, `--permission-mode`,
  `--dangerously-skip-permissions`…) se elimina con un aviso, porque podría devolver a Claude el
  acceso a la terminal o a los archivos.
- La cámara **no** se expone a Claude Code: las fotos y vídeos solo los activa el usuario
  (órdenes locales explícitas o botones; ver `app/intents.py`).

## Configuración (`config.json` → `claude`)

| Clave | Por defecto | Descripción |
|---|---|---|
| `executable` | `claude` | ruta o nombre en el PATH |
| `model` | `null` | modelo (`--model`); `null` = el predeterminado de tu cuenta |
| `timeout_seconds` | 120 | límite por turno |
| `session_max_turns` | 40 | turnos antes de abrir sesión nueva (0 = sin límite) |
| `max_budget_usd` | `null` | `--max-budget-usd` (solo cuentas con API key) |
| `enable_web_search` | `true` | expone o no `web_search` |
| `extra_args` | `[]` | argumentos adicionales validados |

La personalidad está en `agent.personality`. Las reglas de voz y de búsqueda se añaden siempre
(`app/claude_code_client.py`, `VOICE_RULES`).

## Comprobación manual

```bash
claude auth status                      # sesión
venv/bin/python main.py --check         # versión, ruta y sesión desde Vimo
```

## Análisis visual

No está implementado. Vimo nunca afirma «ver» nada, y las reglas del sistema se lo indican también
a Claude. Para añadirlo de forma explícita se podría permitir a Claude la herramienta `Read`
limitada a la ruta de una foto recién tomada (`--tools Read --allowedTools "Read(<ruta>)"`). Esa
opción no se ha verificado.
