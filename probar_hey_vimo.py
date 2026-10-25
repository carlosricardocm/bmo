#!/usr/bin/env python3
"""Prueba el modelo "hey vimo" con openWakeWord en la Raspberry Pi.

Uso:
  python probar_hey_vimo.py --modelo hey_vimo.onnx --umbral 0.6 --wav positivos/*.wav
  python probar_hey_vimo.py --modelo hey_vimo.onnx --umbral 0.6 --mic

Los WAV deben ser mono a 16 kHz, por ejemplo:
  arecord -D plughw:1,0 -f S16_LE -r 16000 -c 1 -d 3 positivos/ana_01.wav
"""
import argparse
import os
import time

import numpy as np
import onnxruntime as ort

# En ARM64 (como la Raspberry Pi 5), onnxruntime 1.27 tiene un bug en sus kernels KleidiAI
# que hace que el modelo de embeddings dé resultados erróneos y todos los puntajes salgan ~0.
# Desactivarlos es inofensivo en versiones que no tienen el problema (ignoran la opción).
_SessionOptionsOriginal = ort.SessionOptions


def _session_options_sin_kleidiai(*args, **kwargs):
    opciones = _SessionOptionsOriginal(*args, **kwargs)
    try:
        opciones.add_session_config_entry("mlas.disable_kleidiai", "1")
    except Exception:
        pass
    return opciones


ort.SessionOptions = _session_options_sin_kleidiai

import openwakeword
from openwakeword.model import Model
from openwakeword.utils import download_file

FRAME = 1280  # 80 ms a 16 kHz, lo que espera openWakeWord


def asegurar_modelos_de_features(framework):
    """openWakeWord necesita mel + embedding en el mismo formato que el modelo.

    Si en la Pi solo se descargaron alguna vez los .tflite, los .onnx no existen,
    y download_models() no los baja porque ve el .tflite ya presente.
    """
    carpeta = os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models")
    os.makedirs(carpeta, exist_ok=True)
    for info in openwakeword.FEATURE_MODELS.values():
        url = info["download_url"]
        if framework == "onnx":
            url = url.replace(".tflite", ".onnx")
        if not os.path.exists(os.path.join(carpeta, url.split("/")[-1])):
            print(f"Descargando {url.split('/')[-1]} …")
            download_file(url, carpeta)


def cargar(ruta_modelo):
    framework = "tflite" if ruta_modelo.endswith(".tflite") else "onnx"
    asegurar_modelos_de_features(framework)
    modelo = Model(wakeword_models=[ruta_modelo], inference_framework=framework)
    nombre = list(modelo.models.keys())[0]
    print(f"Modelo '{nombre}' cargado con {framework}")
    return modelo, nombre


def probar_wavs(modelo, nombre, rutas, umbral):
    import soundfile as sf

    arriba = 0
    for ruta in rutas:
        audio, sr = sf.read(ruta, dtype="int16")
        if sr != 16000 or audio.ndim != 1:
            print(f"{ruta}: saltado (debe ser mono a 16 kHz)")
            continue
        modelo.reset()
        pico = max(p[nombre] for p in modelo.predict_clip(audio, padding=1))
        marca = "✅" if pico >= umbral else "  "
        arriba += pico >= umbral
        print(f"{marca} {pico:.2f}  {ruta}")
    print(f"\n{arriba}/{len(rutas)} archivos superan el umbral {umbral}")


def probar_mic(modelo, nombre, umbral, debounce=2.0):
    import pyaudio

    pa = pyaudio.PyAudio()
    stream = pa.open(format=pyaudio.paInt16, channels=1, rate=16000,
                     input=True, frames_per_buffer=FRAME)
    print(f"Escuchando… di '{nombre.replace('_', ' ')}' (Ctrl+C para salir)")
    ultimo = 0.0
    try:
        while True:
            audio = np.frombuffer(stream.read(FRAME, exception_on_overflow=False), dtype=np.int16)
            score = modelo.predict(audio)[nombre]
            if score >= umbral and time.monotonic() - ultimo > debounce:
                ultimo = time.monotonic()
                print(f"{time.strftime('%H:%M:%S')}  ¡Detectado! ({score:.2f})")
                modelo.reset()  # limpia el buffer para no volver a dispararse con el mismo audio
    except KeyboardInterrupt:
        pass
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--modelo", required=True, help="hey_vimo.onnx o hey_vimo.tflite")
    ap.add_argument("--umbral", type=float, default=0.5, help="usa el de LEEME.txt")
    ap.add_argument("--wav", nargs="*", help="archivos WAV mono 16 kHz")
    ap.add_argument("--mic", action="store_true", help="prueba en vivo con el micrófono")
    args = ap.parse_args()

    modelo, nombre = cargar(args.modelo)
    if args.wav:
        probar_wavs(modelo, nombre, args.wav, args.umbral)
    if args.mic:
        probar_mic(modelo, nombre, args.umbral)
    if not args.wav and not args.mic:
        ap.error("indica --wav archivos… o --mic")
