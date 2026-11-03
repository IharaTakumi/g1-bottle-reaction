#!/usr/bin/env python3
"""Pure validators for the MotionDecode production runtime."""
from __future__ import annotations
import argparse
import json
from pathlib import Path

REQUIRED_PUBLICATIONS = {"rt/api/arm/response", "rt/api/sport/request", "rt/arm/action/state", "rt/arm_sdk"}
REQUIRED_SUBSCRIPTIONS = {"rt/api/arm/request", "rt/api/sport/response", "rt/armsdk", "rt/lf/bmsstate", "rt/lowstate", "rt/sportmodestate"}

def strict_arm_pid(report: dict, source_ip: str) -> int:
    publishers = report.get("topics", {}).get("rt/arm_sdk", {}).get("publishers", [])
    candidates = [item for item in publishers
                  if item.get("hostname") == "Unitree"
                  and item.get("process_name") == "python3"
                  and source_ip in item.get("source_ips", [])
                  and REQUIRED_PUBLICATIONS <= set(item.get("related_publications", []))
                  and REQUIRED_SUBSCRIPTIONS <= set(item.get("related_subscriptions", []))
                  and str(item.get("pid", "")).isdigit() and int(item["pid"]) > 0]
    if len(candidates) != 1:
        raise ValueError(f"ARM_IDENTITY_NOT_UNIQUE candidates={len(candidates)}")
    if report.get("lowstate", {}).get("status") != "PASS":
        raise ValueError("LOWSTATE_NOT_PASS")
    if report.get("lowstate", {}).get("stationary") != "PASS":
        raise ValueError("G1_NOT_STATIONARY")
    action = report.get("topics", {}).get("rt/arm/action/state", {}).get("status_during_observation")
    if action != "IDLE":
        raise ValueError(f"ARM_ACTION_NOT_IDLE status={action}")
    return int(candidates[0]["pid"])

def ready_status(status: dict) -> None:
    checks = {
        "accepted": status.get("accepted") is True,
        "state": status.get("state") == "READY",
        "mode": status.get("mode") == "real",
        "arm_action": status.get("arm_action") == "IDLE",
        "weight": status.get("weight") == 0.0,
        "external_writers": status.get("external_writers") == 0,
        "ownership_safe": status.get("ownership_safe") is True,
        "fault": status.get("fault") is None,
        "preflight": {"found", "surprise"} <= set(status.get("preflight") or {})
        and all((status.get("preflight") or {}).values()),
    }
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ValueError("RESIDENT_NOT_READY " + ",".join(failed))

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=("arm", "status"))
    parser.add_argument("json_file", type=Path)
    parser.add_argument("--source-ip", default="192.168.123.161")
    args = parser.parse_args()
    data = json.loads(args.json_file.read_text())
    try:
        if args.kind == "arm":
            print(strict_arm_pid(data, args.source_ip))
        else:
            ready_status(data)
            print("READY")
    except ValueError as exc:
        parser.exit(3, f"{exc}\n")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
