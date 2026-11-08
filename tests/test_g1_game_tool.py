from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name, relative):
    spec = importlib.util.spec_from_file_location(name, ROOT / relative)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


game = load("g1_game", "scripts/g1_game.py")
demo = load("run_integrated_demo", "scripts/run_integrated_demo.py")
REF = [0.] * 29
STANDING = {"motor_error": False, "temps": [50] * 14, "tilt": .05, "gyro": .02, "all_q": [0.] * 29}


def levels(**changes):
    return {name: level for name, level, _ in game.evaluate_state({**STANDING, **changes}, REF)}


def test_standing_robot_passes():
    assert set(levels().values()) == {game.OK}


@pytest.mark.parametrize("changes,name,level", [
    ({"temps": [65] * 14}, "arm temperature", game.FAIL),
    ({"temps": [60] * 14}, "arm temperature", game.WARN),
    ({"motor_error": True}, "motors", game.FAIL),
    ({"tilt": .21}, "tilt", game.FAIL),
    ({"gyro": .51}, "gyro", game.FAIL),
    ({"gyro": .2}, "gyro", game.WARN),
    ({"all_q": [0.] * 12 + [.4] * 17}, "upper-body pose", game.FAIL),
    ({"all_q": [.9] * 12 + [0.] * 17}, "upper-body pose", game.OK),
])
def test_start_limits(changes, name, level):
    assert levels(**changes)[name] == level


def test_tailscale_route_flags_relay_and_absence():
    host = "100.78.135.14"
    assert game.tailscale_route(f'{host} g1 a linux active; direct 1.2.3.4:5', host)[0] == game.OK
    assert game.tailscale_route(f'{host} g1 a linux active; relay "tok"', host)[0] == game.WARN
    assert game.tailscale_route("", host)[0] == game.FAIL


def test_run_plan_defaults_to_three_laps_and_times_a_game():
    assert game.run_plan(None, None)[0] == 3
    assert game.run_plan(5, None)[:2] == (5, 1800.)
    assert game.run_plan(None, 5)[:2] == (game.MINUTES_MAX_LAPS, 300.)
    for bad in ((0, None), (101, None), (None, 0), (None, 31)):
        with pytest.raises(ValueError):
            game.run_plan(*bad)


def test_walking_needs_the_exact_word_or_yes():
    assert game.confirmed("3 laps", True, read=lambda _: pytest.fail("asked"))
    assert game.confirmed("3 laps", False, read=lambda _: "WALK")
    for answer in ("", "walk", "y", "yes"):
        assert not game.confirmed("3 laps", False, read=lambda _: answer)
    def closed(_):
        raise EOFError
    assert not game.confirmed("3 laps", False, read=closed)


def test_supervisor_walks_only_in_real_mode():
    assert "--real-patrol" not in game.supervisor_args(["--no-locomotion"], 30.)
    command = game.supervisor_args(["--real-patrol", "--operator-approved-real-patrol"], 300., 3)
    assert command[command.index("--laps") + 1] == "3"
    assert command[command.index("--patrol-host") + 1] == game.LAN_HOST


def test_relays_use_the_lan_and_the_hackathon_profile():
    commands = game.relay_commands("192.168.0.9")
    loco = commands["locomotion_relay"]
    assert loco[loco.index("--bind") + 1] == game.LAN_HOST
    assert loco[loco.index("--telemetry-host") + 1] == "192.168.0.9"
    assert "--allow-reverse" not in loco
    assert all("--hackathon-runtime" in args for args in commands.values())
    assert game.RELAY_MARKER in game.relay_start_script("192.168.0.9")


def test_worker_loads_every_game_reaction_and_found_for_readiness():
    loaded = game.WORKER_REACTIONS.split(",")
    assert "found" in loaded and set(game.GAME_REACTIONS) <= set(loaded) and "surprise" in loaded


def test_patrol_laps_and_lan_host_reach_the_command(monkeypatch):
    routes = []
    monkeypatch.setattr(demo, "route_to", lambda host: (routes.append(host), ("wlan0", "192.168.0.9"))[1])
    args = demo.parser().parse_args(["--real-patrol", "--operator-approved-real-patrol",
                                     "--laps", "3", "--patrol-host", "192.168.0.82",
                                     "--ssh-target", "unitree@100.78.135.14"])
    command = demo.patrol_command(args)
    assert routes == ["192.168.0.82"]
    assert command[command.index("--loops") + 1] == "3"
    assert command[command.index("--locomotion-relay-host") + 1] == "192.168.0.82"


def test_stand_drops_only_the_patrol_interlock(monkeypatch):
    real = ["python", "app.py", "--enable-real-robot", "--confirm-site-ready",
            "--patrol-control-socket", "/tmp/s.sock", "--patrol-pause-timeout", "30",
            "--hackathon-runtime", "--headless"]
    monkeypatch.setitem(sys.modules, "run_integrated_demo", demo)
    monkeypatch.setattr(demo, "reaction_command", lambda _args: real)
    assert game.stand_command(90) == ["python", "app.py", "--enable-real-robot",
                                      "--confirm-site-ready", "--headless"]


def test_unknown_motion_is_refused_before_contacting_g1(capsys):
    assert game.motion("rm -rf /", assume_yes=True) == 1
    assert "unknown motion" in capsys.readouterr().out


def test_catalog_lists_each_target_with_paired_voices():
    catalog = game.reaction_catalog()
    person = {entry["motion"]: entry["voices"] for entry in catalog["pools"]["person"]}
    assert "salute" in person and all(v.startswith("person/person_salute_") for v in person["salute"])
    assert {"comical_dance", "tekitou_dance"} <= set(catalog["unused"])


def test_voice_must_be_a_reaction_wav_inside_the_voice_folder():
    assert game.voice_path("person/person_salute_1.wav").name == "person_salute_1.wav"
    for bad in ("../../README.md", "person/../../x.wav", "person/missing.wav"):
        with pytest.raises(ValueError):
            game.voice_path(bad)
