"""Acceso a la cámara CSI (Sony IMX519) mediante Picamera2/libcamera.

CameraManager es el único propietario de la instancia de Picamera2 dentro de Vimo:
- La cámara se abre bajo demanda y se cierra al terminar (no queda encendida en reposo).
- Un lock impide que una foto y una grabación configuren la cámara a la vez; si hay una
  grabación en curso, la foto se toma del flujo de vídeo ya activo (sin reconfigurar).
- Si otro proceso está usando la cámara, libcamera falla al adquirirla y se informa del error.
No se usa OpenCV VideoCapture ni se modifica la configuración del sistema.
"""
from __future__ import annotations

import logging
import os
import threading
import time
import uuid

log = logging.getLogger(__name__)


class CameraError(RuntimeError):
    pass


def timestamped_path(directory: str, prefix: str, ext: str) -> str:
    os.makedirs(directory, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = os.path.join(directory, f"{prefix}_{stamp}.{ext}")
    if os.path.exists(path):
        path = os.path.join(directory, f"{prefix}_{stamp}_{uuid.uuid4().hex[:6]}.{ext}")
    return path


class CameraManager:
    def __init__(self, photo_dir: str, photo_resolution: tuple[int, int] = (2328, 1748), rotation: int = 0,
                 settle_seconds: float = 1.0):
        self.photo_dir = photo_dir
        self.photo_resolution = tuple(photo_resolution)
        self.rotation = rotation
        self.settle_seconds = settle_seconds
        self._lock = threading.RLock()
        self._picam2 = None
        self.mode: str = "closed"  # closed | photo | recording
        self.info: dict | None = None
        self.error: str | None = None

    # Detección -------------------------------------------------------------------------
    def detect(self) -> dict | None:
        """Información de la cámara sin abrirla. None si no hay cámara o falta Picamera2."""
        try:
            from picamera2 import Picamera2
        except ImportError as exc:
            self.error = f"Picamera2 no está disponible: {exc}"
            log.warning(self.error)
            return None
        try:
            cams = Picamera2.global_camera_info()
        except Exception as exc:
            self.error = f"libcamera no pudo enumerar cámaras: {exc}"
            log.warning(self.error)
            return None
        if not cams:
            self.error = "No se detecta ninguna cámara"
            log.warning(self.error)
            return None
        self.info = cams[0]
        self.error = None
        log.info("Cámara detectada: %s (%s)", self.info.get("Model"), self.info.get("Id"))
        return self.info

    @property
    def available(self) -> bool:
        return self.info is not None

    # camera.rotation = giro total en sentido horario (0, 90, 180, 270). El procesador de imagen de la
    # Raspberry Pi solo voltea (180°); un Transform(transpose) se ignora en silencio (comprobado con la
    # IMX519: se aplica «identity»). Por eso el giro de 90° se hace después de capturar.
    def transform(self):
        from libcamera import Transform

        return Transform(hflip=1, vflip=1) if self.rotation in (180, 270) else Transform()

    @property
    def post_rotation_cw(self) -> int:
        """Grados (0 o 90, sentido horario) que hay que girar después de capturar."""
        return 90 if self.rotation in (90, 270) else 0

    def _rotate_photo(self, path: str) -> None:
        if not self.post_rotation_cw:
            return
        from PIL import Image

        with Image.open(path) as im:
            exif = im.info.get("exif")
            rotated = im.rotate(-self.post_rotation_cw, expand=True)
        tmp = path + ".tmp.jpg"
        rotated.save(tmp, "JPEG", quality=92, **({"exif": exif} if exif else {}))
        os.replace(tmp, path)

    # Ciclo de vida -----------------------------------------------------------------------
    def open(self, mode: str):
        """Abre la cámara para `mode` ('photo' o 'recording'). Debe llamarse con el lock tomado."""
        if self._picam2 is not None:
            raise CameraError("La cámara ya está en uso")
        from picamera2 import Picamera2

        try:
            self._picam2 = Picamera2(0)
        except Exception as exc:
            raise CameraError(f"No se pudo abrir la cámara (¿la usa otro programa?): {exc}") from exc
        self.mode = mode
        return self._picam2

    def close(self) -> None:
        with self._lock:
            cam, self._picam2 = self._picam2, None
            self.mode = "closed"
            if cam is None:
                return
            for step in (cam.stop, cam.close):
                try:
                    step()
                except Exception:
                    log.debug("Error al cerrar la cámara", exc_info=True)

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    @property
    def picam2(self):
        return self._picam2

    # Fotografía ------------------------------------------------------------------------
    def take_photo(self) -> str:
        if not self.available:
            raise CameraError(self.error or "No hay cámara disponible")
        path = timestamped_path(self.photo_dir, "foto", "jpg")
        t0 = time.monotonic()
        with self._lock:
            if self.mode == "recording":
                # Foto desde el flujo de vídeo activo (resolución del vídeo).
                try:
                    self._picam2.capture_file(path)
                except Exception as exc:
                    raise CameraError(f"No se pudo capturar la foto durante la grabación: {exc}") from exc
            else:
                cam = self.open("photo")
                try:
                    config = cam.create_still_configuration(main={"size": self.photo_resolution},
                                                            transform=self.transform())
                    cam.configure(config)
                    cam.start()
                    time.sleep(self.settle_seconds)  # exposición y balance de blancos automáticos
                    cam.capture_file(path)
                except CameraError:
                    raise
                except Exception as exc:
                    raise CameraError(f"Error al tomar la foto: {exc}") from exc
                finally:
                    self.close()
        if not os.path.isfile(path) or os.path.getsize(path) < 1024:
            raise CameraError("La foto no se guardó correctamente")
        try:
            self._rotate_photo(path)
        except OSError as exc:
            log.warning("No se pudo girar la foto %s: %s", path, exc)
        log.info("Foto guardada en %s (%.1f s, %d KB)", path, time.monotonic() - t0, os.path.getsize(path) // 1024)
        return path
