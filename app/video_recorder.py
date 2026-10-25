"""Grabación de vídeo bajo demanda con Picamera2 (IMX519).

Flujo:
1. Comprueba espacio libre y que no haya otra grabación.
2. Configura el flujo de vídeo (YUV420, tamaño y FPS del perfil) y graba con H264Encoder
   (en la Raspberry Pi 5 es el codificador software LibavH264Encoder: no tiene codificador
   H.264 por hardware) hacia un contenedor MPEG-TS mediante PyavOutput (<nombre>.ts.part).
   MPEG-TS sigue siendo legible si el proceso muere a mitad de grabación.
3. Al detener, finaliza el codificador, libera la cámara y remultiplexa a MP4 con
   `ffmpeg -c copy` (sin recodificar; conserva las marcas de tiempo).
4. Verifica el MP4 con ffprobe (códec, resolución, duración, fotogramas) antes de darlo por bueno.

Recuperación: `recover_incomplete()` convierte a MP4 los .ts.part que quedaran de una sesión
interrumpida (corte de luz, cierre forzado). Lo grabado hasta el último paquete escrito se
conserva; lo que estuviera en búferes en memoria se pierde.
"""
from __future__ import annotations

import glob
import json
import logging
import os
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass
from typing import Callable

from .camera_manager import CameraError, CameraManager, timestamped_path

log = logging.getLogger(__name__)


@dataclass
class RecordingResult:
    path: str
    duration: float
    size_bytes: int
    codec: str
    width: int
    height: int
    frames: int | None
    fps: float | None
    requested_fps: float | None = None

    rotation_cw: int = 0  # giro de visualización guardado en el MP4

    @property
    def display_size(self) -> tuple[int, int]:
        """Tamaño tal como se ve al reproducirlo (con el giro aplicado)."""
        return (self.height, self.width) if self.rotation_cw % 180 else (self.width, self.height)

    def describe(self) -> str:
        fps = f", {self.fps:.1f} fps" if self.fps else ""
        w, h = self.display_size
        rot = f", girado {self.rotation_cw}°" if self.rotation_cw else ""
        return (f"{os.path.basename(self.path)}: {self.duration:.1f} s, {w}x{h}{fps}{rot}, "
                f"{self.size_bytes / 1e6:.1f} MB")


def free_mb(path: str) -> float:
    os.makedirs(path, exist_ok=True)
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize / 1e6


def probe(path: str) -> dict:
    """Información del vídeo con ffprobe. Lanza CameraError si el archivo no es válido."""
    if not shutil.which("ffprobe"):
        raise CameraError("ffprobe no está instalado (apt install ffmpeg)")
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
         "-show_entries", "stream=codec_name,width,height,nb_read_packets,avg_frame_rate:stream_side_data=rotation:"
                          "format=duration,format_name",
         "-of", "json", path], capture_output=True, text=True, timeout=120)
    if proc.returncode != 0:
        raise CameraError(f"ffprobe no puede leer {os.path.basename(path)}: {proc.stderr.strip()[:200]}")
    data = json.loads(proc.stdout or "{}")
    if not data.get("streams"):
        raise CameraError(f"{os.path.basename(path)} no contiene vídeo")
    return data


def result_from_probe(path: str, data: dict, requested_fps: float | None = None) -> RecordingResult:
    s = data["streams"][0]
    duration = float(data.get("format", {}).get("duration") or 0)
    frames = int(s["nb_read_packets"]) if str(s.get("nb_read_packets", "")).isdigit() else None
    fps = frames / duration if frames and duration > 0 else None
    # ffprobe da la rotación de la matriz de visualización en sentido antihorario (-90 = 90° horario).
    rotation = 0
    for side in s.get("side_data_list", []) or []:
        if "rotation" in side:
            rotation = int(round(-float(side["rotation"]))) % 360
    return RecordingResult(path, duration, os.path.getsize(path), s.get("codec_name", "?"),
                           int(s.get("width", 0)), int(s.get("height", 0)), frames, fps, requested_fps, rotation)


def remux_to_mp4(src: str, dst: str, rotate_cw: int = 0, mode: str = "metadata") -> None:
    """MPEG-TS → MP4. Con `rotate_cw` (90/180/270, sentido horario) marca la orientación en el MP4
    sin recodificar (mode="metadata") o gira los píxeles recodificando (mode="reencode")."""
    if not shutil.which("ffmpeg"):
        raise CameraError("ffmpeg no está instalado (apt install ffmpeg)")
    tmp = dst + ".tmp.mp4"
    rotate_cw %= 360
    cmd = ["ffmpeg", "-y", "-v", "error", "-fflags", "+genpts"]
    if rotate_cw and mode == "metadata":
        # -display_rotation mide en sentido antihorario: 90° horario = -90.
        cmd += ["-display_rotation:v:0", str(-rotate_cw), "-i", src, "-c", "copy"]
    elif rotate_cw:
        vf = {90: "transpose=clock", 180: "hflip,vflip", 270: "transpose=cclock"}[rotate_cw]
        cmd += ["-i", src, "-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-an"]
    else:
        cmd += ["-i", src, "-c", "copy"]
    proc = subprocess.run(cmd + ["-movflags", "+faststart", tmp], capture_output=True, text=True,
                          timeout=7200 if (rotate_cw and mode != "metadata") else 1800)
    if proc.returncode != 0 or not os.path.isfile(tmp):
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise CameraError(f"ffmpeg no pudo crear el MP4: {proc.stderr.strip()[:300]}")
    os.replace(tmp, dst)


class VideoRecorder:
    def __init__(self, camera: CameraManager, directory: str, profiles: dict, profile: str,
                 min_free_mb: int = 500, max_minutes: float = 30, rotate_mode: str = "metadata"):
        self.camera = camera
        self.rotate_mode = rotate_mode
        self.directory = directory
        self.profiles = profiles
        self.profile = profile
        self.min_free_mb = min_free_mb
        self.max_minutes = max_minutes
        self._lock = threading.Lock()
        self._encoder = None
        self._part_path: str | None = None
        self._final_path: str | None = None
        self._started_at: float | None = None
        self._requested_fps: float | None = None
        self._watchdog: threading.Thread | None = None
        self._stop_evt = threading.Event()
        self.on_auto_stop: Callable[[RecordingResult | None, str], None] | None = None

    @property
    def recording(self) -> bool:
        return self._started_at is not None

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self._started_at if self._started_at else 0.0

    def start(self, profile: str | None = None) -> str:
        with self._lock:
            if self.recording:
                raise CameraError("Ya hay una grabación en curso")
            if not self.camera.available:
                raise CameraError(self.camera.error or "No hay cámara disponible")
            name = profile or self.profile
            prof = self.profiles.get(name)
            if not prof:
                raise CameraError(f"Perfil de vídeo desconocido: {name}")
            free = free_mb(self.directory)
            if free < self.min_free_mb:
                raise CameraError(f"Espacio insuficiente: quedan {free:.0f} MB (mínimo {self.min_free_mb} MB)")
            from picamera2.encoders import H264Encoder
            from picamera2.outputs import PyavOutput

            final = timestamped_path(self.directory, "video", "mp4")
            part = final[:-4] + ".ts.part"
            size, fps = tuple(prof["size"]), float(prof["fps"])
            with self.camera.lock:
                cam = self.camera.open("recording")
                try:
                    config = cam.create_video_configuration(
                        main={"size": size, "format": "YUV420"}, controls={"FrameRate": fps},
                        transform=self.camera.transform(), buffer_count=6)
                    cam.configure(config)
                    encoder = H264Encoder(bitrate=int(prof.get("bitrate", 8_000_000)))
                    output = PyavOutput(part, format="mpegts")
                    cam.start_recording(encoder, output)
                except Exception as exc:
                    self.camera.close()
                    if os.path.exists(part):
                        os.unlink(part)
                    raise CameraError(f"No se pudo iniciar la grabación: {exc}") from exc
            self._encoder = encoder
            self._part_path, self._final_path = part, final
            self._requested_fps = fps
            self._started_at = time.monotonic()
            self._stop_evt.clear()
            self._watchdog = threading.Thread(target=self._watch, name="rec-watchdog", daemon=True)
            self._watchdog.start()
            log.info("Grabación iniciada: %s (%dx%d a %.0f fps, %s)", final, size[0], size[1], fps,
                     type(encoder).__name__)
            return final

    def stop(self) -> RecordingResult:
        """Detiene, finaliza el contenedor, libera la cámara y verifica el MP4."""
        with self._lock:
            if not self.recording:
                raise CameraError("No hay ninguna grabación en curso")
            self._stop_evt.set()
            elapsed = self.elapsed
            part, final, fps = self._part_path, self._final_path, self._requested_fps
            try:
                with self.camera.lock:
                    try:
                        self.camera.picam2.stop_recording()
                    finally:
                        self.camera.close()
            finally:
                self._started_at = None
                self._encoder = None
            log.info("Grabación detenida tras %.1f s; generando MP4…", elapsed)
            return self._finalize(part, final, fps)

    def _finalize(self, part: str, final: str, fps: float | None) -> RecordingResult:
        if not os.path.isfile(part) or os.path.getsize(part) == 0:
            raise CameraError("La grabación no produjo datos")
        t0 = time.monotonic()
        remux_to_mp4(part, final, self.camera.post_rotation_cw, self.rotate_mode)
        result = result_from_probe(final, probe(final), fps)
        if result.duration <= 0 or result.size_bytes < 10_000:
            raise CameraError(f"El vídeo generado parece vacío ({result.describe()})")
        os.unlink(part)
        log.info("Vídeo verificado en %.1f s: %s", time.monotonic() - t0, result.describe())
        return result

    def _watch(self) -> None:
        """Detiene la grabación si se alcanza la duración máxima o se agota el disco."""
        while not self._stop_evt.wait(5):
            reason = None
            if self.elapsed >= self.max_minutes * 60:
                reason = f"se alcanzó la duración máxima de {self.max_minutes:g} min"
            elif free_mb(self.directory) < self.min_free_mb / 2:
                reason = "el disco se está quedando sin espacio"
            if reason:
                log.warning("Parada automática de la grabación: %s", reason)
                result = None
                try:
                    result = self.stop()
                except CameraError as exc:
                    reason += f" ({exc})"
                if self.on_auto_stop:
                    self.on_auto_stop(result, reason)
                return

    def shutdown(self) -> None:
        if self.recording:
            try:
                self.stop()
            except CameraError:
                log.exception("Error al detener la grabación durante el cierre")

    def recover_incomplete(self) -> list[RecordingResult]:
        """Convierte en MP4 las grabaciones interrumpidas (.ts.part)."""
        recovered = []
        for part in sorted(glob.glob(os.path.join(self.directory, "*.ts.part"))):
            final = part[:-len(".ts.part")] + "_recuperado.mp4"
            try:
                recovered.append(self._finalize(part, final, None))
                log.warning("Grabación interrumpida recuperada: %s", final)
            except (CameraError, subprocess.TimeoutExpired) as exc:
                log.error("No se pudo recuperar %s: %s (se conserva el archivo)", part, exc)
        return recovered
