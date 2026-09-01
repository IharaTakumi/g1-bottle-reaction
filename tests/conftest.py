from __future__ import annotations

from dataclasses import replace
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from g1_bottle_reaction.config.loader import AppConfig, load_config


@pytest.fixture
def app_config(tmp_path: Path) -> AppConfig:
    config = load_config(PROJECT_ROOT / "config" / "default.yaml")
    return replace(config, event_log=tmp_path / "events.jsonl")

