"""Orientación de la cámara: 180° en la cámara, 90° después de capturar (fotos y vídeo)."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.camera_manager import CameraManager  # noqa: E402
from app.video_recorder import probe, remux_to_mp4, result_from_probe  # noqa: E402


class RotationMappingTest(unittest.TestCase):
    def test_split_between_camera_and_post(self):
        for rotation, flips, post in [(0, False, 0), (90, False, 90), (180, True, 0), (270, True, 90)]:
            cam = CameraManager("/tmp", rotation=rotation)
            tr = cam.transform()
            self.assertEqual(bool(tr.hflip and tr.vflip), flips, rotation)
            self.assertEqual(cam.post_rotation_cw, post, rotation)

    def test_photo_rotated_with_pixels(self):
        from PIL import Image
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "f.jpg")
            img = Image.new("RGB", (400, 300), "white")
            img.paste((255, 0, 0), (0, 0, 40, 40))          # marca roja arriba a la izquierda
            img.save(path)
            CameraManager(d, rotation=270)._rotate_photo(path)
            out = Image.open(path).convert("RGB")
            self.assertEqual(out.size, (300, 400))
            r, g, b = out.getpixel((280, 20))               # 90° horario: arriba a la derecha
            self.assertTrue(r > 200 and g < 80 and b < 80)


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "sin ffmpeg")
class RemuxRotationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ts = os.path.join(self.tmp.name, "v.ts")
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=10:duration=1",
                        "-c:v", "libx264", "-preset", "ultrafast", "-f", "mpegts", self.ts], check=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_metadata_rotation_keeps_pixels(self):
        out = os.path.join(self.tmp.name, "m.mp4")
        remux_to_mp4(self.ts, out, 90, "metadata")
        r = result_from_probe(out, probe(out))
        self.assertEqual((r.width, r.height, r.rotation_cw), (320, 240, 90))
        self.assertEqual(r.display_size, (240, 320))
        self.assertIn("girado 90°", r.describe())

    def test_reencode_rotation_changes_pixels(self):
        out = os.path.join(self.tmp.name, "r.mp4")
        remux_to_mp4(self.ts, out, 90, "reencode")
        r = result_from_probe(out, probe(out))
        self.assertEqual((r.width, r.height, r.rotation_cw), (240, 320, 0))

    def test_no_rotation(self):
        out = os.path.join(self.tmp.name, "n.mp4")
        remux_to_mp4(self.ts, out, 0)
        r = result_from_probe(out, probe(out))
        self.assertEqual((r.width, r.height, r.rotation_cw), (320, 240, 0))


if __name__ == "__main__":
    unittest.main()
