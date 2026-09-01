from __future__ import annotations

from math import gcd

import numpy as np


def normalize_audio(
    waveform: np.ndarray,
    *,
    source_sample_rate: int,
    target_sample_rate: int = 16000,
) -> np.ndarray:
    """Return mono float32 PCM in [-1, 1] at the target sample rate."""
    if source_sample_rate <= 0 or target_sample_rate <= 0:
        raise ValueError("sample rates must be positive")
    source = np.asarray(waveform)
    if source.ndim == 2:
        source = source.astype(np.float64).mean(axis=1)
    elif source.ndim != 1:
        raise ValueError("waveform must have shape (samples,) or (samples, channels)")

    if np.issubdtype(source.dtype, np.integer):
        limits = np.iinfo(source.dtype)
        scale = float(max(abs(limits.min), limits.max))
        mono = source.astype(np.float64) / scale
    else:
        mono = source.astype(np.float64)
    mono = np.nan_to_num(mono, nan=0.0, posinf=1.0, neginf=-1.0)
    mono = np.clip(mono, -1.0, 1.0)

    if source_sample_rate != target_sample_rate and mono.size:
        mono = _resample(mono, source_sample_rate, target_sample_rate)
    return np.clip(mono, -1.0, 1.0).astype(np.float32, copy=False)


def _resample(waveform: np.ndarray, source_rate: int, target_rate: int) -> np.ndarray:
    try:
        from scipy.signal import resample_poly
    except ImportError:
        # Keeps core/tests usable without the audio extra. Production audio installs
        # scipy and uses its anti-aliased polyphase resampler.
        output_length = max(1, round(waveform.size * target_rate / source_rate))
        source_positions = np.arange(waveform.size, dtype=np.float64)
        target_positions = np.linspace(
            0, max(0, waveform.size - 1), output_length, dtype=np.float64
        )
        return np.interp(target_positions, source_positions, waveform)
    divisor = gcd(source_rate, target_rate)
    return resample_poly(
        waveform, target_rate // divisor, source_rate // divisor
    )

