"""Push-to-talk microphone capture: 16 kHz mono PCM16, saved as WAV.

A GNOME custom shortcut runs ``cairn note``. Press it once to start recording and
again to stop (the second invocation signals the first through a PID file); in a
terminal, Enter also stops it. Recording stops by itself after ``MAX_SECONDS``.
Needs ``sounddevice`` (and PortAudio: ``sudo apt install libportaudio2``).
"""

import io
import os
import signal
import threading
import wave
from pathlib import Path

RATE = 16000
MAX_SECONDS = 120


def to_wav(pcm: bytes, rate: int = RATE) -> bytes:
    """Wrap mono PCM16 samples in a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def record(stop: threading.Event, max_seconds: float = MAX_SECONDS, rate: int = RATE) -> bytes:
    """Record from the default microphone until ``stop`` is set (or the time cap)."""
    try:
        import sounddevice as sd
    except (ImportError, OSError) as exc:  # OSError: PortAudio library missing
        raise RuntimeError(
            "voice notes need sounddevice and PortAudio: pip install sounddevice;"
            " sudo apt install libportaudio2"
        ) from exc
    chunks: list[bytes] = []

    def callback(indata, frames, time_info, status):  # noqa: ARG001
        chunks.append(bytes(indata))

    with sd.RawInputStream(samplerate=rate, channels=1, dtype="int16", callback=callback):
        stop.wait(max_seconds)
    return b"".join(chunks)


class Toggle:
    """A PID file that lets a second ``cairn note`` stop the recording of the first."""

    def __init__(self, pid_file: Path):
        self.pid_file = pid_file

    def running_pid(self) -> int | None:
        """PID of a live recorder, or None (stale files are removed)."""
        try:
            pid = int(self.pid_file.read_text().strip())
        except (OSError, ValueError):
            return None
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
        except OSError:
            cmdline = b""
        if pid == os.getpid() or b"cairn" not in cmdline:
            self.pid_file.unlink(missing_ok=True)
            return None
        return pid

    def stop_other(self) -> bool:
        """Signal a running recorder to stop. True if there was one."""
        pid = self.running_pid()
        if pid is None:
            return False
        os.kill(pid, signal.SIGUSR1)
        return True

    def claim(self, stop: threading.Event) -> None:
        """Become the recorder: write the PID file, stop on SIGUSR1/SIGINT/SIGTERM."""
        self.pid_file.parent.mkdir(parents=True, exist_ok=True)
        self.pid_file.write_text(str(os.getpid()))
        for sig in (signal.SIGUSR1, signal.SIGINT, signal.SIGTERM):
            signal.signal(sig, lambda *_: stop.set())

    def release(self) -> None:
        self.pid_file.unlink(missing_ok=True)
