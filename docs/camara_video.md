# Fotos y grabación de vídeo (IMX519)

Cámara: Sony IMX519 por CSI, detectada como `imx519` en
`/base/axi/pcie@1000120000/rp1/i2c@80000/imx519@1a` (libcamera 0.7.0+rpt20260205, Picamera2 0.3.34).
Se usa **Picamera2**, no OpenCV `VideoCapture`, y no se cambian drivers ni configuración del sistema.

## Reglas

- Solo se activa con una **acción explícita**: botones **Foto** / **Grabar**, u órdenes de voz
  claras («toma una foto», «graba un vídeo», «detén la grabación»). Claude Code no puede
  controlarla. La wake word nunca hace fotos.
- `CameraManager` es el único propietario de Picamera2 dentro de Vimo, protegido por un lock. La
  cámara se abre bajo demanda y se cierra al terminar. Si otro programa la usa, se informa del error.
- No se graba audio. Al cerrar Vimo se detiene la grabación en curso y se libera la cámara.

## Fotos

- Resolución `camera.photo_resolution` (por defecto 2328×1748, modo completo del sensor a 30 fps).
  Hay ~1 s de espera para la exposición y el balance de blancos automáticos.
- Durante una grabación, la foto se toma del flujo de vídeo (resolución del vídeo) sin
  reconfigurar la cámara.
- Destino: `photos/foto_AAAAMMDD-HHMMSS.jpg`, con un sufijo único si coincide el segundo.
- `camera.rotation`: giro total en sentido horario: 0, 90, 180 o 270. **En esta Raspberry Pi es 270**
  porque la cámara está montada **de lado**: fotos y vídeos salen verticales.
  - La parte de 180° la hace la cámara (volteo horizontal + vertical de libcamera).
  - La parte de 90° se hace **después de capturar**: el procesador de imagen de la Pi solo sabe
    voltear, y un `Transform(transpose)` se ignora en silencio (comprobado: se aplica «identity»).
    En las **fotos** se giran los píxeles. En los **vídeos**, según `video.rotate_mode`:
    `metadata` (por defecto) marca la orientación en el MP4 sin recodificar, al instante y sin
    pérdida (lo respetan VLC, mpv, navegadores y móviles); `reencode` gira los píxeles con ffmpeg
    (compatible con cualquier programa, pero lento y con mucha CPU).
  - Historial: el 21-10 se configuró 180 creyendo que estaba boca abajo (escena oscura, sin
    referencias). El 22-10, con una persona en el plano, se vio que estaba girada 90°; se corrigió
    a 270 y se reparó sin pérdida el vídeo ya grabado (original en `data/respaldo_videos/`).

## Vídeo

Perfiles (`video.profiles`, elegible en Ajustes):

| Perfil | Tamaño | FPS solicitados | Bitrate |
|---|---|---|---|
| `720p30` | 1280×720 | 30 | 4 Mb/s |
| `1080p30` (defecto) | 1920×1080 (se ve 1080×1920 con la cámara de lado) | 30 | 8 Mb/s |
| `4k` (avanzado) | 3840×2160 | 15 | 20 Mb/s |

Los FPS que anuncia el sensor (p. ej. 1080p a 60) **no** garantizan los FPS del vídeo codificado.
La Raspberry Pi 5 **no tiene codificador H.264 por hardware**: Picamera2 usa `LibavH264Encoder`
(software) y consume CPU. El 4K a 15 fps es especialmente exigente.

Proceso:

1. Comprueba que no hay otra grabación y que quedan al menos `video.min_free_mb` (500 MB).
2. Graba en **MPEG-TS** (`recordings/video_….ts.part`), un formato que sigue siendo legible aunque
   el proceso muera.
3. Al detener: finaliza el codificador, libera la cámara y crea el **MP4 real** con
   `ffmpeg -c copy -movflags +faststart` (sin recodificar; no es un cambio de extensión).
4. Verifica el MP4 con `ffprobe`: códec, resolución, duración, número de fotogramas y FPS medidos.
   Solo se da por buena si la duración es mayor que 0 y el archivo pesa más de 10 KB. La GUI
   muestra la ruta y los datos.
5. Parada automática al llegar a `video.max_minutes` (30) o si el espacio libre cae por debajo de la
   mitad del mínimo.

Durante la grabación la GUI muestra `● REC mm:ss` y el botón **Detener**. La conversación sigue
funcionando con normalidad.

### Recuperación

Si Vimo termina de forma inesperada (corte de luz, `kill -9`), al siguiente arranque convierte
los `*.ts.part` en `*_recuperado.mp4`. Se conserva lo escrito hasta el último paquete; lo que
estaba en los búferes de memoria se pierde (normalmente menos de un segundo). Si no se puede
recuperar, el `.ts.part` se conserva para intentarlo a mano.

## Estado de la verificación

- ✓ Detección de la cámara desde Vimo (`main.py --check`).
- ✓ APIs verificadas en el código de Picamera2 instalado: `H264Encoder` = `LibavH264Encoder` en la
  Pi 5, `PyavOutput(..., format=...)`, guardado JPEG desde YUV420 con simplejpeg.
- ✓ Grabación real 720p30 con `tools/probar.py video 3`: MP4 H.264 verificado con ffprobe
  (2,6 s, 1280×720, 30,0 fps, 78 fotogramas), con la imagen girada 180°.
- ✓ Pruebas manuales del usuario (`tools/probar.py foto` y `video`) superadas, según el propio
  usuario (22-10-2026).

Prueba manual desde Python:

```bash
venv/bin/python - <<'EOF'
from app.camera_manager import CameraManager
from app.video_recorder import VideoRecorder
from app.config_manager import ConfigManager
import time
c = ConfigManager(); v = c.section("video")
cam = CameraManager("photos"); cam.detect()
print(cam.take_photo())
rec = VideoRecorder(cam, "recordings", v["profiles"], "720p30")
print(rec.start()); time.sleep(5); print(rec.stop().describe())
EOF
```
