from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from contextlib import closing
from pathlib import Path

from xangi_stackchan.app_types import BridgeConfig
from xangi_stackchan.settings import RuntimeState, config_to_dict, merge_config
from xangi_stackchan.settings_server import start_settings_server
from xangi_stackchan.stackchan import StackchanConfig, StackchanSimulator, create_backend


def _pick_free_port() -> int:
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _state(tmp_path: Path) -> RuntimeState:
    cfg = BridgeConfig(
        xangi_url="http://127.0.0.1:18888",
        thread_id=None,
        stackchan=StackchanConfig(simulator=True, port="/dev/null"),
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


def _read_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def test_simulator_backend_tracks_firmware_commands():
    backend = StackchanSimulator()

    assert backend.send_command("STATUS")["simulator"] is True
    assert backend.send_command("FACE:happy") == {
        "status": "ok",
        "face": "happy",
        "simulator": True,
    }
    assert backend.send_command("MOVE:120,-40")["yaw"] == 100.0
    assert backend.send_command("PUZZLE:thinking")["puzzle"] == "thinking"
    assert backend.send_command("STACKLED:blue")["stack_led"] == "blue"
    wav_result = backend.send_wav(b"RIFF" + b"\x00" * 32 + b"WAVE" + b"\x00" * 64)

    state = backend.snapshot()
    assert state["face"] == "happy"
    assert state["yaw"] == 100.0
    assert state["pitch"] == -30.0
    assert state["puzzle"] == "thinking"
    assert state["stack_led"] == "blue"
    assert state["state"] == "playing"
    assert state["wav_id"] == 1
    assert state["has_audio"] is True
    assert backend.latest_wav() == b"RIFF" + b"\x00" * 32 + b"WAVE" + b"\x00" * 64
    assert wav_result["simulator"] is True


def test_simulator_config_is_persisted_through_settings():
    base = _state(Path("/tmp")).snapshot()[0]
    merged = merge_config(base, {"simulator": "on"})

    assert merged.stackchan.simulator is True
    assert config_to_dict(merged)["simulator"] is True
    assert isinstance(create_backend(StackchanConfig(simulator=True)), StackchanSimulator)


def test_settings_server_serves_simulator_page_and_state(tmp_path: Path):
    state = _state(tmp_path)
    state.set_runtime(StackchanSimulator(), None)
    port = _pick_free_port()
    server, bound = start_settings_server(state, "127.0.0.1", port, autoshift_tries=1)
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{bound}/simulator", timeout=3) as response:
            html = response.read().decode("utf-8")
        assert "xangi-stackchan simulator" in html
        assert "enable audio" in html

        with urllib.request.urlopen(
            f"http://127.0.0.1:{bound}/simulator?reload=1",
            timeout=3,
        ) as response:
            assert response.status == 200

        data = _read_json(f"http://127.0.0.1:{bound}/api/simulator/state")
        assert data["simulator"] is True
        assert data["face"] == "neutral"
    finally:
        server.shutdown()
        server.server_close()


def test_settings_server_serves_latest_simulator_audio(tmp_path: Path):
    state = _state(tmp_path)
    backend = StackchanSimulator()
    wav = b"RIFF" + b"\x00" * 32 + b"WAVE" + b"\x01" * 64
    backend.send_wav(wav)
    state.set_runtime(backend, None)
    port = _pick_free_port()
    server, bound = start_settings_server(state, "127.0.0.1", port, autoshift_tries=1)
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{bound}/api/simulator/audio/latest.wav",
            timeout=3,
        ) as response:
            assert response.headers["Content-Type"] == "audio/wav"
            assert response.read() == wav
    finally:
        server.shutdown()
        server.server_close()


def test_simulator_api_returns_503_without_simulator_runtime(tmp_path: Path):
    state = _state(tmp_path)
    port = _pick_free_port()
    server, bound = start_settings_server(state, "127.0.0.1", port, autoshift_tries=1)
    try:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{bound}/api/simulator/state", timeout=3)
        except urllib.error.HTTPError as exc:
            assert exc.code == 503
        else:
            raise AssertionError("expected HTTP 503")
    finally:
        server.shutdown()
        server.server_close()
