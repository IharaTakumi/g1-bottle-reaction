#!/usr/bin/env python3
"""Offline JSONL evidence replay. Requires explicit config; never recommends limits."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from patrol.stationary_observer import EvidenceConfig, StationaryEvidenceObserver


def analyze(records, config):
    observer = StationaryEvidenceObserver(config)
    rows = []
    for index, record in enumerate(records, 1):
        try:
            kind = record["type"]
            if kind == "start":
                result = observer.start(record["context"])
            elif kind == "sample":
                result = observer.observe(record["sample"], record["now"])
            elif kind == "evaluate":
                result = observer.evaluate(record["now"], record["context"])
            elif kind == "invalidate":
                result = observer.invalidate(record["reason"])
            else:
                result = observer.invalidate("unknown replay record type")
        except (KeyError, TypeError):
            result = observer.invalidate("malformed replay record")
        rows.append(dict(line=index, **result))
    return dict(evaluations=rows, final=observer.result(),
                invalid_evidence_records=sum(r["state"] == "INVALID_EVIDENCE" for r in rows),
                thresholds_recommended=False, production_authorization=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recording", type=Path)
    parser.add_argument("--config", type=Path, required=True,
                        help="explicit calibration/test EvidenceConfig JSON; no defaults")
    args = parser.parse_args(argv)
    try:
        config = EvidenceConfig(**json.loads(args.config.read_text(encoding="utf-8")))
        records = [json.loads(line) for line in args.recording.read_text(encoding="utf-8").splitlines()
                   if line.strip()]
        print(json.dumps(analyze(records, config), allow_nan=False, indent=2))
    except (OSError, TypeError, ValueError) as exc:
        parser.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
