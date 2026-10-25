"""Compara el modelo de embeddings de openWakeWord con y sin KleidiAI.
Si la diferencia es ~0, tu onnxruntime no tiene el bug y no necesitas el parche."""
import os
import numpy as np
import onnxruntime as ort
import openwakeword

carpeta = os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models")
mel_path = os.path.join(carpeta, "melspectrogram.onnx")
emb_path = os.path.join(carpeta, "embedding_model.onnx")
if not (os.path.exists(mel_path) and os.path.exists(emb_path)):
    raise SystemExit("Faltan los .onnx de features; corre antes probar_hey_vimo.py una vez.")


def sesion(path, sin_kleidiai):
    opciones = ort.SessionOptions()
    if sin_kleidiai:
        opciones.add_session_config_entry("mlas.disable_kleidiai", "1")
    return ort.InferenceSession(path, sess_options=opciones, providers=["CPUExecutionProvider"])


rng = np.random.default_rng(0)
audio = (rng.standard_normal((1, 16000 * 2)) * 3000).astype(np.float32)
mel = sesion(mel_path, True).run(None, {"input": audio})[0].squeeze()
mel = mel / 10 + 2  # mismo escalado que usa openWakeWord
ventana = mel[None, :76, :, None].astype(np.float32)

con = sesion(emb_path, False).run(None, {"input_1": ventana})[0]
sin = sesion(emb_path, True).run(None, {"input_1": ventana})[0]
dif = float(np.max(np.abs(con - sin)))
print(f"onnxruntime {ort.__version__}  |  diferencia máxima: {dif:.6f}")
print("✅ Sin bug: no necesitas el parche." if dif < 1e-3 else
      "⚠️  Hay diferencia: usa el parche de probar_hey_vimo.py en tu agente.")
