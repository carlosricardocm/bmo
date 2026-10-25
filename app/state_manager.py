"""Máquina de estados explícita de Vimo.

WARMUP → IDLE → LISTENING → THINKING → SPEAKING → IDLE, más CAPTURING y ERROR.
Las transiciones solo se producen cuando el controlador inicia o termina una operación real;
una transición no permitida se rechaza y se registra (no se aplica en silencio).
Los oyentes se notifican desde el hilo que provoca el cambio: la GUI debe reenviar el aviso
a su hilo principal (ver gui.py).
"""
from __future__ import annotations

import enum
import logging
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)


class State(enum.Enum):
    WARMUP = "warmup"
    IDLE = "idle"
    LISTENING = "listening"
    THINKING = "thinking"
    SPEAKING = "speaking"
    CAPTURING = "capturing"
    ERROR = "error"


LABELS = {
    State.WARMUP: "Iniciando…",
    State.IDLE: "Esperando la palabra de activación",
    State.LISTENING: "Escuchando…",
    State.THINKING: "Pensando…",
    State.SPEAKING: "Hablando…",
    State.CAPTURING: "Usando la cámara…",
    State.ERROR: "Error",
}

TRANSITIONS: dict[State, frozenset[State]] = {
    State.WARMUP: frozenset({State.IDLE, State.ERROR}),
    State.IDLE: frozenset({State.LISTENING, State.THINKING, State.SPEAKING, State.CAPTURING, State.ERROR}),
    State.LISTENING: frozenset({State.THINKING, State.SPEAKING, State.CAPTURING, State.IDLE, State.ERROR}),
    State.THINKING: frozenset({State.SPEAKING, State.CAPTURING, State.IDLE, State.ERROR}),
    State.SPEAKING: frozenset({State.IDLE, State.LISTENING, State.ERROR}),
    State.CAPTURING: frozenset({State.SPEAKING, State.IDLE, State.ERROR}),
    State.ERROR: frozenset({State.IDLE, State.LISTENING, State.THINKING, State.SPEAKING, State.CAPTURING}),
}


class InvalidTransition(RuntimeError):
    pass


class StateManager:
    def __init__(self, initial: State = State.WARMUP):
        self._state = initial
        self._since = time.monotonic()
        self._lock = threading.RLock()
        self._listeners: list[Callable[[State, State, str], None]] = []
        self.history: list[tuple[float, State]] = [(time.time(), initial)]

    @property
    def state(self) -> State:
        with self._lock:
            return self._state

    def seconds_in_state(self) -> float:
        with self._lock:
            return time.monotonic() - self._since

    def add_listener(self, fn: Callable[[State, State, str], None]) -> None:
        self._listeners.append(fn)

    def can(self, new: State) -> bool:
        with self._lock:
            return new == self._state or new in TRANSITIONS[self._state]

    def set(self, new: State, reason: str = "", strict: bool = True) -> bool:
        """Cambia de estado. Devuelve False (o lanza si strict) si la transición no es válida."""
        return self._change(None, new, reason, strict)

    def transition_if(self, expected: set[State], new: State, reason: str = "") -> bool:
        """Cambia a `new` solo si el estado actual está en `expected` (comprobación atómica)."""
        return self._change(expected, new, reason, strict=False)

    def _change(self, expected: set[State] | None, new: State, reason: str, strict: bool) -> bool:
        with self._lock:
            old = self._state
            if expected is not None and old not in expected:
                return False
            if new == old:
                return True
            if new not in TRANSITIONS[old]:
                msg = f"Transición no permitida {old.name} → {new.name} ({reason})"
                if strict:
                    raise InvalidTransition(msg)
                log.warning(msg)
                return False
            self._state = new
            self._since = time.monotonic()
            self.history.append((time.time(), new))
            del self.history[:-100]
            log.info("Estado %s → %s%s", old.name, new.name, f" ({reason})" if reason else "")
        for fn in list(self._listeners):
            try:
                fn(old, new, reason)
            except Exception:  # un oyente defectuoso no debe romper la máquina de estados
                log.exception("Error en oyente de estado")
        return True
