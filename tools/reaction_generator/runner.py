from __future__ import annotations

from collections import deque
from pathlib import Path
import re
import subprocess
import sys
from typing import Sequence


_MISSING_MODULE = re.compile(r"(?:ModuleNotFoundError|ImportError):.*?'([^']+)'")


class ExternalStageError(RuntimeError):
    pass


def _print_child_output(line: str) -> None:
    """Print UTF-8 child output even when the Windows console uses cp932."""
    try:
        print(line, end="", flush=True)
    except UnicodeEncodeError:
        encoding = sys.stdout.encoding or "utf-8"
        printable = line.encode(encoding, errors="replace").decode(encoding)
        print(printable, end="", flush=True)


def run_external_stage(
    command: Sequence[str],
    *,
    cwd: Path,
    stage: str,
) -> None:
    """Stream normal progress while withholding a child Python traceback."""

    try:
        process = subprocess.Popen(
            list(command),
            cwd=cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except OSError as exc:
        raise ExternalStageError(
            f"Could not start {stage}: {exc}\n\n"
            "Reaction generator prerequisites are incomplete.\n"
            "Run:\npython tools/reaction_generator/doctor.py"
        ) from exc

    assert process.stdout is not None
    recent: deque[str] = deque(maxlen=80)
    hiding_traceback = False
    for line in process.stdout:
        recent.append(line.rstrip())
        if line.startswith("Traceback (most recent call last):"):
            hiding_traceback = True
        if not hiding_traceback:
            _print_child_output(line)
    return_code = process.wait()
    if return_code == 0:
        return

    output = "\n".join(recent)
    missing = _MISSING_MODULE.search(output)
    if missing:
        reason = f"The {stage} environment is missing Python module '{missing.group(1)}'."
    elif "CUDA" in output or "cuda" in output:
        reason = f"{stage} could not use its required CUDA runtime."
    else:
        last_line = next((line for line in reversed(recent) if line.strip()), "")
        reason = f"{stage} exited with code {return_code}."
        if last_line and not last_line.startswith("Traceback"):
            reason += f" Last message: {last_line}"
    raise ExternalStageError(
        f"{reason}\n\nReaction generator prerequisites are incomplete.\n"
        "Run:\npython tools/reaction_generator/doctor.py"
    )
