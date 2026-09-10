"""Bounded WAV subprocess playback using the existing AudioOutput contract."""
from pathlib import Path
import json
import base64
import inspect
import select
import shutil
import shlex
import subprocess
import threading
import sys
import wave

from .aivis_speech import AudioOutput
from .g1_audio import G1AudioOutput


class G1SshAudioOutput(G1AudioOutput):
    """Reuse WAV player, but place its AudioClient on G1 via authenticated SSH."""
    def __init__(self, target, control=None):
        super().__init__("eth0", volume=None, timeout_seconds=1, chunk_bytes=16000,
                         chunk_delay_seconds=.5, runtime=self)
        self.target, self.control = target, control
        self.process = None
        self.closed = False
        self.lock = threading.Lock()

    def create_audio_client(self, network_interface, timeout):
        from .g1_robot import serve_remote_cached_audio
        source = inspect.getsource(serve_remote_cached_audio) + "\nserve_remote_cached_audio()\n"
        command = ["ssh", "-T", "-o", "StrictHostKeyChecking=yes", "-o", "BatchMode=yes",
                   "-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=2", "-o", "ServerAliveCountMax=2"]
        if self.control:
            command += ["-S", self.control]
        command += ["--", self.target, shlex.join(["python3", "-u", "-B", "-c", source])]
        with self.lock:
            if self.closed:
                raise RuntimeError("G1 audio closed")
            self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                            text=True, bufsize=1)
        response = self._response("ready", 18)
        print(f"G1-local audio ready via SSH; volume: {response['volume']}", flush=True)
        return self

    def _response(self, expected, timeout):
        if not select.select([self.process.stdout], [], [], timeout)[0]:
            raise RuntimeError(f"G1 SSH audio timed out waiting for {expected}")
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError("G1 SSH audio connection closed")
        response = json.loads(line)
        if response.get("status") != expected:
            raise RuntimeError(response.get("error", "invalid G1 SSH audio response"))
        return response

    def _call(self, request):
        if self.closed:
            raise RuntimeError("G1 audio closed")
        self.process.stdin.write(json.dumps(request) + "\n")
        self.process.stdin.flush()
        result = self._response("result", 4)["result"]
        return tuple(result) if isinstance(result, list) else result

    def PlayStream(self, app_name, stream_id, pcm):
        return self._call({"operation": "PlayStream", "stream_id": stream_id,
                           "pcm": base64.b64encode(pcm).decode("ascii")})

    def PlayStop(self, app_name):
        return self._call({"operation": "PlayStop"})

    def play_wav(self, path):
        super().play_wav(path)
        print("G1 AUDIO PLAYBACK COMPLETED (G1-local SDK via SSH)", flush=True)

    def close(self):
        with self.lock:
            self.closed = True
            process = self.process
        if process:
            # EOF lets the G1 helper stop its own stream and exit.
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            process.stdout.close()


class SubprocessWavOutput(AudioOutput):
    def __init__(self):
        self.lock = threading.Lock()
        self.process = None
        self.closed = False

    def play_wav(self, path: Path) -> None:
        with wave.open(str(path), "rb") as stream:
            duration = stream.getnframes() / stream.getframerate()
        with self.lock:
            if self.closed:
                return
            process = subprocess.Popen(self.command(path),
                                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.PIPE)
            self.process = process
        try:
            _, error = process.communicate(timeout=duration+10)
            if process.returncode and not self.closed:
                raise RuntimeError(f"WAV player failed: {error.decode(errors='replace')[:500]}")
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()
            raise RuntimeError("WAV player timed out")
        finally:
            with self.lock:
                self.process = None

    def close(self):
        with self.lock:
            self.closed = True
            if self.process is not None and self.process.poll() is None:
                self.process.terminate()

    def command(self, path):
        raise NotImplementedError


class LinuxAplayOutput(SubprocessWavOutput):
    def __init__(self):
        super().__init__()
        self.executable = shutil.which("aplay")
        if not self.executable:
            raise RuntimeError("Existing aplay is required; no system packages were installed")

    def command(self, path):
        return [self.executable, "-q", "--", str(path)]


class G1CachedOutput(AudioOutput):
    """One persistent audio process/connection; all blocking IO is on audio thread."""
    def __init__(self, root, interface=None):
        self.helper = root / "tools/g1_cached_sound.py"
        self.interface = interface
        self.ready = False
        self.closed = False
        command = [sys.executable, "-B", str(self.helper), "--serve"]
        if interface:
            command += ["--interface", interface]
        self.process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        text=True, bufsize=1)

    def _response(self, expected, timeout):
        if not select.select([self.process.stdout], [], [], timeout)[0]:
            raise RuntimeError(f"G1 audio timed out waiting for {expected}")
        line = self.process.stdout.readline()
        if not line:
            raise RuntimeError(f"G1 audio process ended before {expected}")
        if json.loads(line).get("status") != expected:
            raise RuntimeError("Invalid local G1 audio response")

    def play_wav(self, path):
        if self.closed:
            return
        if not self.ready:
            self.prepare()
        with wave.open(str(path), "rb") as stream:
            duration = stream.getnframes()/stream.getframerate()
        self.process.stdin.write(json.dumps({"wav": str(path)}) + "\n")
        self.process.stdin.flush()
        self._response("played", duration+8)
        print("G1 AUDIO PLAYBACK COMPLETED", flush=True)

    def prepare(self):
        if not self.ready:
            self._response("ready", 14)
            self.ready = True

    def close(self):
        self.closed = True
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
