from __future__ import annotations

import numpy as np

from g1_bottle_reaction.audio.normalization import normalize_audio


def test_stereo_integer_audio_becomes_mono_float32_in_range() -> None:
    stereo = np.array(
        [[32767, 32767], [-32768, -32768], [1000, -1000]], dtype=np.int16
    )
    result = normalize_audio(
        stereo, source_sample_rate=16000, target_sample_rate=16000
    )
    assert result.shape == (3,)
    assert result.dtype == np.float32
    assert float(result.min()) >= -1.0
    assert float(result.max()) <= 1.0
    assert result[2] == 0.0


def test_float_audio_is_clipped_and_non_finite_values_are_sanitized() -> None:
    result = normalize_audio(
        np.array([-2.0, np.nan, np.inf, 2.0], dtype=np.float64),
        source_sample_rate=16000,
    )
    np.testing.assert_array_equal(
        result, np.array([-1.0, 0.0, 1.0, 1.0], dtype=np.float32)
    )


def test_sample_rate_conversion_contract() -> None:
    waveform = np.linspace(-0.5, 0.5, 48000, dtype=np.float32)
    result = normalize_audio(
        waveform, source_sample_rate=48000, target_sample_rate=16000
    )
    assert result.shape == (16000,)
    assert result.dtype == np.float32

