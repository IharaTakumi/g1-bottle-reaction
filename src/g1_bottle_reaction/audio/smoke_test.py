from __future__ import annotations

import numpy as np

from g1_bottle_reaction.config.loader import load_config

from .classifiers import YamnetClassifier


def main() -> int:
    config = load_config()
    classifier = YamnetClassifier(
        model_url=config.audio.yamnet.model_url,
        cache_dir=config.audio.yamnet.cache_dir,
        music_labels=config.audio.yamnet.music_labels,
        top_n=config.audio.yamnet.top_n,
    )
    silence = np.zeros(config.audio.target_sample_rate, dtype=np.float32)
    result = classifier.classify(
        silence, sample_rate=config.audio.target_sample_rate
    )
    if classifier.class_count != 521:
        raise RuntimeError(f"Expected 521 YAMNet classes, got {classifier.class_count}")
    print(
        f"YAMNET_OK classes={classifier.class_count} "
        f"music_score={result.music_score:.6f}"
    )
    for prediction in result.top_predictions:
        print(f"{prediction.label}: {prediction.score:.6f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

