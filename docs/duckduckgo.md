# Búsqueda web con DuckDuckGo

La búsqueda **complementa** a Claude Code, no lo sustituye: es Claude quien decide si una pregunta
necesita información actual, mediante una herramienta MCP que expone Vimo. Claude Code no tiene
acceso a Internet por sí mismo en esta configuración (`--tools ""` desactiva WebFetch/WebSearch).

## Servidor MCP `vimo`

- Código: `app/mcp_server.py` (SDK oficial `mcp` 2.3.0, clase `MCPServer`, transporte stdio).
- Instalación: incluido en el venv (`requirements.txt`); no requiere servicio aparte.
- Configuración en Claude Code: Vimo genera `data/mcp_config.json` al arrancar y lo pasa con
  `--mcp-config ... --strict-mcp-config`. **No** se añade a tu configuración global de Claude Code
  (`claude mcp list` no lo mostrará).

```json
{"mcpServers": {"vimo": {"type": "stdio", "command": "<venv>/bin/python",
                         "args": ["<proyecto>/app/mcp_server.py"], "env": {"VIMO_CONFIG": "<proyecto>/config.json"}}}}
```

- Herramienta: `mcp__vimo__web_search` (única permitida con `--allowedTools`).

| Parámetro | Tipo | Valores |
|---|---|---|
| `query` | texto | hasta 300 caracteres |
| `kind` | texto | `web` (defecto) o `news` |
| `max_results` | entero | 1–10 (defecto `web_search.max_results`) |
| `timelimit` | texto | `d`, `w`, `m`, `y` o vacío |

Devuelve JSON con `query`, `engine: "DuckDuckGo"`, `retrieved_at`, un aviso de contenido no
verificado y `results[]` con `title`, `url`, `snippet` (y `date`/`source` en noticias).

- Permisos: ninguno del sistema; solo acceso de red a DuckDuckGo.
- Comprobar que está disponible: en `logs/vimo.log` cada turno con búsqueda registra
  `búsquedas: N`. En una prueba manual, el evento `init` de `claude -p --output-format stream-json`
  lista `mcp_servers: [{"name": "vimo", "status": "connected"}]` y `tools: ["mcp__vimo__web_search"]`
  (verificado).

## Clientes de DuckDuckGo (`app/web_search.py`)

1. **HTML propio** (principal): `POST https://html.duckduckgo.com/html/` con la biblioteca estándar
   y extracción de título, enlace (desenvolviendo las redirecciones `/l/?uddg=`) y fragmento.
   Se ignoran los anuncios.
2. **ddgs 9.16** (alternativa, y primera opción para noticias), siempre con
   `backend="duckduckgo"`. En modo `auto`, ddgs mezcla Google, Bing, Brave y otros, así que no se usa.

Por qué hay un cliente propio: durante las pruebas, ddgs empezó a recibir **HTTP 202** (la página
anti-bots de DuckDuckGo) por la huella de navegador que imita, mientras una petición HTTP normal
seguía obteniendo resultados.

## Bloqueos de DuckDuckGo (limitación conocida)

DuckDuckGo no ofrece una API oficial de búsqueda y limita las peticiones automáticas. Tras unas
decenas de búsquedas en pocos minutos, la IP recibe durante un tiempo la página de verificación
anti-bots, con cualquier cliente. Medidas:

- Ante un bloqueo no se reintenta: se activa una pausa de 60 s que se duplica hasta 15 min. La
  pausa se guarda en `data/search_state.json` porque cada turno de Claude lanza un servidor MCP nuevo.
- Caché de 5 minutos de consultas idénticas.
- Las instrucciones del sistema limitan a Claude a dos búsquedas por pregunta y le piden no
  insistir si la herramienta informa de un bloqueo.
- Si no hay resultados, Claude lo dice. En las pruebas reales lo hizo sin inventar fuentes.

## Seguridad

- Los resultados se tratan como **datos no confiables**: se recortan, se eliminan caracteres de
  control y van con un aviso para que el modelo ignore instrucciones incrustadas. Las reglas del
  sistema lo repiten y exigen no inventar fuentes, fechas ni noticias.
- DuckDuckGo solo recibe el texto de la búsqueda que formula Claude, nunca audio ni el historial.

## Configuración (`config.json` → `web_search`)

| Clave | Defecto | |
|---|---|---|
| `enabled` | `true` | (usa `claude.enable_web_search` para quitar la herramienta a Claude) |
| `region` | `es-es` | región de DuckDuckGo (`mx-es`, `us-en`, `wt-wt`…) |
| `safesearch` | `moderate` | `on`, `moderate`, `off` |
| `max_results` | 5 | 1–10 |
| `timeout_seconds` | 10 | |

## Sin conexión

Si no hay red, la búsqueda falla con un mensaje («No se pudo conectar con DuckDuckGo»), Claude lo
indica en su respuesta y el resto de Vimo sigue funcionando. Sin red, Claude Code tampoco responde;
Vimo lo informa como error de conexión.
