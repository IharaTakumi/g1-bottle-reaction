from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import sys
from typing import Any, Sequence


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
LOCAL_CONFIG_PATH = Path(__file__).with_name("environment.local.json")
DEFAULT_GVHMR_ROOT = REPOSITORY_ROOT.parent / "GVHMR"
DEFAULT_GMR_ROOT = REPOSITORY_ROOT.parent / "GMR"
DEFAULT_WSL_DISTRO = "Ubuntu"

_WINDOWS_ABSOLUTE = re.compile(r"^([A-Za-z]):[\\/](.*)$")


@dataclass(frozen=True)
class PythonRuntime:
    """A native Python executable or a Python executable inside WSL."""

    executable: str
    wsl_distribution: str | None = None

    @classmethod
    def parse(cls, value: str) -> "PythonRuntime":
        if value.startswith("wsl:"):
            parts = value.split(":", 2)
            if len(parts) != 3 or not parts[1] or not parts[2]:
                raise ValueError(
                    "WSL Python must use wsl:<distribution>:/absolute/python format"
                )
            return cls(parts[2], parts[1])
        return cls(value)

    @property
    def is_wsl(self) -> bool:
        return self.wsl_distribution is not None

    def path(self, value: str | Path) -> str:
        text = str(value)
        if not self.is_wsl:
            return text
        if text.startswith("/"):
            return text
        match = _WINDOWS_ABSOLUTE.match(text)
        if not match:
            raise ValueError(f"Cannot translate path to WSL: {text}")
        drive, remainder = match.groups()
        return f"/mnt/{drive.lower()}/{remainder.replace(chr(92), '/')}"

    def command(
        self,
        script: str | Path,
        arguments: Sequence[str],
        *,
        cwd: str | Path,
    ) -> list[str]:
        if not self.is_wsl:
            return [self.executable, str(script), *arguments]
        return [
            "wsl.exe",
            "-d",
            str(self.wsl_distribution),
            "--cd",
            self.path(cwd),
            "--exec",
            self.executable,
            self.path(script),
            *arguments,
        ]

    def probe_command(self, code: str, *, cwd: str | Path) -> list[str]:
        if not self.is_wsl:
            return [self.executable, "-c", code]
        return [
            "wsl.exe",
            "-d",
            str(self.wsl_distribution),
            "--cd",
            self.path(cwd),
            "--exec",
            self.executable,
            "-c",
            code,
        ]

    def serialize(self) -> str:
        if self.is_wsl:
            return f"wsl:{self.wsl_distribution}:{self.executable}"
        return self.executable


@dataclass(frozen=True)
class ReactionEnvironment:
    gvhmr_root: Path | None
    gmr_root: Path | None
    gvhmr_runtime: PythonRuntime
    gmr_runtime: PythonRuntime
    config_path: Path = LOCAL_CONFIG_PATH


def load_local_config(path: Path = LOCAL_CONFIG_PATH) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid reaction generator config {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Reaction generator config must contain an object: {path}")
    return value


def resolve_environment(path: Path = LOCAL_CONFIG_PATH) -> ReactionEnvironment:
    config = load_local_config(path)
    distro = str(config.get("wsl_distribution", DEFAULT_WSL_DISTRO))

    gvhmr_root = _configured_path(
        os.environ.get("GVHMR_ROOT"), config.get("gvhmr_root"), DEFAULT_GVHMR_ROOT
    )
    gmr_root = _configured_path(
        os.environ.get("GMR_ROOT"), config.get("gmr_root"), DEFAULT_GMR_ROOT
    )

    gvhmr_python = os.environ.get("GVHMR_PYTHON") or config.get("gvhmr_python")
    gmr_python = os.environ.get("GMR_PYTHON") or config.get("gmr_python")
    if config.get("backend") == "wsl":
        if gvhmr_python and not str(gvhmr_python).startswith("wsl:"):
            gvhmr_python = f"wsl:{distro}:{gvhmr_python}"
        if gmr_python and not str(gmr_python).startswith("wsl:"):
            gmr_python = f"wsl:{distro}:{gmr_python}"

    return ReactionEnvironment(
        gvhmr_root=gvhmr_root,
        gmr_root=gmr_root,
        gvhmr_runtime=PythonRuntime.parse(str(gvhmr_python or sys.executable)),
        gmr_runtime=PythonRuntime.parse(str(gmr_python or sys.executable)),
        config_path=path,
    )


def _configured_path(
    environment_value: str | None,
    config_value: object,
    conventional_path: Path,
) -> Path | None:
    if environment_value:
        return Path(environment_value)
    if isinstance(config_value, str) and config_value:
        return Path(config_value)
    if conventional_path.exists():
        return conventional_path
    return None
