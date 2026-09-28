"""Subprocess fixture: production lock/runtime, entirely fake SDK and telemetry."""
import gc
import importlib.util
import json
from pathlib import Path
import sys
import time
import types

root, lock_path, log_path, mode, phase1a_lock = sys.argv[1:]
sys.path.insert(0, root)


def record(event):
    with open(log_path, "a") as stream:
        stream.write(json.dumps(event) + "\n")


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


if mode == "patrol":
    lock = load("reviewed_lock", phase1a_lock)
    lock.acquire_process_lock(lock_path)
    record(["SDK_INIT"])
    while True:
        time.sleep(.05)

from robot_side import ownership_lock
from robot_side.adapters import g1_robot as runtime
acquire = ownership_lock.acquire_process_lock
ownership_lock.acquire_process_lock = lambda: acquire(lock_path)
# Never import real SDK packages or initialize DDS, even if installed.
channel = types.ModuleType("unitree_sdk2py.core.channel")
channel.ChannelConfigHasInterface = "fake"
channel.ChannelFactoryInitialize = lambda *args: record(["DDS_INIT"])
core = types.ModuleType("unitree_sdk2py.core")
core.channel = channel
loco = types.ModuleType("unitree_sdk2py.g1.loco.g1_loco_client")


class Client:
    def __init__(self):
        record(["SDK_INIT"])

    def SetTimeout(self, value):
        pass

    def Init(self):
        pass

    def GetFsmId(self):
        return 0, 501

    def SetVelocity(self, *args):
        record(["SetVelocity", *args])
        if mode == "exception":
            raise RuntimeError("injected fake SDK failure")
        return 0

    def StopMove(self):
        record(["StopMove"])
        return None


loco.LocoClient = Client
sys.modules["unitree_sdk2py.core"] = core
sys.modules["unitree_sdk2py.core.channel"] = channel
sys.modules["unitree_sdk2py.g1.loco.g1_loco_client"] = loco
runtime.require_runtime = lambda: None
runtime.configure_sdk_path = lambda: None

if mode == "wander":
    client = runtime.UnitreeSdkRuntime().create_loco_client("eth0", 2)
    del client
    gc.collect()  # Lock must survive returned-client GC.
    record(["GC_DONE"])
    while True:
        time.sleep(.05)

script = load("wander_cli", Path(root) / "scripts/g1-wander-reactive-mvp.py")
script.body_writer_conflicts = lambda: []


class Telemetry:
    def __init__(self, *args):
        pass

    def wait_ready(self, timeout):
        return self.latest()

    def latest(self):
        return {"cloud_age_s": 0, "cloud_valid": True, "error": None,
                "obstacle_snapshot": dict.fromkeys(script.run_reactive_mvp.__globals__["SECTORS"], 2)}

    def close(self):
        record(["TELEMETRY_CLOSE"])


script.JsonlReactiveTelemetry = Telemetry
raise SystemExit(script.main([
    "--robot", "g1", "--enable-real-robot", "--execute-real-g1",
    "--i-understand-this-will-move-the-robot", "--require-locomotion-ownership-v1",
    "--duration", "2", "--max-pulses", "1",
]))
