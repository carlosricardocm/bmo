"""Canal local entre el servidor MCP (proceso de Claude Code) y Vimo para ejecutar órdenes.

Claude Code lanza app/mcp_server.py como subproceso; la herramienta `ejecutar_orden` de ese
servidor no puede tocar la música ni las luces de Vimo directamente (viven en el proceso de Vimo),
así que envía la orden por este socket Unix. Vimo la ejecuta con los mismos reconocedores que las
órdenes de voz y devuelve lo que habría dicho.

Protocolo: una línea JSON por conexión, {"orden": "..."} → {"ok": bool, "resultado": "..."}.
El socket solo es accesible para el usuario de Vimo (permisos 600).
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
from typing import Callable

log = logging.getLogger(__name__)

MAX_REQUEST = 4096


class ControlServer:
    def __init__(self, path: str, handler: Callable[[str], dict]):
        self.path = path
        self.handler = handler
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def start(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        try:
            os.unlink(self.path)  # resto de una ejecución anterior
        except FileNotFoundError:
            pass
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old = os.umask(0o177)
        try:
            sock.bind(self.path)
        finally:
            os.umask(old)
        sock.listen(4)
        sock.settimeout(0.5)
        self._sock = sock
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="control", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)
        if self._sock:
            self._sock.close()
            self._sock = None
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._serve, args=(conn,), name="control-req", daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        with conn:
            conn.settimeout(10)
            try:
                data = b""
                while b"\n" not in data and len(data) < MAX_REQUEST:
                    chunk = conn.recv(1024)
                    if not chunk:
                        break
                    data += chunk
                text = str(json.loads(data.decode("utf-8")).get("orden", "")).strip()
                reply = self.handler(text) if text else {"ok": False, "resultado": "Orden vacía."}
            except Exception as exc:  # una petición rota no tumba el canal
                log.warning("Petición de control inválida: %s", exc)
                reply = {"ok": False, "resultado": f"Error interno: {exc}"}
            try:
                conn.sendall((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
            except OSError:
                pass


def send(path: str, orden: str, timeout: float = 60.0) -> dict:
    """Cliente (lo usa el servidor MCP)."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        s.connect(path)
        s.sendall((json.dumps({"orden": orden}, ensure_ascii=False) + "\n").encode("utf-8"))
        data = b""
        while not data.endswith(b"\n"):
            chunk = s.recv(4096)
            if not chunk:
                break
            data += chunk
    return json.loads(data.decode("utf-8"))
