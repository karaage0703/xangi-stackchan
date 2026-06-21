"""Settings-UI HTTP server tests.

Focus on the multi-instance port auto-shift behaviour. The dance-demo /
camera plumbing is exercised manually with a real device, not here.
"""

from __future__ import annotations

import socket
from contextlib import closing
from pathlib import Path

import pytest

from xangi_stackchan.app_types import BridgeConfig
from xangi_stackchan.settings import RuntimeState
from xangi_stackchan.settings_server import _execute_demo, start_settings_server
from xangi_stackchan.stackchan import StackchanConfig


def _state(tmp_path: Path) -> RuntimeState:
    cfg = BridgeConfig(
        xangi_url="http://127.0.0.1:18888",
        thread_id=None,
        stackchan=StackchanConfig(wifi=False, host="", port="/dev/null", baud=921600),
        volume=128,
        tts="none",
        piper_bin="",
        piper_model="",
        piper_speaker=0,
        voicevox_url="",
        voicevox_speaker=0,
        serial_chunk=1024,
        serial_delay=0.005,
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
    return RuntimeState(cfg, tmp_path / "cfg.json", instance_id="test")


def _pick_free_port() -> int:
    # The kernel picks an unused port for us, then we close it. There is a
    # small race here but in practice the next bind reuses it quickly.
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeBackend:
    def __init__(self):
        self.wav_calls = []

    def send_command(self, command: str) -> dict:
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


class FakeSpriteAnimator:
    def __init__(self):
        self.events = []

    def pause(self):
        self.events.append("pause")

    def resume(self):
        self.events.append("resume")

    def set_expression(self, expression: str):
        self.events.append(("face", expression))


class FakeLocalSpriteAnimator(FakeSpriteAnimator):
    def keeps_running_during_wav(self):
        return True


def test_autoshift_picks_next_free_port(tmp_path: Path):
    busy_port = _pick_free_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", busy_port))
    try:
        server, bound = start_settings_server(
            _state(tmp_path), "127.0.0.1", busy_port, autoshift_tries=5
        )
        try:
            assert bound != busy_port
            assert busy_port < bound <= busy_port + 4
        finally:
            server.shutdown()
            server.server_close()
    finally:
        blocker.close()


def test_fail_fast_when_autoshift_tries_is_one(tmp_path: Path):
    busy_port = _pick_free_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(("127.0.0.1", busy_port))
    try:
        with pytest.raises(OSError):
            start_settings_server(
                _state(tmp_path), "127.0.0.1", busy_port, autoshift_tries=1
            )
    finally:
        blocker.close()


def test_binds_initial_port_when_free(tmp_path: Path):
    port = _pick_free_port()
    server, bound = start_settings_server(
        _state(tmp_path), "127.0.0.1", port, autoshift_tries=5
    )
    try:
        assert bound == port
    finally:
        server.shutdown()
        server.server_close()


def test_execute_demo_pauses_sprite_animator(tmp_path: Path):
    state = _state(tmp_path)
    state.update({"tts": "piper", "face_mode": "sprite"})
    animator = FakeSpriteAnimator()
    state.set_runtime(FakeBackend(), FakePiper())
    state.set_sprite_animator(animator)

    result = _execute_demo(state, {"text": "テストです", "preset": "chill"})

    assert result["status"] == "ok"
    assert animator.events == [("face", "happy"), "pause", "resume", ("face", "neutral")]


def test_execute_demo_keeps_local_sprite_animation_running(tmp_path: Path):
    state = _state(tmp_path)
    state.update({"tts": "piper", "face_mode": "sprite"})
    animator = FakeLocalSpriteAnimator()
    state.set_runtime(FakeBackend(), FakePiper())
    state.set_sprite_animator(animator)

    result = _execute_demo(state, {"text": "テストです", "preset": "chill"})

    assert result["status"] == "ok"
    assert animator.events == [("face", "happy"), ("face", "neutral")]
