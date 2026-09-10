import os
import subprocess
import sys
import wave

import pytest

from g1_bottle_reaction.adapters.cached_audio import G1SshAudioOutput

pytestmark = pytest.mark.skipif(os.name != "posix", reason="G1 SSH playback is Linux-only")


def fake_server(monkeypatch, code):
    real_popen = subprocess.Popen
    commands = []
    def spawn(command, **kwargs):
        commands.append(command)
        return real_popen([sys.executable, "-u", "-c", code], **kwargs)
    monkeypatch.setattr(subprocess, "Popen", spawn)
    return commands


def test_existing_wav_player_uses_one_ssh_connection(monkeypatch, tmp_path):
    commands = fake_server(monkeypatch,
        'import json, sys, base64\n'
        'print(json.dumps({"status":"ready","volume":{"volume":85}}),flush=True)\n'
        'for line in sys.stdin:\n'
        ' r=json.loads(line)\n'
        ' assert r["operation"] in ("PlayStream","PlayStop")\n'
        ' if r["operation"]=="PlayStream": assert len(base64.b64decode(r["pcm"]))==320\n'
        ' print(json.dumps({"status":"result","result":0}),flush=True)\n')
    wav = tmp_path / 'voice.wav'
    with wave.open(str(wav), 'wb') as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(16000)
        stream.writeframes(b'\0\0' * 160)
    output = G1SshAudioOutput('g1', '/tmp/test-control')
    output.chunk_delay_seconds = 0
    try:
        output.play_wav(wav)
        pid = output.process.pid
        output.play_wav(wav)
        assert len(commands) == 1 and output.process.pid == pid
        assert output.process.poll() is None
        assert 'BatchMode=yes' in commands[0] and '/tmp/test-control' in commands[0]
        assert output.volume is None
    finally:
        output.close()
    assert output.process.poll() == 0


def test_ssh_startup_error_is_reported(monkeypatch):
    fake_server(monkeypatch,
        'import json\nprint(json.dumps({"status":"error","error":"speaker missing"}),flush=True)\n')
    output = G1SshAudioOutput('g1')
    try:
        with pytest.raises(RuntimeError, match='speaker missing'):
            output.initialize()
    finally:
        output.close()


def test_close_before_start_never_spawns_ssh(monkeypatch):
    commands = fake_server(monkeypatch, '')
    output = G1SshAudioOutput('g1')
    output.close()
    with pytest.raises(RuntimeError, match='closed'):
        output.initialize()
    assert not commands
