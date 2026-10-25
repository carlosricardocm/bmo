"""Interfaz gráfica de Vimo (Tkinter + Pillow), pensada para la pantalla táctil DSI de 800×480.

Solo presenta información y traslada las pulsaciones al controlador: no hace búsquedas, ni
llamadas a Claude Code, ni graba. Toda actualización de widgets ocurre en el hilo principal de
Tk; los eventos de otros hilos llegan por `controller.events` y se procesan con `after()`.
"""
from __future__ import annotations

import logging
import os
import queue
import random
import time
import tkinter as tk
from tkinter import messagebox, ttk

from .animation_manager import FaceAnimator, FaceLibrary
from .config_manager import DEFAULTS, resolve_path
from .display import Backlight
from .controller import VimoController
from .state_manager import LABELS, State

log = logging.getLogger(__name__)

BG = "#BDFFCB"          # fondo de las caras originales
BAR = "#1F3B2D"
FG = "#F2FFF5"
ACCENT = "#2E8B57"
WARN = "#FFB347"
ERROR_FG = "#FF6B6B"
REC = "#E53935"
STATE_COLORS = {
    State.WARMUP: "#9E9E9E", State.IDLE: "#4CAF50", State.LISTENING: "#03A9F4",
    State.THINKING: "#FFC107", State.SPEAKING: "#8E24AA", State.CAPTURING: "#FF7043",
    State.ERROR: "#E53935",
}


class VimoGUI:
    def __init__(self, controller: VimoController):
        self.c = controller
        cfg = controller.config
        self.root = tk.Tk()
        self.root.title("Vimo")
        self.root.configure(bg=BG)
        self.root.geometry("800x480")
        self.root.minsize(480, 320)
        self.fullscreen = bool(cfg.get("gui.fullscreen"))
        self.root.attributes("-fullscreen", self.fullscreen)
        self.root.protocol("WM_DELETE_WINDOW", self.on_exit)
        self.root.bind("<Escape>", lambda _e: self.toggle_fullscreen())
        self.root.bind("<F11>", lambda _e: self.toggle_fullscreen())
        self.root.bind("<space>", lambda _e: self.c.push_to_talk() if self.root.focus_get() is not self.entry else None)
        self._closing = False
        self._recording_since: float | None = None
        self._showing_text = False
        self._build()
        self.faces = FaceLibrary(resolve_path(cfg.get("faces.directory")))
        # Los tiempos por defecto se completan con los de config.json (que puede no tener los nuevos).
        frame_ms = {**DEFAULTS["faces"]["frame_ms"], **(cfg.get("faces.frame_ms") or {})}
        self.animator = FaceAnimator(self.canvas, self.faces, frame_ms, BG)
        self.animator.set_state(State.WARMUP.value)
        # Caras de música: solo con Vimo en reposo y la música sonando.
        self._ui_state = State.WARMUP
        self._music_playing = False
        self._music_title: str | None = None
        self._music_face: str | None = None
        self._music_switch_id: str | None = None
        self._music_faces = [n for n in (cfg.get("faces.music_animations") or []) if n in self.faces.frames]
        # Gestos en reposo: de vez en cuando una animación breve y vuelta a la cara «idle».
        self._idle_faces = [n for n in (cfg.get("faces.idle_animations") or []) if n in self.faces.frames]
        self._idle_gesture: str | None = None
        self._idle_gesture_last: str | None = None
        self._idle_timer: str | None = None
        # Pantalla «apagada» por inactividad.
        self.backlight = Backlight(resolve_path("data/backlight.saved"), cfg.get("gui.backlight_path"))
        self.backlight.restore_saved()   # por si un cierre brusco la dejó apagada
        self._sleeping = False
        self._sleep_overlay: tk.Frame | None = None
        self._last_activity = time.monotonic()
        for seq in ("<Motion>", "<ButtonPress>", "<KeyPress>"):
            self.root.bind_all(seq, self._on_user_activity, add="+")
        self.root.after(5000, self._sleep_check)
        self.root.after(50, self._poll)

    # ------------------------------------------------------------------------------------
    # Construcción
    # ------------------------------------------------------------------------------------
    def _build(self) -> None:
        font_btn = ("Sans", 11, "bold")
        top = tk.Frame(self.root, bg=BAR, height=34)
        top.pack(side="top", fill="x")
        top.pack_propagate(False)
        self.state_dot = tk.Canvas(top, width=16, height=16, bg=BAR, highlightthickness=0)
        self.state_dot.pack(side="left", padx=(10, 4))
        self._dot = self.state_dot.create_oval(2, 2, 14, 14, fill=STATE_COLORS[State.WARMUP], outline="")
        self.state_lbl = tk.Label(top, text=LABELS[State.WARMUP], bg=BAR, fg=FG, font=("Sans", 11, "bold"))
        self.state_lbl.pack(side="left")
        self.rec_lbl = tk.Label(top, text="", bg=BAR, fg=REC, font=("Sans", 12, "bold"))
        self.rec_lbl.pack(side="right", padx=10)
        self.msg_lbl = tk.Label(top, text="", bg=BAR, fg=FG, font=("Sans", 10), anchor="w")
        self.msg_lbl.pack(side="left", fill="x", expand=True, padx=12)

        bottom = tk.Frame(self.root, bg=BAR)
        bottom.pack(side="bottom", fill="x")
        buttons = [
            ("listen", "Escucha", self.c.toggle_listening),
            ("talk", "Hablar", self.c.push_to_talk),
            ("stop", "Parar", self.c.interrupt),
            ("photo", "Foto", self.c.take_photo),
            ("rec", "Grabar", self.c.toggle_recording),
            ("text", "Texto", self.toggle_text),
            ("new", "Nueva", self.on_new_conversation),
            ("settings", "Ajustes", self.open_settings),
            ("exit", "Salir", self.on_exit),
        ]
        self.buttons: dict[str, tk.Button] = {}
        for i, (key, label, cmd) in enumerate(buttons):
            b = tk.Button(bottom, text=label, command=cmd, font=font_btn, bg=ACCENT, fg="white",
                          activebackground="#3CB371", activeforeground="white", relief="flat", bd=0,
                          highlightthickness=0, padx=2, pady=12)
            b.grid(row=0, column=i, sticky="nsew", padx=2, pady=4)
            bottom.grid_columnconfigure(i, weight=1, uniform="btn")
            self.buttons[key] = b

        self.middle = tk.Frame(self.root, bg=BG)
        self.middle.pack(side="top", fill="both", expand=True)

        # Barra de música: solo visible mientras hay música en curso.
        self.music_bar = tk.Frame(self.root, bg="#173025")
        self.music_lbl = tk.Label(self.music_bar, text="", bg="#173025", fg=FG, font=("Sans", 10, "bold"),
                                  anchor="w")
        self.music_buttons: dict[str, tk.Button] = {}
        # Los botones se colocan primero (a la derecha) para que nunca queden cortados; el título
        # ocupa el espacio restante.
        for key, label, cmd in reversed([("pause", "Pausa", self.c.music_toggle_pause),
                                         ("next", "Siguiente", self.c.music_next),
                                         ("down", "Vol −", lambda: self.c.music_volume(-self._music_step())),
                                         ("up", "Vol +", lambda: self.c.music_volume(self._music_step())),
                                         ("stop", "Parar", self.c.music_stop)]):
            b = tk.Button(self.music_bar, text=label, command=cmd, font=("Sans", 10, "bold"), bg="#3B6E57",
                          fg="white", activebackground="#4C8A6D", relief="flat", bd=0, padx=7, pady=6)
            b.pack(side="right", padx=2, pady=3)
            self.music_buttons[key] = b
        self.music_buttons["stop"].configure(bg="#8E3B3B", activebackground="#A84B4B")
        self.music_lbl.pack(side="left", fill="x", expand=True, padx=8)
        self._music_visible = False
        self.canvas = tk.Canvas(self.middle, bg=BG, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)

        # Panel de texto: transcripción, respuesta original, fuentes y entrada escrita.
        self.text_panel = tk.Frame(self.middle, bg=BG)
        self.text = tk.Text(self.text_panel, wrap="word", font=("Sans", 12), bg="#F4FFF6", fg="#123",
                            relief="flat", padx=10, pady=8, state="disabled")
        scroll = ttk.Scrollbar(self.text_panel, command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        entry_row = tk.Frame(self.text_panel, bg=BG)
        entry_row.pack(side="bottom", fill="x", pady=4)
        self.entry = tk.Entry(entry_row, font=("Sans", 13))
        self.entry.pack(side="left", fill="x", expand=True, padx=(6, 4), ipady=6)
        self.entry.bind("<Return>", lambda _e: self._send_text())
        tk.Button(entry_row, text="Enviar", command=self._send_text, font=font_btn, bg=ACCENT, fg="white",
                  relief="flat").pack(side="right", padx=6)
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)
        for tag, opts in {"user": {"foreground": "#0B5", "font": ("Sans", 12, "bold")},
                          "vimo": {"foreground": "#123"},
                          "source": {"foreground": "#1565C0", "font": ("Sans", 10)},
                          "error": {"foreground": "#C62828"},
                          "info": {"foreground": "#555", "font": ("Sans", 10, "italic")}}.items():
            self.text.tag_configure(tag, **opts)

    # ------------------------------------------------------------------------------------
    # Eventos del controlador (hilo principal)
    # ------------------------------------------------------------------------------------
    def _poll(self) -> None:
        try:
            for _ in range(50):
                event = self.c.events.get_nowait()
                self._handle(event)
        except queue.Empty:
            pass
        except Exception:
            log.exception("Error procesando evento de la GUI")
        if self._recording_since is not None:
            secs = int(time.monotonic() - self._recording_since)
            self.rec_lbl.configure(text=f"● REC {secs // 60:02d}:{secs % 60:02d}")
        if not self._closing:
            self.root.after(100, self._poll)

    def _handle(self, event: tuple) -> None:
        kind = event[0]
        if kind == "state":
            st: State = event[1]
            self.state_dot.itemconfigure(self._dot, fill=STATE_COLORS[st])
            self.state_lbl.configure(text=LABELS[st])
            self._ui_state = st
            if st != State.IDLE:
                self._last_activity = time.monotonic()
                self._wake("Vimo en actividad")    # p. ej. «Hey Vimo» con la pantalla apagada
            self._refresh_face()
            self.buttons["stop"].configure(state="normal" if st in (State.SPEAKING, State.THINKING,
                                                                     State.LISTENING) else "disabled")
        elif kind == "activity":
            self._last_activity = time.monotonic()
            self._wake(event[1])
        elif kind == "status":
            self._message(event[1], FG)
        elif kind == "progress":
            self._message(event[1], WARN)
            self._append(event[1] + "\n", "info")
        elif kind == "error":
            self._message(event[1], ERROR_FG)
            self._append(f"⚠ {event[1]}\n", "error")
        elif kind == "user_text":
            self._append(f"\nTú: {event[1]}\n", "user")
        elif kind == "response":
            text, sources = event[1], event[2]
            if not text and not sources:
                self._clear_text()
                return
            self._append(f"Vimo: {text}\n", "vimo")
            if sources:
                self._append("Fuentes (DuckDuckGo):\n", "info")
                for s in sources[:8]:
                    title = s.get("title") or s["url"]
                    self._append(f"  • {title}\n    {s['url']}\n", "source")
        elif kind == "listening":
            on = event[1]
            self.buttons["listen"].configure(text="Escucha ✓" if on else "Escucha ✗",
                                             bg=ACCENT if on else "#7A7A7A")
        elif kind == "recording":
            active, info = event[1], event[2]
            if active:
                self._wake("grabación")
                self._recording_since = time.monotonic()
                self.buttons["rec"].configure(text="Detener", bg=REC)
                self._message(f"Grabando en {os.path.basename(info)}", WARN)
            else:
                self._recording_since = None
                self.rec_lbl.configure(text="")
                self.buttons["rec"].configure(text="Grabar", bg=ACCENT)
                if info:
                    self._append(f"Vídeo guardado: {info}\n", "info")
        elif kind == "music":
            st = event[1]
            playing = st["active"] and not st["paused"]
            if playing and st["title"] != self._music_title:
                self._music_face = self._pick_music_face()   # canción nueva → otra animación
            self._music_title = st["title"] if st["active"] else None
            self._music_playing = playing
            self._refresh_face()
            if st["active"]:
                if not self._music_visible:
                    self.music_bar.pack(side="bottom", fill="x", before=self.middle)
                    self._music_visible = True
                icon = "⏸" if st["paused"] else "♪"
                genre = f"{st['genre'].capitalize()} · " if st.get("genre") else ""
                room = max(12, 34 - len(genre))
                title = st["title"] if len(st["title"]) <= room else st["title"][:room - 1] + "…"
                self.music_lbl.configure(text=f"{icon} {genre}{title}  ·  {st['volume']} %")
                self.music_buttons["pause"].configure(text="Reanudar" if st["paused"] else "Pausa")
            elif self._music_visible:
                self.music_bar.pack_forget()
                self._music_visible = False
        elif kind == "components":
            self._append("Estado de los componentes:\n", "info")
            for name, (ok, msg) in event[1].items():
                self._append(f"  {'✓' if ok else '✗'} {name}: {msg}\n", "info" if ok else "error")

    # ------------------------------------------------------------------------------------
    # Caras durante la música
    # ------------------------------------------------------------------------------------
    def _pick_music_face(self) -> str | None:
        options = [n for n in self._music_faces if n != self._music_face] or self._music_faces
        return random.choice(options) if options else None

    def _refresh_face(self) -> None:
        """Cara según el estado real; con música sonando y Vimo en reposo, una animación musical
        que cambia cada `faces.music_switch_seconds`."""
        if self._sleeping:
            return
        if self._ui_state == State.IDLE and self._music_playing and self._music_faces:
            self._cancel_idle_gesture()
            if self._music_face is None:
                self._music_face = self._pick_music_face()
            self.animator.set_state(self._music_face)
            if self._music_switch_id is None:
                delay = int(float(self.c.config.get("faces.music_switch_seconds") or 20) * 1000)
                self._music_switch_id = self.root.after(delay, self._rotate_music_face)
        elif self._ui_state == State.IDLE:
            self._cancel_music_switch()
            if self._idle_gesture is None:
                self.animator.set_state(State.IDLE.value)
            self._schedule_idle_gesture()
        else:
            self._cancel_music_switch()
            self._cancel_idle_gesture()
            self.animator.set_state(self._ui_state.value)

    # ------------------------------------------------------------------------------------
    # Gestos en reposo
    # ------------------------------------------------------------------------------------
    def _schedule_idle_gesture(self) -> None:
        if self._idle_timer is not None or not self._idle_faces:
            return
        lo = float(self.c.config.get("faces.idle_gesture_min_seconds") or 8)
        hi = max(lo, float(self.c.config.get("faces.idle_gesture_max_seconds") or 20))
        self._idle_timer = self.root.after(int(random.uniform(lo, hi) * 1000), self._play_idle_gesture)

    def _play_idle_gesture(self) -> None:
        self._idle_timer = None
        if self._sleeping or self._ui_state != State.IDLE or self._music_playing:
            return
        options = [n for n in self._idle_faces if n != self._idle_gesture_last] or self._idle_faces
        name = random.choice(options)
        self._idle_gesture = self._idle_gesture_last = name
        self.animator.set_state(name)
        # Un ciclo completo de la animación y vuelta a la cara de reposo.
        frames = len(self.faces.frames.get(name, [])) or 1
        duration = frames * self.animator.frame_ms.get(name, self.animator.frame_ms.get("default", 500))
        self._idle_timer = self.root.after(duration, self._end_idle_gesture)

    def _end_idle_gesture(self) -> None:
        self._idle_timer = None
        self._idle_gesture = None
        self._refresh_face()

    def _cancel_idle_gesture(self) -> None:
        if self._idle_timer is not None:
            try:
                self.root.after_cancel(self._idle_timer)
            except Exception:
                pass
            self._idle_timer = None
        self._idle_gesture = None

    # ------------------------------------------------------------------------------------
    # Pantalla «apagada» por inactividad
    # ------------------------------------------------------------------------------------
    def _on_user_activity(self, _event=None) -> None:
        self._last_activity = time.monotonic()
        if self._sleeping:
            self._wake("toque, ratón o teclado")

    def _busy(self) -> bool:
        dialogs = any(isinstance(w, tk.Toplevel) for w in self.root.winfo_children())
        return (self._ui_state != State.IDLE or self._music_playing or self._recording_since is not None
                or dialogs)

    def _sleep_check(self) -> None:
        if self._closing:
            return
        self.root.after(5000, self._sleep_check)
        limit = float(self.c.config.get("gui.sleep_after_seconds") or 0)
        if not limit or self._sleeping:
            return
        if self._busy():
            self._last_activity = time.monotonic()
        elif time.monotonic() - self._last_activity >= limit:
            self._sleep()

    def _sleep(self) -> None:
        if self._sleeping:
            return
        self._sleeping = True
        self._cancel_idle_gesture()
        self._cancel_music_switch()
        self.animator.stop()
        overlay = tk.Frame(self.root, bg="black", cursor="none")
        overlay.place(x=0, y=0, relwidth=1, relheight=1)
        overlay.lift()
        # El primer toque solo despierta: el botón que hubiera debajo no se pulsa.
        for seq in ("<ButtonPress>", "<Motion>", "<KeyPress>"):
            overlay.bind(seq, lambda _e: (self._wake("toque, ratón o teclado"), "break")[1])
        overlay.focus_set()
        self._sleep_overlay = overlay
        if self.c.config.get("gui.sleep_backlight"):
            self.backlight.off()
        log.info("Pantalla en reposo tras %.0f s sin actividad", time.monotonic() - self._last_activity)

    def _wake(self, reason: str) -> None:
        if not self._sleeping:
            return
        self._sleeping = False
        self.backlight.on()
        if self._sleep_overlay is not None:
            self._sleep_overlay.destroy()
            self._sleep_overlay = None
        self._last_activity = time.monotonic()
        log.info("Pantalla despierta (%s)", reason)
        self._refresh_face()

    def _rotate_music_face(self) -> None:
        self._music_switch_id = None
        self._music_face = self._pick_music_face()
        self._refresh_face()

    def _cancel_music_switch(self) -> None:
        if self._music_switch_id is not None:
            try:
                self.root.after_cancel(self._music_switch_id)
            except Exception:
                pass
            self._music_switch_id = None

    def _music_step(self) -> int:
        return int(self.c.config.get("music.volume_step") or 10)

    def _message(self, text: str, color: str) -> None:
        self.msg_lbl.configure(text=text[:120], fg=color)

    def _append(self, text: str, tag: str) -> None:
        self.text.configure(state="normal")
        self.text.insert("end", text, tag)
        # Limita el panel para que no crezca indefinidamente.
        lines = int(self.text.index("end-1c").split(".")[0])
        if lines > 600:
            self.text.delete("1.0", f"{lines - 500}.0")
        self.text.configure(state="disabled")
        self.text.see("end")

    def _clear_text(self) -> None:
        self.text.configure(state="normal")
        self.text.delete("1.0", "end")
        self.text.configure(state="disabled")

    # ------------------------------------------------------------------------------------
    # Acciones
    # ------------------------------------------------------------------------------------
    def toggle_text(self) -> None:
        self._showing_text = not self._showing_text
        if self._showing_text:
            self.canvas.pack_forget()
            self.text_panel.pack(fill="both", expand=True)
            self.entry.focus_set()
        else:
            self.text_panel.pack_forget()
            self.canvas.pack(fill="both", expand=True)
            self.root.focus_set()
        self.buttons["text"].configure(text="Cara" if self._showing_text else "Texto")

    def _send_text(self) -> None:
        text = self.entry.get().strip()
        if text:
            self.entry.delete(0, "end")
            self.c.submit_text(text)

    def toggle_fullscreen(self) -> None:
        self.fullscreen = not self.fullscreen
        self.root.attributes("-fullscreen", self.fullscreen)

    def on_new_conversation(self) -> None:
        if messagebox.askyesno("Nueva conversación", "¿Olvidar la conversación actual y empezar otra?",
                               parent=self.root):
            self.c.reset_conversation()

    def open_settings(self) -> None:
        SettingsDialog(self.root, self.c)

    def on_exit(self, force: bool = False) -> None:
        """Cierra Vimo. `force` (señal del sistema): sin preguntar; la grabación se detiene y se guarda."""
        if self._closing:
            return
        if not force and self.c.video and self.c.video.recording and not messagebox.askyesno(
                "Salir", "Hay una grabación en curso. Se detendrá y se guardará. ¿Salir?", parent=self.root):
            return
        self._closing = True
        self._wake("cierre")
        self._message("Cerrando…", FG)
        self._cancel_music_switch()
        self._cancel_idle_gesture()
        self.animator.stop()
        self.root.update_idletasks()
        try:
            self.c.shutdown()
        finally:
            self.root.destroy()

    def run(self) -> None:
        self.c.start()
        self.root.mainloop()


class SettingsDialog:
    """Ajustes más habituales. Los que requieren reiniciar Vimo se indican como tales."""

    def __init__(self, parent: tk.Tk, controller: VimoController):
        self.c = controller
        cfg = controller.config
        self.win = tk.Toplevel(parent)
        self.win.title("Ajustes de Vimo")
        self.win.configure(bg=BG)
        self.win.geometry("760x440")
        self.win.transient(parent)
        self.win.after(50, self.win.grab_set)
        frm = tk.Frame(self.win, bg=BG)
        frm.pack(fill="both", expand=True, padx=12, pady=8)
        self.vars: dict[str, tk.Variable] = {}
        row = 0

        def label(text: str, note: str = "") -> None:
            nonlocal row
            tk.Label(frm, text=text, bg=BG, font=("Sans", 11, "bold"), anchor="w").grid(
                row=row, column=0, sticky="w", pady=3)
            if note:
                tk.Label(frm, text=note, bg=BG, fg="#555", font=("Sans", 9)).grid(row=row, column=2, sticky="w")

        def scale(key: str, text: str, lo: float, hi: float, res: float, note: str = "") -> None:
            nonlocal row
            label(text, note)
            var = tk.DoubleVar(value=cfg.get(key))
            tk.Scale(frm, variable=var, from_=lo, to=hi, resolution=res, orient="horizontal", length=300,
                     bg=BG, highlightthickness=0).grid(row=row, column=1, sticky="w")
            self.vars[key] = var
            row += 1

        def choice(key: str, text: str, options: list, note: str = "") -> None:
            nonlocal row
            label(text, note)
            current = cfg.get(key)
            var = tk.StringVar(value=str(current) if current is not None else "default")
            values = list(dict.fromkeys([var.get()] + [str(o) for o in options]))
            ttk.Combobox(frm, textvariable=var, values=values, width=34, state="readonly").grid(
                row=row, column=1, sticky="w")
            self.vars[key] = var
            row += 1

        def check(key: str, text: str, note: str = "") -> None:
            nonlocal row
            label(text, note)
            var = tk.BooleanVar(value=bool(cfg.get(key)))
            tk.Checkbutton(frm, variable=var, bg=BG).grid(row=row, column=1, sticky="w")
            self.vars[key] = var
            row += 1

        try:
            from .audio_manager import list_devices
            inputs = [d["name"] for d in list_devices("input")]
            outputs = ["default"] + [d["name"] for d in list_devices("output")]
        except Exception:
            inputs, outputs = [], ["default"]
        whisper_models = []
        mdir = os.path.join(os.path.expanduser(cfg.get("stt.whisper_dir")), "models")
        if os.path.isdir(mdir):
            whisper_models = sorted(f"models/{f}" for f in os.listdir(mdir)
                                    if f.startswith("ggml-") and f.endswith(".bin"))
        scale("wake_word.threshold", "Umbral wake word", 0.1, 0.95, 0.01, "más alto = menos falsas activaciones")
        scale("tts.length_scale", "Velocidad de voz", 0.7, 1.5, 0.05, "más alto = más lento")
        scale("listening.silence_seconds", "Silencio para terminar (s)", 0.5, 3.0, 0.1)
        choice("audio.input_device", "Micrófono", inputs, "requiere reiniciar")
        choice("audio.output_device", "Altavoz", outputs, "requiere reiniciar")
        choice("stt.model", "Modelo Whisper", whisper_models, "requiere reiniciar")
        choice("stt.language", "Idioma de reconocimiento", ["es", "en", "auto", "fr", "pt", "it", "de"],
               "requiere reiniciar")
        choice("video.profile", "Perfil de vídeo", list(cfg.get("video.profiles").keys()))
        check("memory.enabled", "Guardar historial")
        check("gui.fullscreen", "Pantalla completa al iniciar")
        # Valores tal como los muestran los controles (tk.Scale redondea a su resolución y las listas
        # convierten None en texto): solo se guarda lo que el usuario cambie respecto a esto.
        self.initial = {key: var.get() for key, var in self.vars.items()}

        btns = tk.Frame(self.win, bg=BG)
        btns.pack(side="bottom", fill="x", pady=8)
        tk.Button(btns, text="Guardar", command=self.save, font=("Sans", 12, "bold"), bg=ACCENT, fg="white",
                  relief="flat", padx=20, pady=8).pack(side="right", padx=12)
        tk.Button(btns, text="Cancelar", command=self.win.destroy, font=("Sans", 12), relief="flat",
                  padx=20, pady=8).pack(side="right")

    def save(self) -> None:
        changes = {}
        for key, var in self.vars.items():
            value = var.get()
            if value == self.initial.get(key):
                continue
            current = self.c.config.get(key)
            if isinstance(value, float):
                value = int(value) if isinstance(current, int) and not isinstance(current, bool) else round(value, 4)
            if value != current:
                changes[key] = value
        if changes:
            warnings = self.c.apply_settings(changes)
            restart = {"audio.input_device", "audio.output_device", "stt.model", "stt.language"} & set(changes)
            msg = "Ajustes guardados."
            if restart:
                msg += "\nReinicia Vimo para aplicar: " + ", ".join(sorted(restart))
            if warnings:
                msg += "\n\nAvisos:\n" + "\n".join(warnings)
            messagebox.showinfo("Ajustes", msg, parent=self.win)
        self.win.destroy()
