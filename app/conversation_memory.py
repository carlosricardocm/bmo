"""Historial conversacional propio de Vimo (data/conversation_memory.json).

- Se distingue de las sesiones internas de Claude Code: aquí solo se guarda el texto de cada
  turno (usuario, respuesta, fuentes) y la referencia a la sesión de Claude Code en curso. Las
  sesiones de Claude Code las guarda el propio Claude Code en ~/.claude/projects y no se tocan.
- Longitud limitada (memory.max_turns); se puede desactivar (memory.enabled=false) y reiniciar
  explícitamente.
- No guarda audio, vídeo ni credenciales. Escritura atómica (sobrevive a cortes de energía).
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import threading
import time

log = logging.getLogger(__name__)

_SECRET_HINTS = ("api_key", "apikey", "token", "password", "contraseña", "secret", "sk-ant-")


class ConversationMemory:
    def __init__(self, path: str, enabled: bool = True, max_turns: int = 50):
        self.path = path
        self.enabled = enabled
        self.max_turns = max_turns
        self.turns: list[dict] = []
        self.claude_session_id: str | None = None
        self.claude_session_turns = 0
        self._lock = threading.Lock()
        if enabled:
            self.load()

    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.turns = [t for t in data.get("turns", []) if isinstance(t, dict)][-self.max_turns:]
            self.claude_session_id = data.get("claude_session_id")
            self.claude_session_turns = int(data.get("claude_session_turns") or 0)
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as exc:
            backup = f"{self.path}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}"
            log.warning("Memoria dañada (%s); se guarda en %s y se empieza de cero", exc, backup)
            try:
                os.replace(self.path, backup)
            except OSError:
                pass
            self.turns = []

    def save(self) -> None:
        if not self.enabled:
            return
        with self._lock:
            data = {
                "version": 1,
                "claude_session_id": self.claude_session_id,
                "claude_session_turns": self.claude_session_turns,
                "turns": self.turns[-self.max_turns:],
            }
            tmp = None
            try:
                os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
                fd, tmp = tempfile.mkstemp(prefix=".memory-", dir=os.path.dirname(os.path.abspath(self.path)))
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    json.dump(data, fh, ensure_ascii=False, indent=1)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, self.path)
            except OSError as exc:
                # SD llena o en solo lectura: no debe convertir en error un turno que fue bien.
                log.error("No se pudo guardar la memoria en %s: %s", self.path, exc)
                if tmp and os.path.exists(tmp):
                    try:
                        os.unlink(tmp)
                    except OSError:
                        pass

    def set_enabled(self, enabled: bool) -> None:
        """Al activarla en caliente se carga lo guardado antes: si no, el siguiente save()
        sobrescribiría el historial con una lista casi vacía."""
        if enabled and not self.enabled:
            with self._lock:
                recent, sid, sturns = list(self.turns), self.claude_session_id, self.claude_session_turns
            self.load()
            with self._lock:  # lo hablado mientras estaba desactivada va detrás de lo guardado
                self.turns = (self.turns + recent)[-self.max_turns:]
                if sid:
                    self.claude_session_id, self.claude_session_turns = sid, sturns
        self.enabled = enabled

    @staticmethod
    def _redact(text: str) -> str:
        low = text.lower()
        if any(h in low for h in _SECRET_HINTS) and len(text) > 20:
            # No se conserva texto que parezca contener credenciales.
            return "[texto omitido: podía contener datos sensibles]"
        return text

    def add_turn(self, user: str, assistant: str, sources: list[dict] | None = None,
                 meta: dict | None = None) -> None:
        turn = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "user": self._redact(user),
            "assistant": self._redact(assistant),
        }
        if sources:
            turn["sources"] = [{"title": s.get("title", ""), "url": s.get("url", "")} for s in sources][:10]
        if meta:
            turn["meta"] = meta
        with self._lock:
            self.turns.append(turn)
            del self.turns[:-self.max_turns]
        self.save()

    def set_session(self, session_id: str, turns: int) -> None:
        self.claude_session_id = session_id
        self.claude_session_turns = turns
        self.save()

    def reset(self) -> None:
        """Borra el historial de Vimo y olvida la sesión de Claude Code (no la borra del disco)."""
        with self._lock:
            self.turns = []
            self.claude_session_id = None
            self.claude_session_turns = 0
        self.save()

    def resumable_session(self) -> tuple[str | None, int]:
        """Sesión de Claude Code a reanudar al arrancar (solo si la memoria está activada)."""
        if not self.enabled:
            return None, 0
        return self.claude_session_id, self.claude_session_turns
