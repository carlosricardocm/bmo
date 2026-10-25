#!/usr/bin/env python3
"""Vimo: agente conversacional para Raspberry Pi 5.

Uso:
    python main.py              # interfaz gráfica (pantalla completa según config.json)
    python main.py --windowed   # en ventana
    python main.py --check      # diagnóstico sin GUI: no reproduce audio ni enciende la cámara
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import signal
import sys

from app.config_manager import PROJECT_ROOT, ConfigManager, resolve_path


def setup_logging(cfg: ConfigManager) -> None:
    level = getattr(logging, cfg.get("logging.level", "INFO"), logging.INFO)
    path = resolve_path(cfg.get("logging.file"))
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(threadName)-10s %(name)s: %(message)s")
    file_handler = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers[:] = [file_handler, console]
    for noisy in ("httpx", "primp", "hickory_net", "hickory_resolver", "rustls", "h2", "hpack", "PIL",
                  "picamera2", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def check(cfg: ConfigManager) -> int:
    """Comprueba recursos y componentes sin efectos visibles (sin audio ni cámara encendida)."""
    from app.animation_manager import FaceLibrary
    from app.audio_manager import SoundBank, list_devices
    from app.camera_manager import CameraManager
    from app.claude_code_client import ClaudeCodeClient
    from app.speech_to_text import WhisperTranscriber
    from app.wake_word import WakeWordDetector, validate_model

    ok = True

    def report(name: str, fn) -> None:
        nonlocal ok
        try:
            print(f"  ✓ {name}: {fn()}")
        except Exception as exc:
            ok = False
            print(f"  ✗ {name}: {exc}")

    print("Diagnóstico de Vimo")
    for w in cfg.warnings:
        print(f"  ! config: {w}")
    report("caras", lambda: {k: len(v) for k, v in FaceLibrary(resolve_path(cfg.get("faces.directory"))).frames.items()})
    snd = cfg.section("sounds")
    sdir = os.path.join(resolve_path(snd["directory"]), snd["language_subdir"])

    def sounds():
        found = {k: len(v) for k, v in SoundBank(sdir).sounds.items()}
        if not found:
            raise RuntimeError(f"no hay sonidos en {sdir}")
        return found
    report("sonidos", sounds)
    report("entradas de audio", lambda: [d["name"] for d in list_devices("input")])
    report("salidas de audio", lambda: [d["name"] for d in list_devices("output")])
    w = cfg.section("wake_word")

    def wake():
        validate_model(resolve_path(w["model_path"]))
        det = WakeWordDetector(resolve_path(w["model_path"]), resolve_path(w["melspec_model_path"]),
                               resolve_path(w["embedding_model_path"]), w["threshold"])
        det.load()
        return f"{det.name} carga correctamente"
    report("wake word", wake)
    s = cfg.section("stt")
    report("whisper.cpp", lambda: (WhisperTranscriber(os.path.expanduser(s["whisper_dir"]), s["server_binary"],
                                                      s["cli_binary"], s["model"]).check() or s["model"]))
    t = cfg.section("tts")

    def voice():
        path = resolve_path(t["voice_model"])
        for f in (path, resolve_path(t["voice_config"]) if t["voice_config"] else path + ".json"):
            if not os.path.isfile(f):
                raise FileNotFoundError(f"no existe {f}")
        return os.path.basename(path)
    report("voz Piper (archivos)", voice)
    client = ClaudeCodeClient(cfg.section("claude"), cfg.get("agent.personality"),
                              os.path.join(PROJECT_ROOT, "data", "claude_workspace"), os.path.join(PROJECT_ROOT, "data"))
    report("Claude Code", lambda: client.check())
    cam = CameraManager(resolve_path(cfg.get("camera.photo_dir")))

    def camera():
        info = cam.detect()
        if not info:
            raise RuntimeError(cam.error)
        return f"{info.get('Model')} ({info.get('Id')})"
    report("cámara (detección)", camera)
    print("Resultado:", "todo correcto" if ok else "hay componentes con problemas")
    return 0 if ok else 1


def acquire_single_instance():
    """Impide abrir dos Vimo a la vez (se pelearían por el micrófono, la cámara y whisper-server).
    flock se libera solo al terminar el proceso, aunque sea por un corte de energía."""
    import fcntl

    path = os.path.join(PROJECT_ROOT, "data", "vimo.lock")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fh = open(path, "a+")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fh.close()
        return None
    fh.seek(0)
    fh.truncate()
    fh.write(str(os.getpid()))
    fh.flush()
    return fh


def notify_user(title: str, text: str, error: bool = False) -> None:
    """Muestra un aviso en el escritorio (útil al lanzar Vimo desde el icono, sin terminal)."""
    import shutil
    import subprocess

    print(f"{title}: {text}", file=sys.stderr)
    if shutil.which("zenity") and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        subprocess.Popen(["zenity", "--error" if error else "--info", "--title", title, "--text", text,
                          "--width", "360"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def main() -> int:
    parser = argparse.ArgumentParser(description="Vimo, agente conversacional")
    parser.add_argument("--config", default=os.path.join(PROJECT_ROOT, "config.json"))
    parser.add_argument("--check", action="store_true", help="diagnóstico sin GUI")
    parser.add_argument("--windowed", action="store_true", help="no usar pantalla completa")
    args = parser.parse_args()
    os.chdir(PROJECT_ROOT)
    cfg = ConfigManager(args.config)
    setup_logging(cfg)
    if args.check:
        return check(cfg)
    if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
        print("No hay sesión gráfica (DISPLAY/WAYLAND_DISPLAY). Ejecuta Vimo desde el escritorio.", file=sys.stderr)
        return 2
    lock = acquire_single_instance()
    if lock is None:
        notify_user("Vimo", "Vimo ya está abierto.")
        return 0
    from app.controller import VimoController
    from app.gui import VimoGUI

    controller = VimoController(cfg)
    gui = VimoGUI(controller)
    if args.windowed:
        gui.fullscreen = True
        gui.toggle_fullscreen()

    def on_signal(signum, _frame):
        logging.getLogger("vimo").info("Señal %s recibida", signum)
        gui.root.after(0, lambda: gui.on_exit(force=True))

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    # Tk solo atiende señales si el intérprete recupera el control periódicamente.
    def tick():
        if not gui._closing:
            gui.root.after(500, tick)
    gui.root.after(500, tick)
    try:
        gui.run()
    finally:
        controller.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
