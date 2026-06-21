from __future__ import annotations

import io
import wave

from xangi_stackchan.app_types import BridgeConfig
from xangi_stackchan.dance import DanceLoop, PRESETS, run_demo, synthesize_text
from xangi_stackchan.stackchan import StackchanConfig


class FakeBackend:
    def __init__(self):
        self.commands = []
        self.wav_calls = []

    def send_command(self, command: str) -> dict:
        self.commands.append(command)
        if command == "STATUS":
            return {"status": "ok"}
        return {"status": "ok", "command": command}

    def send_wav(self, wav: bytes, chunk_size: int = 1024, chunk_delay: float = 0.005) -> dict:
        self.wav_calls.append(
            {"wav": wav, "chunk_size": chunk_size, "chunk_delay": chunk_delay}
        )
        return {"status": "ok", "size": len(wav)}


class FakePiper:
    def synthesize_many(self, chunks: list[str]) -> list[bytes]:
        return [b"RIFFxxxxWAVE" for _ in chunks]


class FixedWavPiper:
    def __init__(self, wav: bytes):
        self._wav = wav

    def synthesize_many(self, chunks: list[str]) -> list[bytes]:
        return [self._wav for _ in chunks]


def _wav_bytes(duration_seconds: float = 1.0, rate: int = 8000) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(rate)
        wav.writeframes(b"\x00\x00" * int(rate * duration_seconds))
    return out.getvalue()


def _config() -> BridgeConfig:
    return BridgeConfig(
        xangi_url="http://127.0.0.1:18888",
        thread_id=None,
        stackchan=StackchanConfig(wifi=False, host="", port="/dev/null", baud=115200),
        volume=128,
        tts="piper",
        piper_bin="",
        piper_model="",
        piper_speaker=0,
        voicevox_url="",
        voicevox_speaker=0,
        serial_chunk=512,
        serial_delay=0.015,
        stackchan_retry_seconds=3.0,
        face_idle="neutral",
        face_thinking="doubt",
        face_talking="happy",
        face_error="sad",
        face_mode="avatar",
        sprite_sheet="assets/pets/default/spritesheet.webp",
        sprite_jpeg_quality=85,
        stream_timeout=65,
        retry_seconds=1.0,
        max_retry_seconds=30.0,
    )


def test_run_demo_uses_configured_serial_wav_transfer_settings():
    backend = FakeBackend()

    result = run_demo(
        backend,
        FakePiper(),
        _config(),
        "テストです",
        preset_name="chill",
    )

    assert result["status"] == "ok"
    assert backend.wav_calls
    assert backend.wav_calls[0]["chunk_size"] == 512
    assert backend.wav_calls[0]["chunk_delay"] == 0.015


def test_run_demo_sends_face_commands_for_avatar_mode():
    backend = FakeBackend()

    result = run_demo(
        backend,
        FakePiper(),
        _config(),
        "テストです",
        preset_name="chill",
    )

    assert result["status"] == "ok"
    assert "FACE:happy" in backend.commands
    assert "FACE:neutral" in backend.commands


def test_run_demo_does_not_send_face_commands_for_sprite_mode():
    backend = FakeBackend()
    config = _config()
    config.face_mode = "sprite"

    result = run_demo(
        backend,
        FakePiper(),
        config,
        "テストです",
        preset_name="chill",
    )

    assert result["status"] == "ok"
    assert all(not command.startswith("FACE:") for command in backend.commands)


def test_run_demo_delays_sprite_resume_and_motion_after_wav(monkeypatch):
    backend = FakeBackend()
    events = []

    def fake_sleep(seconds: float) -> None:
        events.append(("sleep", round(seconds, 2)))

    monkeypatch.setattr("xangi_stackchan.dance.time.sleep", fake_sleep)
    original_send_wav = backend.send_wav

    def tracked_send_wav(*args, **kwargs):
        events.append(("wav", None))
        return original_send_wav(*args, **kwargs)

    backend.send_wav = tracked_send_wav

    result = run_demo(
        backend,
        FixedWavPiper(_wav_bytes(1.0)),
        _config(),
        "テストです",
        preset_name="chill",
        before_wav_send=lambda: events.append(("pause", None)),
        after_wav_send=lambda: events.append(("resume", None)),
    )

    assert result["status"] == "ok"
    assert events[:5] == [
        ("pause", None),
        ("wav", None),
        ("sleep", 0.35),
        ("resume", None),
        ("sleep", 0.25),
    ]


def test_synthesize_text_downsamples_piper_wavs(monkeypatch):
    source_wav = _wav_bytes(1.0, rate=16000)
    monkeypatch.setenv("STACKCHAN_TTS_DOWNSAMPLE", "2")

    result = synthesize_text(
        "長い文章が句読点なしで続いてAtomS3Rでは分割したい",
        _config(),
        FixedWavPiper(source_wav),
    )

    assert len(result) >= 2
    assert all(len(wav) < len(source_wav) for _, wav in result)


def test_dance_loop_skips_move_when_backend_actor_is_none():
    backend = FakeBackend()
    backend.actor = None

    loop = DanceLoop(backend, PRESETS["happy"], idle_yaw=0.0, idle_pitch=5.0)
    loop._send_move(10.0, 5.0)

    assert backend.commands == []
