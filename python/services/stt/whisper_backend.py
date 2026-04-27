"""
Whisper backend for the STT service.

Uses `faster-whisper` (CTranslate2-backed) rather than `openai-whisper`:
smaller RAM footprint, faster on both CPU and CUDA, and supports int8
quantization out of the box. The API surface is still load-once,
transcribe-many-audio-arrays.

Audio is accepted as float32 numpy arrays sampled at 16 kHz, mono. The
caller is responsible for resampling if needed (WAV loader in this
package handles it).

Install: `pip install 'lexaire[stt-whisper]'`
Model default: `small` (good quality/latency balance; configurable via
`stt.whisper_model`). For pure-CPU dev, `base` or `tiny` is snappier.
"""

from __future__ import annotations

import logging
import wave
from pathlib import Path
from typing import Optional

import numpy as np

log = logging.getLogger(__name__)


class FasterWhisperBackend:
    def __init__(
        self,
        model_size: str = "small",
        device: str = "auto",
        compute_type: Optional[str] = None,
        language: Optional[str] = None,
    ):
        try:
            from faster_whisper import WhisperModel  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "faster-whisper not installed. "
                "pip install 'lexaire[stt-whisper]' or `pip install faster-whisper`."
            ) from e

        # Sensible defaults per device. CUDA -> float16 (fast + accurate);
        # CPU -> int8 (RAM-friendly and still usable on a laptop).
        if compute_type is None:
            if device == "cpu":
                compute_type = "int8"
            elif device and device.startswith("cuda"):
                compute_type = "float16"
            else:
                compute_type = "auto"

        self._model = WhisperModel(model_size, device=device, compute_type=compute_type)
        self._language = language or None
        log.info(
            "FasterWhisperBackend ready  model=%s device=%s compute=%s",
            model_size, device, compute_type,
        )

    def transcribe(self, audio_f32_mono_16k: np.ndarray) -> str:
        """Transcribe a float32 mono 16 kHz numpy array. Returns empty string
        on silence / no speech."""
        if audio_f32_mono_16k.size == 0:
            return ""
        segments, _info = self._model.transcribe(
            audio_f32_mono_16k,
            language=self._language,
            vad_filter=True,
        )
        # `segments` is a generator — materialize and join.
        text = " ".join(s.text.strip() for s in segments).strip()
        return text


def load_wav_as_f32_mono_16k(path: str | Path) -> np.ndarray:
    """Load a WAV file and return a float32 mono array at 16 kHz.

    Uses the stdlib `wave` module — no extra deps. Handles 8/16/32-bit PCM
    (24-bit is rejected with ValueError) and basic channel collapsing.
    Resampling is linear (rough but fine for whisper; swap in
    scipy.signal.resample_poly if you need better)."""
    path = Path(path)
    with wave.open(str(path), "rb") as wf:
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        framerate = wf.getframerate()
        n_frames  = wf.getnframes()
        raw = wf.readframes(n_frames)

    if sampwidth == 2:
        arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    elif sampwidth == 4:
        arr = np.frombuffer(raw, dtype=np.int32).astype(np.float32) / 2147483648.0
    elif sampwidth == 1:
        arr = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ValueError(f"unsupported WAV sample width: {sampwidth} bytes")

    if n_channels > 1:
        arr = arr.reshape(-1, n_channels).mean(axis=1)

    if framerate != 16_000:
        # Linear-interp resample to whisper's expected rate.
        n_out = int(round(arr.size * 16_000 / framerate))
        x_old = np.linspace(0.0, 1.0, arr.size, endpoint=False)
        x_new = np.linspace(0.0, 1.0, n_out, endpoint=False)
        arr = np.interp(x_new, x_old, arr).astype(np.float32)

    return arr
